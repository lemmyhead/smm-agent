#!/usr/bin/env bash
# Idempotent: creates the GCP resources from gcp-provisioning-spec.md. Safe to re-run.
# Usage: scripts/provision.sh            (uses the active gcloud project)
source "$(dirname "$0")/_env.sh"

echo "== 1. APIs"
# eventarc + pubsub are additions to the spec: the Firestore-triggered function needs them.
gcloud services enable \
  aiplatform.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  cloudfunctions.googleapis.com firestore.googleapis.com storage.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com \
  eventarc.googleapis.com pubsub.googleapis.com iam.googleapis.com

echo "== 2. Firestore (native, default database)"
if gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  echo "already exists"
else
  gcloud firestore databases create --location="$REGION" --type=firestore-native
fi

echo "== 3. Storage bucket + 90-day lifecycle"
if gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  echo "bucket already exists"
else
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
fi
LIFECYCLE="$(mktemp)"
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":90}}]}' > "$LIFECYCLE"
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file="$LIFECYCLE"
rm -f "$LIFECYCLE"

echo "== 4. Secret container (value is added by hand, see DEPLOY.md)"
gcloud secrets describe slack-bot-token >/dev/null 2>&1 || \
  gcloud secrets create slack-bot-token --replication-policy=automatic

echo "== 5. Service accounts"
for name in "sa-brief-intake:SMM brief intake" "sa-agent-orchestrator:SMM agent orchestrator" \
            "sa-render-service:SMM render service" "sa-notify-function:SMM notify function"; do
  id="${name%%:*}"; display="${name#*:}"
  gcloud iam service-accounts describe "$(sa "$id")" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "$id" --display-name="$display"
done

echo "== 5b. Roles"
bind() { # bind <sa-name> <role>
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$(sa "$1")" --role="$2" \
    --condition=None --quiet >/dev/null
  echo "  $1 -> $2"
}
bind sa-brief-intake       roles/datastore.user
bind sa-brief-intake       roles/aiplatform.user
bind sa-agent-orchestrator roles/datastore.user
bind sa-agent-orchestrator roles/aiplatform.user
bind sa-agent-orchestrator roles/storage.objectAdmin
bind sa-agent-orchestrator roles/logging.logWriter        # addition: lets the deployed agent write logs
bind sa-render-service     roles/storage.objectAdmin
bind sa-notify-function    roles/datastore.viewer
bind sa-notify-function    roles/storage.objectViewer
bind sa-notify-function    roles/eventarc.eventReceiver   # addition: required for Firestore triggers
gcloud secrets add-iam-policy-binding slack-bot-token \
  --member="serviceAccount:$(sa sa-notify-function)" --role=roles/secretmanager.secretAccessor >/dev/null
echo "  sa-notify-function -> secretAccessor on slack-bot-token"

echo
echo "Done. The run.invoker binding for sa-agent-orchestrator is applied by deploy-render.sh"
echo "(the render service has to exist first). Verification:"
echo "  gcloud services list --enabled | grep -E 'aiplatform|run|firestore|storage|secretmanager|eventarc'"
echo "  gcloud firestore databases list; gcloud storage ls | grep smm-agent-assets"
echo "  gcloud secrets list | grep slack-bot-token; gcloud iam service-accounts list | grep sa-"
