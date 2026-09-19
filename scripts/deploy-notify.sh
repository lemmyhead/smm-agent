#!/usr/bin/env bash
# Deploys smm-notify-reviewer (2nd gen Cloud Function, Firestore trigger on jobs/{jobId} updates).
# Usage: SLACK_CHANNEL_ID=C0123456789 scripts/deploy-notify.sh
source "$(dirname "$0")/_env.sh"
: "${SLACK_CHANNEL_ID:?Set SLACK_CHANNEL_ID (Slack channel ID, e.g. C0123456789)}"

gcloud functions deploy smm-notify-reviewer \
  --gen2 --runtime=python312 --region="$REGION" \
  --source="$ROOT/notify-function" --entry-point=notify_reviewer \
  --trigger-location="$REGION" \
  --trigger-event-filters="type=google.cloud.firestore.document.v1.updated" \
  --trigger-event-filters="database=(default)" \
  --trigger-event-filters-path-pattern="document=jobs/{jobId}" \
  --service-account "$(sa sa-notify-function)" \
  --set-env-vars "SLACK_CHANNEL_ID=$SLACK_CHANNEL_ID"
