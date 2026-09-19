# Sourced by the other scripts. Reads the active gcloud project unless PROJECT_ID is already set.
set -euo pipefail
export PATH="$HOME/google-cloud-sdk/bin:$PATH"   # harmless if gcloud is already on PATH elsewhere
command -v gcloud >/dev/null || { echo "gcloud not found on PATH" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT_ID" ] || { echo "No project set. Run: gcloud config set project YOUR_PROJECT_ID" >&2; exit 1; }
REGION="${REGION:-us-central1}"
BUCKET="smm-agent-assets-${PROJECT_ID}"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
sa() { echo "$1@${PROJECT_ID}.iam.gserviceaccount.com"; }
echo ">> project: $PROJECT_ID ($PROJECT_NUMBER)   region: $REGION   account: $(gcloud config get-value account 2>/dev/null)"
