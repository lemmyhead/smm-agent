#!/usr/bin/env bash
# Deploys smm-notify-reviewer (2nd gen Cloud Function, Firestore trigger on jobs/{jobId} updates).
# Usage: TELEGRAM_CHAT_ID=123456789 scripts/deploy-notify.sh
source "$(dirname "$0")/_env.sh"
: "${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID (Telegram chat ID, e.g. 123456789 or -1001234567890 for a group)}"

gcloud functions deploy smm-notify-reviewer \
  --gen2 --runtime=python312 --region="$REGION" \
  --source="$ROOT/notify-function" --entry-point=notify_reviewer \
  --trigger-location="$REGION" \
  --trigger-event-filters="type=google.cloud.firestore.document.v1.updated" \
  --trigger-event-filters="database=(default)" \
  --trigger-event-filters-path-pattern="document=jobs/{jobId}" \
  --service-account "$(sa sa-notify-function)" \
  --set-env-vars "TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID"

# The Eventarc trigger calls the function as sa-notify-function, so that account must be allowed to invoke it.
gcloud run services add-iam-policy-binding smm-notify-reviewer --region="$REGION" \
  --member="serviceAccount:$(sa sa-notify-function)" --role=roles/run.invoker >/dev/null
echo "sa-notify-function -> run.invoker on smm-notify-reviewer"
