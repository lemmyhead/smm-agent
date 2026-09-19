#!/usr/bin/env bash
# Deploys the ADK orchestrator to Vertex AI Agent Engine (display name smm-orchestrator-agent).
# Re-running with AGENT_ENGINE_ID=<numeric id> updates the existing agent instead of creating a new one.
source "$(dirname "$0")/_env.sh"
"$ROOT/scripts/sync-shared.sh"

command -v adk >/dev/null || { echo "adk not found. Install it: python3.12 -m venv .venv && . .venv/bin/activate && pip install 'google-adk==2.9.2'" >&2; exit 1; }
RENDER_URL="$(gcloud run services describe smm-render-service --region "$REGION" --format='value(status.url)')"
[ -n "$RENDER_URL" ] || { echo "smm-render-service is not deployed yet: run deploy-render.sh first" >&2; exit 1; }

AGENT_DIR="$ROOT/orchestrator-agent/orchestrator_agent"
# Runtime env vars for the agent (do NOT put GOOGLE_CLOUD_PROJECT/LOCATION here: reserved).
cat > "$AGENT_DIR/.env" <<ENVEOF
RENDER_SERVICE_URL=$RENDER_URL
ENVEOF
cat > "$AGENT_DIR/.agent_engine_config.json" <<CFGEOF
{
  "display_name": "smm-orchestrator-agent",
  "description": "SMM drafting orchestrator (drafts copy, renders cover images)",
  "service_account": "$(sa sa-agent-orchestrator)",
  "min_instances": 0
}
CFGEOF

# The Agent Engine service agent must be able to mint tokens as the custom service account.
# The service agent can be missing until the first agent is created, so this is best-effort.
gcloud iam service-accounts add-iam-policy-binding "$(sa sa-agent-orchestrator)" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
  --role=roles/iam.serviceAccountTokenCreator >/dev/null 2>&1 \
  && echo "token-creator binding ok" \
  || echo "NOTE: could not bind token creator yet (service agent not created). If the deploy fails on permissions, re-run this script after the first attempt."

cd "$ROOT/orchestrator-agent"
LOG="$(mktemp)"
EXTRA=()
[ -n "${AGENT_ENGINE_ID:-}" ] && EXTRA=(--agent_engine_id "$AGENT_ENGINE_ID")
adk deploy agent_engine --project "$PROJECT_ID" --region "$REGION" \
  --display_name smm-orchestrator-agent ${EXTRA[@]+"${EXTRA[@]}"} orchestrator_agent 2>&1 | tee "$LOG"

RESOURCE="$(grep -oE 'projects/[0-9]+/locations/[a-z0-9-]+/reasoningEngines/[0-9]+' "$LOG" | head -1 || true)"
if [ -n "$RESOURCE" ]; then
  echo "$RESOURCE" > "$ROOT/.agent-engine-resource"
  echo "Agent resource saved to $ROOT/.agent-engine-resource: $RESOURCE"
else
  echo "Could not find the resource name in the output. List agents with:" >&2
  echo "  curl -s -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" https://$REGION-aiplatform.googleapis.com/v1/projects/$PROJECT_ID/locations/$REGION/reasoningEngines" >&2
fi
