#!/usr/bin/env bash
# Manage the brand context (voice guide, example posts, visual defaults). Examples:
#   scripts/brand-context.sh voice ~/docs/tone-of-voice.pdf
#   scripts/brand-context.sh examples ~/docs/posts.txt ~/docs/screenshots/*.png
#   scripts/brand-context.sh extract ~/docs/visual-guidelines.pdf   # then review brand-proposal.json
#   scripts/brand-context.sh apply brand-proposal.json
#   scripts/brand-context.sh logo ~/docs/logo.png
#   scripts/brand-context.sh show [--full]
# Needs the Python packages in brand-context/requirements.txt and your ADC login (DEPLOY.md step 4).
source "$(dirname "$0")/_env.sh"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
PROJECT_ID="$PROJECT_ID" exec "$PY" "$ROOT/brand-context/brandctx.py" "$@"
