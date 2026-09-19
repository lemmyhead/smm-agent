"""notify-function: on jobs/{id} writes, post to Telegram when status becomes ready_for_review or failed."""
import html
import json
import logging
import os
from functools import lru_cache

import functions_framework
import google.auth
import requests
from google.cloud import firestore, secretmanager, storage
from google.events.cloud import firestore as firestore_events

log = logging.getLogger("notify")
logging.basicConfig(level=logging.INFO)

NOTIFY_STATES = {"ready_for_review", "failed"}
SECRET_NAME = "telegram-bot-token"
MESSAGE_LIMIT = 4000  # Telegram text max is 4096
CAPTION_LIMIT = 1000  # Telegram caption max is 1024
ALBUM_LIMIT = 10  # sendMediaGroup takes 2-10 items


# ---------- pure helpers (unit-tested) ----------

def should_notify(old_status: str | None, new_status: str | None) -> bool:
    return new_status in NOTIFY_STATES and new_status != old_status


def esc(text: str) -> str:
    """Telegram HTML parse mode: escape &, < and >."""
    return html.escape(text, quote=False)


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_messages(job_id: str, job: dict, console_url: str = "") -> list[str]:
    """Returns HTML messages: a header, then one message per draft. Each is under Telegram's 4096-char limit."""
    brief = job.get("brief") or {}
    name = esc(brief.get("campaign_name") or "(unknown campaign)")
    link = f'\n<a href="{esc(console_url)}">Open job in Firestore</a>' if console_url else ""

    if job.get("status") == "failed":
        error = _fit(esc(job.get("error") or "no error message recorded"), 2500)
        return [f"❌ <b>Drafting failed: {name}</b>\n<pre>{error}</pre>\nJob <code>{esc(job_id)}</code>{link}"]

    messages = [f"✅ <b>Ready for review: {name}</b>\nJob <code>{esc(job_id)}</code>{link}"]
    for d in job.get("drafts") or []:
        tags = " ".join(f"#{h}" for h in d.get("hashtags") or [])
        tail = f"\n<i>{esc(tags)}</i>" if tags else ""
        head = f"<b>{esc(d['platform'])}</b>\n<b>{esc(d.get('headline', ''))}</b>\n"
        body = esc(d["body"]) if d.get("body") else "<i>(no caption for this format)</i>"
        room = MESSAGE_LIMIT - len(head) - len(tail)
        messages.append(head + (body if len(body) <= room else _fit(body, room)) + tail)
    return messages


# ---------- GCP / Telegram I/O (patched in tests) ----------

@lru_cache(maxsize=1)
def _project() -> str:
    return google.auth.default()[1]


@lru_cache(maxsize=1)
def _token() -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{_project()}/secrets/{SECRET_NAME}/versions/latest"
    return client.access_secret_version(name=name).payload.data.decode("utf-8").strip()


def _telegram(method: str, data: dict, files: dict | None = None) -> dict:
    """Calls the Bot API. Raises with Telegram's own error text (e.g. 'chat not found') on failure."""
    resp = requests.post(f"https://api.telegram.org/bot{_token()}/{method}", data=data, files=files, timeout=60)
    body = resp.json() if resp.content else {}
    if not body.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {body.get('description') or resp.status_code}")
    return body["result"]


def _read_job(job_id: str) -> dict:
    snap = firestore.Client().collection("jobs").document(job_id).get()
    if not snap.exists:
        raise RuntimeError(f"jobs/{job_id} no longer exists")
    return snap.to_dict()


def _download(gcs_path: str) -> bytes:
    bucket, _, name = gcs_path.removeprefix("gs://").partition("/")
    return storage.Client().bucket(bucket).blob(name).download_as_bytes()


def _send(chat_id: str, text: str, reply_to: int | None = None) -> dict:
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": json.dumps({"is_disabled": True})}
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to})
    return _telegram("sendMessage", data)


def _send_images(chat_id: str, images: list[dict], reply_to: int) -> None:
    """Sends the renders as photo albums (up to 10 per album). Each photo is captioned with platform and size."""
    for i in range(0, len(images), ALBUM_LIMIT):
        batch = images[i:i + ALBUM_LIMIT]
        media, files = [], {}
        for n, img in enumerate(batch):
            files[f"f{n}"] = (f"{img['platform']}.png", _download(img["gcs_path"]), "image/png")
            caption = f"{img['platform']} ({img['width']}×{img['height']})"
            media.append({"type": "photo", "media": f"attach://f{n}", "caption": _fit(caption, CAPTION_LIMIT)})
        data = {"chat_id": chat_id, "media": json.dumps(media), "reply_parameters": json.dumps({"message_id": reply_to})}
        if len(batch) == 1:  # albums need 2+ items
            single = {"chat_id": chat_id, "caption": media[0]["caption"], "reply_parameters": data["reply_parameters"]}
            _telegram("sendPhoto", single, {"photo": files["f0"]})
        else:
            _telegram("sendMediaGroup", data, files)


def post_job(job_id: str, job: dict) -> None:
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    console = (f"https://console.cloud.google.com/firestore/databases/-default-/data/panel/jobs/{job_id}"
               f"?project={_project()}")
    first, *rest = build_messages(job_id, job, console)
    reply_to = _send(chat_id, first)["message_id"]
    for text in rest:
        _send(chat_id, text, reply_to)

    images = job.get("images") or []
    if job.get("status") != "ready_for_review" or not images:
        return
    try:
        _send_images(chat_id, images, reply_to)
    except Exception:
        log.exception("could not send images")
        _send(chat_id, "⚠️ Some images could not be attached: " + ", ".join(f"<code>{esc(i['platform'])}</code>" for i in images), reply_to)


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
