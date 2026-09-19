#!/usr/bin/env bash
# Copies shared/ into the places that get deployed. `gcloud run deploy --source .` and
# `adk deploy` only upload their own folder, so ../shared is not reachable from them.
# Run this before every deploy of render-service or orchestrator-agent.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$ROOT/render-service/shared"
mkdir -p "$ROOT/render-service/shared"
cp -R "$ROOT/shared/." "$ROOT/render-service/shared/"
cp "$ROOT/shared/platform_specs.yaml" "$ROOT/orchestrator-agent/orchestrator_agent/platform_specs.yaml"
rm -rf "$ROOT/brief-intake/shared" && mkdir -p "$ROOT/brief-intake/shared"
cp "$ROOT/shared/platform_specs.yaml" "$ROOT/brief-intake/shared/platform_specs.yaml"
rm -rf "$ROOT/telegram-intake/shared" && mkdir -p "$ROOT/telegram-intake/shared"
cp "$ROOT/shared/platform_specs.yaml" "$ROOT/telegram-intake/shared/platform_specs.yaml"
cp "$ROOT/brand-context/brandcore.py" "$ROOT/telegram-intake/brandcore.py"
echo "synced shared/ -> render-service, orchestrator-agent, brief-intake; brandcore.py -> telegram-intake"
