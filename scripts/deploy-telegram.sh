#!/usr/bin/env bash
# Deploys smm-telegram-intake (the bot that manages the brand context) and points the Telegram webhook at it.
# Usage: TELEGRAM_ALLOWED_CHAT_IDS=440581589 scripts/deploy-telegram.sh     (comma-separate several chat ids)
# Needs the token already stored in the telegram-bot-token secret (DEPLOY.md step 0).
#
# Unlike the other services this one is public (--allow-unauthenticated): Telegram cannot send a Google identity
# token. It is protected by Telegram's secret-token header (a random value stored in telegram-webhook-secret) and
# it ignores every chat that is not in TELEGRAM_ALLOWED_CHAT_IDS.
source "$(dirname "$0")/_env.sh"
: "${TELEGRAM_ALLOWED_CHAT_IDS:?Set TELEGRAM_ALLOWED_CHAT_IDS (your chat id, e.g. 440581589)}"
"$ROOT/scripts/sync-shared.sh"

gcloud secrets versions list telegram-bot-token --filter="state=enabled" --format="value(name)" | grep -q . \
  || { echo "telegram-bot-token has no value yet (see DEPLOY.md step 0)" >&2; exit 1; }
if ! gcloud secrets versions list telegram-webhook-secret --filter="state=enabled" --format="value(name)" | grep -q .; then
  openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add telegram-webhook-secret --data-file=- >/dev/null
  echo "generated a new webhook secret"
fi

# --no-cpu-throttling: files are processed (Gemini calls) after the 200 has been returned to Telegram.
# The ^@^ prefix makes '@' the separator so the chat id list can contain commas.
gcloud run deploy smm-telegram-intake \
  --source "$ROOT/telegram-intake" --region "$REGION" \
  --service-account "$(sa sa-telegram-intake)" \
  --no-cpu-throttling --memory 512Mi --max-instances 2 \
  --allow-unauthenticated \
  --set-env-vars "^@^PROJECT_ID=$PROJECT_ID@TELEGRAM_ALLOWED_CHAT_IDS=$TELEGRAM_ALLOWED_CHAT_IDS" \
  --set-secrets "TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest"

URL="$(gcloud run services describe smm-telegram-intake --region "$REGION" --format='value(status.url)')"
echo "Service URL: $URL"

# Point Telegram at the service. The token is read from Secret Manager and sent only to api.telegram.org.
TOKEN="$(gcloud secrets versions access latest --secret telegram-bot-token)"
SECRET="$(gcloud secrets versions access latest --secret telegram-webhook-secret)"
curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
  --data-urlencode "url=${URL}/telegram" --data-urlencode "secret_token=${SECRET}" \
  --data-urlencode 'allowed_updates=["message"]' --data-urlencode "drop_pending_updates=true" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("setWebhook:", r.get("ok"), r.get("description", ""))'

# Old update ids expire after two days (best effort; needs a Firestore TTL policy).
gcloud firestore fields ttls update expires_at --collection-group=telegram_updates --enable-ttl --async >/dev/null 2>&1 || true
