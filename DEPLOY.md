# Deploying the SMM drafting agent to Google Cloud

Four services, deployed in this order. Every command below is wrapped in a script under
`scripts/`, so each step is one line; the sections explain what the script does and how to check it.

| # | Service | Type | GCP name | Runs as | Script |
|---|---|---|---|---|---|
| 0 | (infrastructure) | APIs, Firestore, bucket, secret, service accounts, roles | – | – | `scripts/provision.sh` |
| 1 | `render-service` | Cloud Run (Chromium) | `smm-render-service` | `sa-render-service` | `scripts/deploy-render.sh` |
| 2 | `orchestrator-agent` | Vertex AI Agent Engine (ADK) | `smm-orchestrator-agent` | `sa-agent-orchestrator` | `scripts/deploy-agent.sh` |
| 3 | `brief-intake` | Cloud Run | `smm-brief-intake` | `sa-brief-intake` | `scripts/deploy-intake.sh` |
| 4 | `notify-function` | Cloud Function (2nd gen) | `smm-notify-reviewer` | `sa-notify-function` | `scripts/deploy-notify.sh` |

Order matters: the agent needs the render service's URL, and intake needs the agent's resource name.
The notify function only needs Firestore, so it can go any time after step 0.

Run everything from the `smm-agent/` folder.

---

## Before you start

**1. Check which project gcloud is pointed at.** All scripts use the active project and print it
first, so you can stop if it's wrong.

```bash
gcloud auth list
gcloud config get-value project
```

- If `gcloud` is "command not found", it lives at `~/google-cloud-sdk/bin`. The scripts add that
  to `PATH` themselves, but for your own commands run `export PATH="$HOME/google-cloud-sdk/bin:$PATH"`.
- When this guide was written, the active project was `gen-lang-client-0718812360`. Projects named
  `gen-lang-client-…` are usually auto-created by Google AI Studio. Confirm it is the project you want
  and that **billing is enabled** on it (Cloud Run, Cloud Build and Agent Engine all need it):
  `gcloud billing projects describe $(gcloud config get-value project)`
- To use a different project: `gcloud config set project YOUR_PROJECT_ID` (or prefix a script with `PROJECT_ID=...`).
- You need Owner or Editor on the project. Cloud Build also has to be allowed to build from source;
  see [Troubleshooting](#troubleshooting) if the first `--source` deploy is refused.

**2. Create the Slack app** (about 5 minutes, only needed for step 4):

1. <https://api.slack.com/apps> → *Create New App* → *From scratch*.
2. *OAuth & Permissions* → Bot Token Scopes: `chat:write` and `files:write`.
3. *Install to Workspace* and copy the **Bot User OAuth Token** (`xoxb-…`).
4. In Slack, open the review channel and run `/invite @YourAppName`. The bot cannot post to a channel it isn't in.
5. Get the channel ID: right-click the channel → *View channel details* → the ID at the bottom (`C0123456789`).

**3. Python 3.12 + the ADK CLI** (only needed for step 2, the agent deploy runs from your machine):

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install "google-adk==2.9.2"
```

(`uv venv --python 3.12 .venv` works too if you use uv.) Leave the venv active for step 2.

---

## Step 0: Provision the infrastructure

```bash
scripts/provision.sh
```

This is the provisioning spec as an idempotent script (safe to re-run). It enables the APIs, creates
the Firestore database (native mode, `us-central1`), the bucket `smm-agent-assets-<PROJECT_ID>` with a
90-day delete rule, the empty `slack-bot-token` secret, the four service accounts and their roles.

Add the real Slack token to the secret. Type it in your own terminal so it stays out of files and git:

```bash
echo -n "xoxb-PASTE-YOUR-REAL-TOKEN" | gcloud secrets versions add slack-bot-token --data-file=-
```

Verify (each command must print something):

```bash
gcloud services list --enabled | grep -E "aiplatform|run|firestore|storage|secretmanager|eventarc"
gcloud firestore databases list
gcloud storage ls | grep smm-agent-assets
gcloud secrets versions list slack-bot-token
gcloud iam service-accounts list | grep sa-
```

Three additions to the spec, needed for the Firestore trigger and the agent: the `eventarc` and `pubsub`
APIs, `roles/eventarc.eventReceiver` on `sa-notify-function`, and `roles/logging.logWriter` on
`sa-agent-orchestrator`.

---

## Step 1: render-service

```bash
scripts/deploy-render.sh
```

What it does: copies `shared/` into the service folder (`scripts/sync-shared.sh`), builds the
Playwright/Chromium image with Cloud Build, and deploys `smm-render-service` with 2 GiB / 2 CPU,
concurrency 4, `min-instances=1`, private access only. Afterwards it grants `sa-agent-orchestrator`
the `run.invoker` role. This is the binding the provisioning spec says must come after this deploy.

The first build takes several minutes (the Playwright base image is large).

Test it on its own. Your user needs permission to call the private service first (Owners usually
have it already; this makes it explicit):

```bash
gcloud run services add-iam-policy-binding smm-render-service --region us-central1 \
  --member="user:$(gcloud config get-value account)" --role=roles/run.invoker

RENDER_URL=$(gcloud run services describe smm-render-service --region us-central1 --format='value(status.url)')
curl -sS -X POST "$RENDER_URL/render" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" -H "Content-Type: application/json" \
  -d '{"template_id":"square_1x1","width":1080,"height":1080,"variables":{
        "headline":"Hello from the render service","cta":"Try it","campaign_name":"Test",
        "logo_url":"https://REAL-LOGO-URL.png","background_image_url":"https://REAL-IMAGE-URL.jpg",
        "primary_color":"#12355b","accent_color":"#ffb703","font":"Inter"}}'
```

Expected: `{"gcs_path":"gs://smm-agent-assets-…/renders/….png","width":1080,"height":1080}`. Download and look at it:

```bash
gcloud storage cp gs://smm-agent-assets-<PROJECT_ID>/renders/<FILE>.png . && open <FILE>.png
```

---

## Step 2: orchestrator-agent (Vertex AI Agent Engine)

With the venv from "Before you start" active:

```bash
scripts/deploy-agent.sh
```

What it does:

1. Copies `platform_specs.yaml` into the agent package.
2. Writes the agent's runtime settings: `orchestrator_agent/.env` (just `RENDER_SERVICE_URL`, looked up from step 1)
   and `orchestrator_agent/.agent_engine_config.json` (display name `smm-orchestrator-agent`,
   runs as `sa-agent-orchestrator`, `min_instances: 0`).
3. Lets the Agent Engine service agent act as `sa-agent-orchestrator`
   (`roles/iam.serviceAccountTokenCreator`, best effort, see Troubleshooting).
4. Runs `adk deploy agent_engine --project … --region us-central1 --display_name smm-orchestrator-agent orchestrator_agent`,
   which builds a container with Cloud Build and creates the agent. This takes roughly 5–10 minutes.
5. Saves the agent's resource name (`projects/<NUMBER>/locations/us-central1/reasoningEngines/<ID>`) to
   `.agent-engine-resource`, which step 3 reads.

To **update** the agent later, pass the numeric ID at the end of that resource name so it updates in place
instead of creating a second agent:

```bash
AGENT_ENGINE_ID=1234567890 scripts/deploy-agent.sh
```

Verify it exists:

```bash
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/$(gcloud config get-value project)/locations/us-central1/reasoningEngines" | grep -E '"displayName"|"name"'
```

Model: the drafter uses `gemini-2.5-flash` in `us-central1`. To change it, add `DRAFT_MODEL=<model id>` to the
`.env` block in `scripts/deploy-agent.sh` and redeploy. Some newer Gemini models are only served from the `global`
location; also add `DRAFT_MODEL_LOCATION=global` for those.

---

## Step 3: brief-intake

```bash
scripts/deploy-intake.sh
```

Deploys `smm-brief-intake` as `sa-brief-intake`, private, with `AGENT_ENGINE_RESOURCE` set from step 2.
It adds `--no-cpu-throttling` to the spec's command. The agent run happens in a background task
after the 202 response is sent, and Cloud Run would otherwise freeze the container's CPU as soon as
the response goes out.

Callers must present a Google identity token and hold `roles/run.invoker` on the service
(your own account, as above; grant it to teammates or a service account the same way):

```bash
gcloud run services add-iam-policy-binding smm-brief-intake --region us-central1 \
  --member="user:$(gcloud config get-value account)" --role=roles/run.invoker
```

---

## Step 4: notify-function

```bash
SLACK_CHANNEL_ID=C0123456789 scripts/deploy-notify.sh
```

Deploys the 2nd gen function `smm-notify-reviewer` with a Firestore trigger: event
`google.cloud.firestore.document.v1.updated`, database `(default)`, path pattern `jobs/{jobId}`, trigger
location `us-central1`, running as `sa-notify-function`. The function itself ignores updates where
`status` didn't change to `ready_for_review` or `failed`.

The spec's command has no path pattern (it would fire for every document in the database) and no
`--source`/`--entry-point`, so both were added. The first deploy also creates the Eventarc trigger and can take a few minutes.

---

## Test the whole pipeline

Edit `scripts/sample-brief.json`: replace the two `REPLACE-WITH-A-REAL-…` URLs with a real logo and a real
background image (https, publicly reachable, or `gs://` objects the render service can read). Then:

```bash
scripts/smoke-test.sh
# {"job_id":"…"}
# HTTP 202
```

Watch the job (replace `JOB_ID`); status should go `drafting` → `rendering` → `ready_for_review` within a couple of minutes:

```bash
P=$(gcloud config get-value project)
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://firestore.googleapis.com/v1/projects/$P/databases/(default)/documents/jobs/JOB_ID" | grep -A1 '"status"'
```

Or open `https://console.cloud.google.com/firestore/databases/-default-/data/panel/jobs/JOB_ID?project=<PROJECT>`.
A Slack message with all drafts should appear in your channel, with the images attached in its thread.

**Failure path.** A brief that is well-formed but points at an image that doesn't exist should end in `failed`
with a readable error, and still post to Slack:

```bash
sed 's#https://REPLACE-WITH-A-REAL-IMAGE.jpg#https://example.com/does-not-exist.jpg#' scripts/sample-brief.json > /tmp/bad-brief.json
scripts/smoke-test.sh /tmp/bad-brief.json
```

A brief that is structurally invalid (missing `platforms`, unknown platform, malformed URL or colour) is rejected
by intake with **HTTP 400** and a field-by-field message *before any job exists*, so there is no Slack message for it.
See the note at the bottom.

---

## Updating a service later

| Changed | Command |
|---|---|
| `shared/platform_specs.yaml` or `shared/templates/*` | `scripts/deploy-render.sh`, then `AGENT_ENGINE_ID=<id> scripts/deploy-agent.sh`, then `scripts/deploy-intake.sh` (all three hold a copy) |
| render-service code | `scripts/deploy-render.sh` |
| agent code or prompt | `AGENT_ENGINE_ID=<id> scripts/deploy-agent.sh` |
| intake code | `scripts/deploy-intake.sh` |
| notify code | `SLACK_CHANNEL_ID=… scripts/deploy-notify.sh` |

If the render service URL ever changes (deleting and recreating it), redeploy the agent so it picks up the new `RENDER_SERVICE_URL`.

## Logs

```bash
gcloud run services logs read smm-render-service --region us-central1 --limit 50
gcloud run services logs read smm-brief-intake   --region us-central1 --limit 50
gcloud functions logs read smm-notify-reviewer --gen2 --region us-central1 --limit 50
gcloud logging read 'resource.type="aiplatform.googleapis.com/ReasoningEngine"' --limit 30 --freshness=1h
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `gcloud run deploy --source` fails with a Cloud Build or Artifact Registry permission error | Newer projects run builds as the default Compute service account. Grant it the builder role: `gcloud projects add-iam-policy-binding $P --member="serviceAccount:$(gcloud projects describe $P --format='value(projectNumber)')-compute@developer.gserviceaccount.com" --role=roles/cloudbuild.builds.builder` |
| Deploy says you lack `iam.serviceaccounts.actAs` | Your account needs `roles/iam.serviceAccountUser` on the service account being attached (Owner already has it). |
| `deploy-agent.sh` fails on permissions / "service agent" | Run the script once (this creates the Agent Engine service agent), then run it again so the token-creator binding in step 3 of the script succeeds. Binding by hand: `gcloud iam service-accounts add-iam-policy-binding sa-agent-orchestrator@$P.iam.gserviceaccount.com --member="serviceAccount:service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com" --role=roles/iam.serviceAccountTokenCreator` |
| Job `failed`: "render service returned 403" | The `run.invoker` binding for `sa-agent-orchestrator` is missing. Re-run `scripts/deploy-render.sh`. |
| Job `failed`: "could not load image" | The logo or background URL isn't reachable from the internet, isn't an image, is over 15 MB, or is a `gs://` object `sa-render-service` cannot read (it only has access to the assets bucket). |
| Job `failed`: "Could not run the drafting agent … 403/404" | Intake's `AGENT_ENGINE_RESOURCE` is wrong or `sa-brief-intake` lacks `roles/aiplatform.user`. Check `gcloud run services describe smm-brief-intake --region us-central1 --format='value(spec.template.spec.containers[0].env)'`. |
| Job `failed` with a model "not found" or "not supported in location" error | Set `DRAFT_MODEL` (and possibly `DRAFT_MODEL_LOCATION=global`) as described in step 2. |
| Job stuck on `drafting` | Read the agent logs (command above). Intake also marks a job `failed` if the agent run ends without finishing it. |
| No Slack message, function logs show `not_in_channel` / `channel_not_found` | Invite the bot to the channel (`/invite @App`) and check `SLACK_CHANNEL_ID` is the ID, not the name. |
| Function logs show `missing_scope` | Add `chat:write` and `files:write`, reinstall the app, and add the new token as a new secret version. |
| Function logs show a Secret Manager permission error | `sa-notify-function` needs `roles/secretmanager.secretAccessor` on the secret and the secret needs a version. |
| Function never triggers | Confirm the trigger exists: `gcloud eventarc triggers list --location=us-central1`. Then confirm `eventarc.googleapis.com` is enabled and the function's service account has `roles/eventarc.eventReceiver`. |
| First render after idle is slow | Expected only if `min-instances` was lowered; the script keeps 1 warm instance. |

## Cost notes

- `smm-render-service` keeps one 2-CPU / 2 GiB instance running all the time (`min-instances=1`, as the spec asks).
  It's the biggest fixed cost here. Setting it to 0 saves that, at the price of a slow first request.
- `smm-brief-intake` uses instance-based billing (`--no-cpu-throttling`); it scales to zero when idle, so it is only billed while running.
- The agent has `min_instances: 0`; Gemini and Cloud Build usage is billed per use.
- Rendered PNGs are deleted after 90 days by the bucket lifecycle rule.

## Where this differs from the specs

- **Invalid briefs get a 400 and no job.** The build spec says both "return 400 on invalid payload" (Service A) and "a
  deliberately broken brief (missing platforms, bad URL) ends in `failed` and still produces a Slack notification"
  (acceptance criteria). They conflict for structurally invalid input. I followed Service A: structural problems get a 400;
  problems found later (unreachable image, model or render errors) become `failed` + Slack. If you'd rather have every
  bad brief create a `failed` job and ping Slack, that is a small change in `brief-intake/main.py`.
- `--allow-unauthenticated=false` in the spec is written `--no-allow-unauthenticated` in gcloud.
- Added to the spec's commands: `--execution-environment gen2` (render), `--no-cpu-throttling` (intake),
  `--source`, `--entry-point` and the path pattern (notify), `ASSETS_BUCKET` on the render service.
- `gcloud storage` replaces `gsutil` in `provision.sh` (same result).
- The agent is deployed with the `adk deploy agent_engine` CLI, not a hand-written `agent_engines.create(...)` call.
  This ADK version (2.9.2) deploys a container built from your agent folder, and the CLI is the documented path.
- Copy limits apply to the post `body` only (not headline or hashtags). If the model overshoots, the body is cut at a
  word boundary and ends with "…".

## What has and hasn't been tested

Tested locally (38 automated tests): the render service in real Chromium (all four templates at exact pixel sizes,
bad-image and injection cases), the agent pipeline end to end through ADK's runner with a fake Gemini, a fake render
service and a fake Firestore, brief validation, and the notify function against real Firestore event protobufs
with a fake Slack. Shell scripts are syntax-checked.

**Not tested, because nothing was deployed and no Docker or live GCP calls were made:** the container builds,
the real Gemini call, the Agent Engine REST calls in `brief-intake/agent_client.py` (session creation and `:streamQuery`;
if the first end-to-end run fails in `brief-intake`'s logs at "creating agent session", that file is where to look), IAM
behaviour, the Eventarc trigger, and real Slack posting. Expect to fix small things on the first run.
