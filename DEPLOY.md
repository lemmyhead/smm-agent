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
| 5 | `telegram-intake` (optional) | Cloud Run, public webhook | `smm-telegram-intake` | `sa-telegram-intake` | `scripts/deploy-telegram.sh` |

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

**2. Create the Telegram bot** (about 5 minutes, only needed for step 4):

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the **bot token** (`123456:ABC-…`).
2. Open a chat with your new bot and press *Start* (a bot cannot message you until you have written to it first).
   For a group instead: add the bot to the group and send any message there.
3. Get the chat ID: open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read `"chat":{"id": …}`.
   A private chat ID is a positive number; a group ID is negative (`-100…`). If `result` is empty, send the bot another message and reload.

**3. Python 3.12 + the ADK CLI** (only needed for step 2, the agent deploy runs from your machine):

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install "google-adk==2.9.2" "google-cloud-aiplatform[agent_engines,adk]"
```

(`uv venv --python 3.12 .venv` works too if you use uv.) Leave the venv active for step 2.

**4. Application Default Credentials** (also only for step 2). The ADK CLI uses Google's Python libraries, which
don't reuse your `gcloud` login. Sign in once in the browser and point quota at your project:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project "$(gcloud config get-value project)"
```

---

## Step 0: Provision the infrastructure

```bash
scripts/provision.sh
```

This is the provisioning spec as an idempotent script (safe to re-run). It enables the APIs, creates
the Firestore database (native mode, `us-central1`), the bucket `smm-agent-assets-<PROJECT_ID>` with a
90-day delete rule on `renders/`, the empty `telegram-bot-token` secret, the four service accounts and their roles.

Add the real bot token to the secret. Type it in your own terminal so it stays out of files and git:

```bash
echo -n "PASTE-YOUR-BOT-TOKEN" | gcloud secrets versions add telegram-bot-token --data-file=-
```

Verify (each command must print something):

```bash
gcloud services list --enabled | grep -E "aiplatform|run|firestore|storage|secretmanager|eventarc"
gcloud firestore databases list
gcloud storage ls | grep smm-agent-assets
gcloud secrets versions list telegram-bot-token
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
2. Writes the agent's runtime settings: `orchestrator_agent/.env` (`RENDER_SERVICE_URL`, looked up from step 1, and `FIRESTORE_PROJECT_ID`, because inside Agent Engine the default project is the project *number*, which Firestore rejects)
   and `orchestrator_agent/.agent_engine_config.json` (display name `smm-orchestrator-agent`,
   runs as `sa-agent-orchestrator`, `min_instances: 0`).
3. Lets the Agent Engine service agent act as `sa-agent-orchestrator`
   (`roles/iam.serviceAccountTokenCreator`, best effort, see Troubleshooting).
4. Runs `adk deploy agent_engine --project … --region us-central1 --display_name smm-orchestrator-agent orchestrator_agent`,
   which builds a container with Cloud Build and creates the agent. This takes roughly 5–10 minutes.
5. Saves the agent's resource name (`projects/<PROJECT>/locations/us-central1/reasoningEngines/<ID>`) to
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
TELEGRAM_CHAT_ID=123456789 scripts/deploy-notify.sh
```

Deploys the 2nd gen function `smm-notify-reviewer` with a Firestore trigger: event
`google.cloud.firestore.document.v1.updated`, database `(default)`, path pattern `jobs/{jobId}`, trigger
location `us-central1`, running as `sa-notify-function`. The function itself ignores updates where
`status` didn't change to `ready_for_review` or `failed`.

The spec's command has no path pattern (it would fire for every document in the database) and no
`--source`/`--entry-point`, so both were added. The first deploy also creates the Eventarc trigger and can take a few minutes.

---

## Step 5 (optional): brand context

Teach the agent your brand once, so a brief only needs the campaign details. The context lives in Firestore
(`brand/default`); the original files are kept under `gs://smm-agent-assets-<PROJECT>/brand/default/` (the 90-day
delete rule only applies to `renders/`, so these stay).

What each piece does:

| You give it | What the agent does with it |
|---|---|
| Tone-of-voice guide (`.md`, `.txt`, `.rtf` or PDF) | The whole text goes into the drafting prompt on every run. A PDF is transcribed to markdown by Gemini when you upload it, so you can check what was read. Limit 30,000 characters. |
| Example posts (text files with posts separated by a line of `---`, and/or screenshots `.png/.jpg/.webp`) | Text examples are shown to the drafter as style references; screenshots are attached to the drafting request as images (Gemini reads them). Up to 12 screenshots of 4 MB each. |
| Visual-guidelines PDF | Gemini extracts the primary and accent colour and the font, which become the **default brand block**. It also lists your layout, logo and imagery rules so you can see what needs manual template work. Writing rules it finds are added to the drafting prompt. |
| Logo image | Becomes the default `brand.logo_url`. |

### Through the Telegram bot (no terminal needed)

```bash
TELEGRAM_ALLOWED_CHAT_IDS=<your chat id> scripts/deploy-telegram.sh
```

Deploys `smm-telegram-intake` and points your bot's webhook at it (this replaces the `getUpdates` URL you used to find
your chat ID; that URL stops returning messages once a webhook is set). Then chat with your bot:

| You send | What happens |
|---|---|
| `/voice`, then the file (or paste the text) | Saved. A PDF is transcribed and the start of it is echoed back so you can check it. |
| `/examples`, then captions (one message each), a `.txt` file, or screenshots, then `/done` | Saved. `/examples add` keeps the ones already there. |
| `/guidelines`, then the PDF | It replies with the colours, font, palette and rules it found, and a **NEEDS MANUAL TEMPLATE WORK** list. **Nothing is saved yet.** |
| `/set primary #1f4d3a`, `/set accent #f2b134`, `/set font Lora` | Correct the proposal, then `/save` to keep it (or `/cancel`). |
| `/logo`, then the image | Saved as the default logo. |
| `/show`, `/show full`, `/clear` | See what is saved / delete everything (asks you to confirm). |

Because this service must be reachable by Telegram, it is **public**, unlike the others. It is protected three ways:
requests must carry Telegram's secret-token header (a random value in the `telegram-webhook-secret` secret; anything
else gets 401), the bot answers only the chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS` and stays silent for everyone else, and its
service account can only touch the assets bucket and the two secrets. The API docs pages are switched off. Bots can only
download files up to 20 MB. Chat ID list: comma-separate several.

### From a terminal instead

Run these from the repo root. They use your own login (step 4 above) and the packages in `brand-context/requirements.txt`
(`pip install -r brand-context/requirements.txt` in the same venv as the ADK):

```bash
scripts/brand-context.sh voice ~/Documents/tone-of-voice.pdf
scripts/brand-context.sh examples ~/Documents/posts.txt ~/Documents/screenshots/*.png
scripts/brand-context.sh extract ~/Documents/visual-guidelines.pdf     # writes brand-proposal.json and prints it
```

`extract` saves nothing yet. It prints the colours, font, palette and rules it found, plus a list headed
**NEEDS MANUAL TEMPLATE WORK** (layout, logo and imagery rules cannot be applied automatically; they need edits to
`shared/templates/*.html.j2`). Correct `brand-proposal.json` if anything is wrong, then save it and add the logo:

```bash
scripts/brand-context.sh apply brand-proposal.json
scripts/brand-context.sh logo ~/Documents/logo.png
scripts/brand-context.sh show            # add --full to read everything the drafter will see
```

Then deploy the two services that read it:

```bash
AGENT_ENGINE_ID=<id> scripts/deploy-agent.sh    # the drafter reads voice, examples and writing rules
scripts/deploy-intake.sh                        # intake fills in the default brand block and tone
```

Afterwards a brief can drop `brand` and `tone`:

```json
{"campaign_name": "Spring Sale", "key_message": "…", "cta": "Shop now",
 "background_image_url": "https://…/photo.jpg", "platforms": ["facebook", "threads"]}
```

**Language.** The drafter writes in the language of the voice guide and example posts, unless the brief explicitly asks
for another language; with no guide and no examples it uses the language of the key message. (Change the "Language" line
in `INSTRUCTION` in `orchestrator-agent/orchestrator_agent/agent.py` if you want a different rule.)

**File formats.** Rich-text (`.rtf`) files, for example saved from TextEdit, are converted to plain text automatically, even
if they were saved with a `.txt` extension. One example post can be at most 5,000 characters; a longer file is refused
with a hint to put a line containing only `---` between posts. Screenshot and text examples together are capped at
12 screenshots and 30,000 characters, and the check is atomic, so files sent at the same moment cannot slip past it.

Anything you do put in the brief wins: a partial `brand` (for example only `primary_color`) overrides just that field and
keeps the rest of the defaults. `tone`, if given, is used together with the voice guide. Without a saved brand block, a
brief must still contain the whole `brand`; without a voice guide it must contain `tone`. Either gap is a 400 that names the fields.

Re-run any command to replace what it saved (`examples --add` keeps the existing ones). `scripts/brand-context.sh clear --yes`
deletes the saved context and its files. `BRAND_PROFILE` (default `default`) selects which profile the agent and intake read.

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
A Telegram message with the campaign name should appear in your chat, followed by one reply per platform draft and the rendered images as a photo album.

**Failure path.** A brief that is well-formed but points at an image that doesn't exist should end in `failed`
with a readable error, and still post to Telegram:

```bash
sed 's#https://REPLACE-WITH-A-REAL-IMAGE.jpg#https://example.com/does-not-exist.jpg#' scripts/sample-brief.json > /tmp/bad-brief.json
scripts/smoke-test.sh /tmp/bad-brief.json
```

A brief that is structurally invalid (missing `platforms`, unknown platform, malformed URL or colour) is rejected
by intake with **HTTP 400** and a field-by-field message *before any job exists*, so there is no Telegram message for it.
See the note at the bottom.

---

## Updating a service later

| Changed | Command |
|---|---|
| `shared/platform_specs.yaml` or `shared/templates/*` | `scripts/deploy-render.sh`, then `AGENT_ENGINE_ID=<id> scripts/deploy-agent.sh`, then `scripts/deploy-intake.sh` (all three hold a copy) |
| render-service code | `scripts/deploy-render.sh` |
| agent code or prompt | `AGENT_ENGINE_ID=<id> scripts/deploy-agent.sh` |
| intake code | `scripts/deploy-intake.sh` |
| brand context (voice, examples, guidelines) | `scripts/brand-context.sh …`, no redeploy: the agent and intake read it on every request |
| notify code | `TELEGRAM_CHAT_ID=… scripts/deploy-notify.sh` |

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
| Job `failed`: "The database (default) does not exist for project <number>" in the agent logs | The agent's `.env` is missing `FIRESTORE_PROJECT_ID` (redeploy with `scripts/deploy-agent.sh`; the script writes it). |
| First `gcloud run deploy --source` asks to create an Artifact Registry repository `cloud-run-source-deploy` | Answer `Y`; it is a one-time prompt per project and region. |
| `adk deploy` asks about telemetry | Optional; either answer is fine. |
| Job `failed`: "render service returned 403" | The `run.invoker` binding for `sa-agent-orchestrator` is missing. Re-run `scripts/deploy-render.sh`. |
| Job `failed`: "could not load image" | The logo or background URL isn't reachable from the internet, isn't an image, is over 15 MB, or is a `gs://` object `sa-render-service` cannot read (it only has access to the assets bucket). |
| Job `failed`: "Could not run the drafting agent … 403/404" | Intake's `AGENT_ENGINE_RESOURCE` is wrong or `sa-brief-intake` lacks `roles/aiplatform.user`. Check `gcloud run services describe smm-brief-intake --region us-central1 --format='value(spec.template.spec.containers[0].env)'`. |
| Job `failed` with a model "not found" or "not supported in location" error | Set `DRAFT_MODEL` (and possibly `DRAFT_MODEL_LOCATION=global`) as described in step 2. |
| Job `failed`: "could not read brand example image … serviceusage.services.use" | `sa-agent-orchestrator` needs `roles/serviceusage.serviceUsageConsumer` (`scripts/provision.sh` grants it). |
| The bot does not answer | Check `getWebhookInfo` for `last_error_message` (`curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`), confirm your chat ID is in `TELEGRAM_ALLOWED_CHAT_IDS` (the bot ignores every other chat silently), and read the logs: `gcloud run services logs read smm-telegram-intake --region us-central1 --limit 30`. |
| Job stuck on `drafting` | Read the agent logs (command above). Intake also marks a job `failed` if the agent run ends without finishing it. |
| No Telegram message, function logs show `chat not found` or `bot can't initiate conversation` | Press *Start* on the bot (or add it to the group), and check `TELEGRAM_CHAT_ID` is the numeric ID from `getUpdates`. |
| Function logs show `Unauthorized` | The token in the secret is wrong. Add the correct one as a new secret version and redeploy (`scripts/deploy-notify.sh`) so the function re-reads it. |
| Function logs show a Secret Manager permission error | `sa-notify-function` needs `roles/secretmanager.secretAccessor` on the secret and the secret needs a version. |
| Deploy of the function fails with "Permission denied while using the Eventarc Service Agent" | First Eventarc use in the project; permissions take a few minutes to propagate. Wait and re-run `scripts/deploy-notify.sh`. |
| Function logs / Cloud Run show 403 "request was not authenticated" | `sa-notify-function` lacks `roles/run.invoker` on `smm-notify-reviewer` (`deploy-notify.sh` grants it; re-run it). |
| Function never triggers | Confirm the trigger exists: `gcloud eventarc triggers list --location=us-central1`. Then confirm `eventarc.googleapis.com` is enabled and the function's service account has `roles/eventarc.eventReceiver`. |
| First render after idle is slow | Expected only if `min-instances` was lowered; the script keeps 1 warm instance. |

## Cost notes

- `smm-render-service` keeps one 2-CPU / 2 GiB instance running all the time (`min-instances=1`, as the spec asks).
  It's the biggest fixed cost here. Setting it to 0 saves that, at the price of a slow first request.
- `smm-telegram-intake` and `smm-brief-intake` use instance-based billing (`--no-cpu-throttling`); they scale to zero when idle, so they are only billed while running.
- The agent has `min_instances: 0`; Gemini and Cloud Build usage is billed per use.
- Rendered PNGs are deleted after 90 days by the bucket lifecycle rule.

## Where this differs from the specs

- **Invalid briefs get a 400 and no job.** The build spec says both "return 400 on invalid payload" (Service A) and "a
  deliberately broken brief (missing platforms, bad URL) ends in `failed` and still produces a Telegram notification"
  (acceptance criteria). They conflict for structurally invalid input. I followed Service A: structural problems get a 400;
  problems found later (unreachable image, model or render errors) become `failed` + Telegram. If you'd rather have every
  bad brief create a `failed` job and ping Telegram, that is a small change in `brief-intake/main.py`.
- `smm-telegram-intake` is public (not in the spec): Telegram cannot present a Google identity token. See step 5 for how it is locked down.
- `--allow-unauthenticated=false` in the spec is written `--no-allow-unauthenticated` in gcloud.
- Added to the spec's commands: `--execution-environment gen2` (render), `--no-cpu-throttling` (intake),
  `--source`, `--entry-point` and the path pattern (notify), `ASSETS_BUCKET` on the render service.
- `gcloud storage` replaces `gsutil` in `provision.sh` (same result).
- The 90-day lifecycle rule only applies to objects under `renders/`, not the whole bucket, so the brand files stored in it are not deleted.
- `brand` and `tone` may be left out of a brief when a brand context has been saved (step 5); intake fills them in. Without saved defaults they are required, as in the spec.
- The agent is deployed with the `adk deploy agent_engine` CLI, not a hand-written `agent_engines.create(...)` call.
  This ADK version (2.9.2) deploys a container built from your agent folder, and the CLI is the documented path.
- Copy limits apply to the post `body` only (not headline or hashtags). If the model overshoots, the body is cut at a
  word boundary and ends with "…".

## What has and hasn't been tested

Tested locally (90 automated tests): the render service in real Chromium (all templates at exact pixel sizes,
bad-image and injection cases), the agent pipeline end to end through ADK's runner with a fake Gemini, a fake render
service and a fake Firestore, brief validation, and the notify function against real Firestore event protobufs
with a fake Telegram API. Shell scripts are syntax-checked.

Verified live on the author's project (steps 0 to 3): a brief posted to `brief-intake` returned a `job_id` with a 202,
and about 50 seconds later the job was `ready_for_review` with 5 drafts and 5 PNGs at the exact platform sizes. A brief
with an unreachable background image ended in `failed` with a readable per-platform error.

Step 5 (brand context) was verified live with made-up brand documents: a voice guide PDF was transcribed, a visual-guidelines PDF produced the right colours, font and rule lists, and a brief with no `brand` and no `tone` came back `ready_for_review` with drafts that followed the voice guide (no exclamation marks, British spelling, the required sign-off) and images in the saved colours and font. It needed one extra permission that `provision.sh` now grants: `roles/serviceusage.serviceUsageConsumer` on `sa-agent-orchestrator`, because Agent Engine bills Cloud Storage reads to the project (without it the job fails with "serviceusage.services.use" when a screenshot is read).

Step 4 was verified live too: a finished job produced a Telegram message with one reply per draft and a photo album of the images, and a `failed` job produced an error message. Two first-run issues showed up and are handled in the scripts and the table above: the Eventarc service agent can take a few minutes after first use (the deploy is refused with "Permission denied while using the Eventarc Service Agent"; just re-run it), and the function needs `run.invoker` for `sa-notify-function`, which `deploy-notify.sh` now grants. Eventarc redelivers events it could not deliver, so events that failed with 403 before the binding existed can arrive as duplicate messages.
