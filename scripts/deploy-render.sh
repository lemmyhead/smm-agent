#!/usr/bin/env bash
# Deploys smm-render-service and lets the orchestrator's service account call it.
source "$(dirname "$0")/_env.sh"
"$ROOT/scripts/sync-shared.sh"

gcloud run deploy smm-render-service \
  --source "$ROOT/render-service" --region "$REGION" \
  --memory 2Gi --cpu 2 --concurrency 4 --min-instances 1 \
  --execution-environment gen2 \
  --service-account "$(sa sa-render-service)" \
  --set-env-vars "ASSETS_BUCKET=$BUCKET" \
  --no-allow-unauthenticated

# The cross-dependency from the provisioning spec: only possible once the service exists.
gcloud run services add-iam-policy-binding smm-render-service --region "$REGION" \
  --member="serviceAccount:$(sa sa-agent-orchestrator)" --role=roles/run.invoker

echo "Render service URL: $(gcloud run services describe smm-render-service --region "$REGION" --format='value(status.url)')"
