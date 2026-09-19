#!/usr/bin/env bash
# Sends a sample brief to brief-intake and prints the job id. Usage: scripts/smoke-test.sh [brief.json]
source "$(dirname "$0")/_env.sh"
URL="$(gcloud run services describe smm-brief-intake --region "$REGION" --format='value(status.url)')"
BRIEF="${1:-$ROOT/scripts/sample-brief.json}"
curl -sS -X POST "$URL/briefs" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" -d @"$BRIEF" -w '\nHTTP %{http_code}\n'
