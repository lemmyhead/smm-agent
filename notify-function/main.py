"""notify-function: on jobs/{id} writes, post to Slack when status becomes ready_for_review or failed."""
import logging
import os
from functools import lru_cache

import functions_framework
import google.auth
from google.cloud import firestore, secretmanager, storage
from google.events.cloud import firestore as firestore_events
from slack_sdk import WebClient

log = logging.getLogger("notify")
logging.basicConfig(level=logging.INFO)

NOTIFY_STATES = {"ready_for_review", "failed"}
SECRET_NAME = "slack-bot-token"
SECTION_LIMIT = 2900  # Slack section text max is 3000


# ---------- pure helpers (unit-tested) ----------

def should_notify(old_status: str | None, new_status: str | None) -> bool:
    return new_status in NOTIFY_STATES and new_status != old_status


def esc(text: str) -> str:
    """Slack mrkdwn control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chunks(text: str, size: int = SECTION_LIMIT) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def build_message(job_id: str, job: dict, console_url: str = "") -> tuple[str, list[dict]]:
    """Returns (fallback_text, blocks)."""
    brief = job.get("brief") or {}
    name = brief.get("campaign_name") or "(unknown campaign)"
    link = f"\n<{console_url}|Open job in Firestore>" if console_url else ""

    if job.get("status") == "failed":
        text = f":x: Drafting failed: {name}"
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text":
                f"*Error*\n```{esc(job.get('error') or 'no error message recorded')[:2500]}```\nJob `{job_id}`{link}"}},
        ]
        return text, blocks

    text = f":white_check_mark: Ready for review: {name}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Job `{job_id}`"}]},
    ]
    for d in job.get("drafts") or []:
        tags = " ".join(f"#{h}" for h in d.get("hashtags") or [])
        body = d.get("body") or "_(no caption for this format)_"
        md = f"*{esc(d['platform'])}*\n*{esc(d.get('headline', ''))}*\n{esc(body) if d.get('body') else body}"
        if tags:
            md += f"\n_{esc(tags)}_"
        blocks.append({"type": "divider"})
        blocks.extend({"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in _chunks(md))
    if link:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": link.strip()}]})
    return text, blocks[:50]  # Slack allows 50 blocks per message


# ---------- GCP / Slack I/O (patched in tests) ----------

@lru_cache(maxsize=1)
def _project() -> str:
    return google.auth.default()[1]


@lru_cache(maxsize=1)
def _slack() -> WebClient:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{_project()}/secrets/{SECRET_NAME}/versions/latest"
    token = client.access_secret_version(name=name).payload.data.decode("utf-8").strip()
    return WebClient(token=token)


def _read_job(job_id: str) -> dict:
    snap = firestore.Client().collection("jobs").document(job_id).get()
    if not snap.exists:
        raise RuntimeError(f"jobs/{job_id} no longer exists")
    return snap.to_dict()


def _download(gcs_path: str) -> bytes:
    bucket, _, name = gcs_path.removeprefix("gs://").partition("/")
    return storage.Client().bucket(bucket).blob(name).download_as_bytes()


def post_job(job_id: str, job: dict) -> None:
    channel = os.environ["SLACK_CHANNEL_ID"]
    slack = _slack()
    console = (f"https://console.cloud.google.com/firestore/databases/-default-/data/panel/jobs/{job_id}"
               f"?project={_project()}")
    text, blocks = build_message(job_id, job, console)
    resp = slack.chat_postMessage(channel=channel, text=text, blocks=blocks)
    thread_ts = resp["ts"]

    images = job.get("images") or []
    if job.get("status") != "ready_for_review" or not images:
        return
    uploads, failed = [], []
    for img in images:
        try:
            uploads.append({"content": _download(img["gcs_path"]),
                            "filename": f"{img['platform']}.png",
                            "title": f"{img['platform']} ({img['width']}×{img['height']})"})
        except Exception:
            log.exception("could not read %s", img.get("gcs_path"))
            failed.append(img.get("gcs_path", "?"))
    if uploads:
        try:
            slack.files_upload_v2(channel=channel, thread_ts=thread_ts, file_uploads=uploads)
        except Exception:
            log.exception("slack file upload failed")
            failed.extend(u["filename"] for u in uploads)
    if failed:
        slack.chat_postMessage(channel=channel, thread_ts=thread_ts, text=(
            ":warning: Some images could not be attached: " + ", ".join(f"`{esc(f)}`" for f in failed)))


# ---------- entrypoint ----------

@functions_framework.cloud_event
def notify_reviewer(cloud_event):
    event = firestore_events.DocumentEventData()
    event._pb.ParseFromString(cloud_event.data)

    old = event.old_value.fields.get("status")
    new = event.value.fields.get("status")
    old_status = old.string_value if old else None
    new_status = new.string_value if new else None
    if not should_notify(old_status, new_status):
        return  # a field other than the status changed: stay quiet

    job_id = event.value.name.rsplit("/", 1)[-1]
    log.info("job %s: %s -> %s", job_id, old_status, new_status)
    post_job(job_id, _read_job(job_id))
