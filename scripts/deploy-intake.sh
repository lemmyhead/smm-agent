#!/usr/bin/env bash
# Deploys smm-brief-intake (private Cloud Run service).
# Needs AGENT_ENGINE_RESOURCE, or the .agent-engine-resource file written by deploy-agent.sh.
source "$(dirname "$0")/_env.sh"
"$ROOT/scripts/sync-shared.sh"

AGENT_ENGINE_RESOURCE="${AGENT_ENGINE_RESOURCE:-$(cat "$ROOT/.agent-engine-resource" 2>/dev/null || true)}"
[ -n "$AGENT_ENGINE_RESOURCE" ] || { echo "Set AGENT_ENGINE_RESOURCE=projects/N/locations/$REGION/reasoningEngines/ID" >&2; exit 1; }

# --no-cpu-throttling: the agent run continues in a background task AFTER the 202 is returned,
# and Cloud Run would otherwise starve the container of CPU once the response is sent.
gcloud run deploy smm-brief-intake \
  --source "$ROOT/brief-intake" --region "$REGION" \
  --service-account "$(sa sa-brief-intake)" \
  --no-cpu-throttling \
  --set-env-vars "AGENT_ENGINE_RESOURCE=$AGENT_ENGINE_RESOURCE" \
  --no-allow-unauthenticated

echo "Intake URL: $(gcloud run services describe smm-brief-intake --region "$REGION" --format='value(status.url)')"
