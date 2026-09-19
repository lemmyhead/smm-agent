"""telegram-intake: Telegram webhook. Chat with the bot to manage the brand context (voice guide, example posts,
visual guidelines, logo) and to submit campaign briefs. Only chats listed in TELEGRAM_ALLOWED_CHAT_IDS are answered, and every request must carry
Telegram's secret-token header, because this Cloud Run service has to be publicly reachable."""
import hmac
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import requests
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

import brandcore as core
import briefs

log = logging.getLogger("telegram-intake")
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="smm-telegram-intake", docs_url=None, redoc_url=None, openapi_url=None)

API = "https://api.telegram.org"
MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API limit for getFile
MESSAGE_LIMIT = 4000  # Telegram text max is 4096

HELP = """I keep your brand context so briefs only need the campaign details.

/brief: start a campaign brief. Describe it in a message, send the background image, then /send. The drafts and images arrive here when ready

/voice: send the tone-of-voice guide (PDF, .md, .txt, .rtf, or paste the text)
/examples: send example posts (text messages, a .txt file with posts separated by ---, or screenshots), then /done. Use "/examples add" to keep the ones already saved
/guidelines: send the visual-guidelines PDF; I show what I found so you can correct it, then /save
/logo: send the logo as a file (a photo works too)
/show: what is saved ("/show full" for everything)
/clear: delete everything saved
/cancel: forget the current step or brief draft"""


# ---------- configuration and Telegram I/O (patched in tests) ----------

def allowed_chats() -> set[str]:
    return {c.strip() for c in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()}


def _token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def chunks(text: str, size: int = MESSAGE_LIMIT) -> list[str]:
    """Splits on line breaks where possible so a long message stays readable."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return [*out, cur] if cur or not out else out


def tg_send(chat_id, text: str) -> None:
    for part in chunks(text):
        r = requests.post(f"{API}/bot{_token()}/sendMessage", json={"chat_id": chat_id, "text": part}, timeout=30)
        if not r.ok:
            log.error("sendMessage failed: %s", r.text[:300])


def tg_download(file_id: str) -> bytes:
    r = requests.get(f"{API}/bot{_token()}/getFile", params={"file_id": file_id}, timeout=30).json()
    if not r.get("ok"):
        raise core.BrandError(f"Telegram would not give me that file: {r.get('description')}. "
                              "Bots can only download files up to 20 MB.")
    data = requests.get(f"{API}/file/bot{_token()}/{r['result']['file_path']}", timeout=60)
    data.raise_for_status()
    return data.content


@lru_cache(maxsize=1)
def _db() -> firestore.Client:
    return firestore.Client(project=os.environ["PROJECT_ID"])


@lru_cache(maxsize=1)
def store() -> core.Store:
    return core.Store(os.environ["PROJECT_ID"], os.environ.get("BRAND_PROFILE", "default"))


@lru_cache(maxsize=1)
def ai() -> core.Gemini:
    return core.Gemini(os.environ["PROJECT_ID"])


def claim_update(update_id: int) -> bool:
    """True the first time an update id is seen. Telegram redelivers when it does not get a quick 200."""
    try:
        _db().collection("telegram_updates").document(str(update_id)).create({
            "at": firestore.SERVER_TIMESTAMP, "expires_at": datetime.now(timezone.utc) + timedelta(days=2)})
        return True
    except AlreadyExists:
        return False


def session_get(chat_id) -> dict:
    snap = _db().collection("telegram_sessions").document(str(chat_id)).get()
    return snap.to_dict() or {} if snap.exists else {}


def session_put(chat_id, **fields) -> None:
    data = session_get(chat_id)
    data.update(fields)
    _db().collection("telegram_sessions").document(str(chat_id)).set(data)


# ---------- conversation ----------

def counts(examples: list[dict]) -> str:
    images = sum(1 for i in examples if i["type"] == "image")
    return f"{len(examples) - images} posts and {images} screenshots"


def command(chat_id, text: str) -> None:
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    session = session_get(chat_id)

    if cmd in ("/start", "/help"):
        tg_send(chat_id, HELP)
    elif cmd == "/brief":
        if arg.lower() == "new" or "brief" not in session:
            session_put(chat_id, brief={})
        session_put(chat_id, mode="brief")
        draft = session_get(chat_id).get("brief") or {}
        if draft:
            tg_send(chat_id, briefs.format_draft(draft, store().get()))
        else:
            tg_send(chat_id, "Describe the campaign in a message, in any language: name, what the posts must say, "
                             "the call to action, the link, and which platforms. Then send the background image "
                             "(as a file for full quality). Colours, font, logo and tone come from your saved brand "
                             "context, so you don't need to repeat them.\n\n"
                             f"Platforms: {', '.join(briefs.PLATFORMS)}.")
    elif cmd == "/send":
        send_brief(chat_id, session.get("brief") or {})
    elif cmd == "/voice":
        session_put(chat_id, mode="voice")
        tg_send(chat_id, "Send the tone-of-voice guide as a PDF, .md, .txt or .rtf file, or paste it here as one message "
                         "(sending it again later replaces the saved one).")
    elif cmd == "/examples":
        if arg.lower() != "add":
            store().delete_prefix("examples/")
            store().merge({"examples": []}, ["examples"])
        session_put(chat_id, mode="examples")
        tg_send(chat_id, "Send the example posts now: paste each caption as its own message, or send a .txt file "
                         "(posts separated by a line of ---), or screenshots. Send /done when finished.")
    elif cmd == "/guidelines":
        session_put(chat_id, mode="guidelines")
        tg_send(chat_id, "Send the visual-guidelines PDF.")
    elif cmd == "/logo":
        session_put(chat_id, mode="logo")
        tg_send(chat_id, "Send the logo image (PNG, JPG, WebP or SVG). Sending it as a file keeps it sharp.")
    elif cmd == "/done":
        session_put(chat_id, mode=None)
        examples = store().get().get("examples") or []
        tg_send(chat_id, f"Done. Saved: {counts(examples)}." if session.get("mode") == "examples" else "Okay.")
    elif cmd == "/cancel":
        session_put(chat_id, mode=None, pending=None, brief=None)
        tg_send(chat_id, "Cancelled. Nothing further was saved.")
    elif cmd == "/set":
        pending = session.get("pending")
        if not pending:
            raise core.BrandError("Nothing to correct. Send /guidelines first.")
        key, _, value = arg.partition(" ")
        core.set_field(pending, key, value)
        session_put(chat_id, pending=pending)
        tg_send(chat_id, "Updated.\n\n" + core.format_proposal(pending) + "\n\n/save to keep this.")
    elif cmd == "/save":
        pending = session.get("pending")
        if not pending:
            raise core.BrandError("Nothing to save. Send /guidelines first.")
        source = pending.get("source_pdf") or ""
        has_logo = core.apply_proposal(store(), pending, source if source.startswith("gs://") else None)
        session_put(chat_id, mode=None, pending=None)
        tg_send(chat_id, "Saved. Briefs without a brand block now use these colours and font."
                         + ("" if has_logo else "\nNo logo yet: send /logo."))
    elif cmd == "/show":
        tg_send(chat_id, core.format_summary(store().get(), store().profile, full=arg.lower() == "full"))
    elif cmd == "/clear":
        if arg.lower() == "confirm":
            store().delete_profile()
            session_put(chat_id, mode=None, pending=None)
            tg_send(chat_id, "Deleted the saved brand context and its files.")
        else:
            tg_send(chat_id, "This deletes the saved voice guide, examples, brand colours, logo and files. "
                             "Send /clear confirm to go ahead.")
    else:
        tg_send(chat_id, "I don't know that command. /help lists what I can do.")


def update_brief(chat_id, text: str | None = None, **fields) -> None:
    draft = session_get(chat_id).get("brief") or {}
    if text:
        draft = briefs.parse(ai(), text, draft)
    draft.update(fields)
    session_put(chat_id, brief=draft)
    tg_send(chat_id, briefs.format_draft(draft, store().get()))


def send_brief(chat_id, draft: dict) -> None:
    if not draft:
        raise core.BrandError("There is no brief in progress. Send /brief to start one.")
    need = briefs.missing(draft, store().get())
    if need:
        raise core.BrandError("The brief is not complete yet. Still needed: " + "; ".join(need) + ".")
    job_id = briefs.submit(draft)
    session_put(chat_id, mode=None, brief=None)
    tg_send(chat_id, f"Started job {job_id[:8]}. The drafts and images will arrive in this chat in a few minutes.")


def on_text(chat_id, mode: str | None, text: str) -> None:
    if mode == "brief":
        update_brief(chat_id, text)
    elif mode == "voice":
        saved = core.save_voice_text(store(), "voice.txt", text)
        session_put(chat_id, mode=None)
        tg_send(chat_id, f"Saved the voice guide ({len(saved):,} characters). /show full to read it back.")
    elif mode == "examples":
        add_examples(chat_id, core.text_example_items(text))
    else:
        tg_send(chat_id, "Send /help to see what I can do.")


def add_examples(chat_id, items: list[dict]) -> None:
    merged = store().append_examples(items, core.check_examples)  # atomic; raises BrandError over the limits
    tg_send(chat_id, f"Saved. {counts(merged)} so far. Send more or /done.")


def on_file(chat_id, mode: str | None, name: str, fetch) -> None:
    if mode is None:
        tg_send(chat_id, "I got a file but don't know what it is for. Send /brief, /voice, /examples, /guidelines "
                         "or /logo first, then the file.")
        return
    if mode == "brief":
        ext = core.ext_of(name)
        if ext not in core.IMAGE_EXT:
            raise core.BrandError("the background must be a .png, .jpg or .webp image")
        data = fetch()
        core.check_size(name, data, core.MAX_BACKGROUND_BYTES)
        uri = store().upload_path(f"briefs/{chat_id}/{uuid.uuid4().hex[:8]}-{core.clean_name(name)}", data,
                                  core.content_type(name, "image/jpeg"))
        update_brief(chat_id, background_image_url=uri)
    elif mode == "voice":
        if core.ext_of(name) == ".pdf":
            tg_send(chat_id, "Reading the PDF, this takes a few seconds ...")
        text = core.save_voice(store(), ai, name, fetch())
        session_put(chat_id, mode=None)
        tg_send(chat_id, f"Saved the voice guide ({len(text):,} characters). Here is how it starts, so you can check it "
                         f"was read correctly:\n\n{text[:600]}{' [...]' if len(text) > 600 else ''}\n\n/show full to read all of it.")
    elif mode == "examples":
        add_examples(chat_id, core.example_items(store(), name, fetch()))
    elif mode == "guidelines":
        tg_send(chat_id, "Reading the guidelines, this takes about half a minute ...")
        data = fetch()
        if core.ext_of(name) != ".pdf":
            raise core.BrandError("the visual guidelines must be a PDF")
        source = store().upload(f"guidelines/{core.clean_name(name)}", data, "application/pdf")
        proposal = core.extract_proposal(ai, name, data, source)
        session_put(chat_id, mode=None, pending=proposal)
        tg_send(chat_id, "Here is what I found. Nothing is saved yet.\n\n" + core.format_proposal(proposal)
                + "\n\nIf a colour or the font is wrong, fix it with:\n/set primary #1f4d3a\n/set accent #f2b134\n/set font Lora"
                  "\nThen send /save (or /cancel to discard).")
    elif mode == "logo":
        uri = core.save_logo(store(), name, fetch())
        session_put(chat_id, mode=None)
        tg_send(chat_id, f"Logo saved as the default: {uri}")


def handle(chat_id, msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        return command(chat_id, text)
    mode = session_get(chat_id).get("mode")
    if doc := msg.get("document"):
        if (doc.get("file_size") or 0) > MAX_DOWNLOAD:
            raise core.BrandError("that file is over 20 MB, which is the most a bot can download")
        return on_file(chat_id, mode, doc.get("file_name") or "file", lambda: tg_download(doc["file_id"]))
    if photos := msg.get("photo"):
        best = photos[-1]  # Telegram lists sizes ascending
        return on_file(chat_id, mode, f"photo_{best['file_unique_id']}.jpg", lambda: tg_download(best["file_id"]))
    if text:
        return on_text(chat_id, mode, text)
    tg_send(chat_id, "I can read text, files and photos. /help lists what I can do.")


def process(update: dict) -> None:
    if not claim_update(update["update_id"]):
        return
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    try:
        handle(chat_id, msg)
    except core.BrandError as e:
        tg_send(chat_id, f"⚠️ {e}")
    except Exception as e:
        log.exception("update %s failed", update.get("update_id"))
        tg_send(chat_id, f"⚠️ Something went wrong ({type(e).__name__}: {str(e)[:200]}). Nothing was lost; try again, "
                         "or check the service logs.")


# ---------- webhook ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/telegram")
def webhook(update: dict, background: BackgroundTasks,
            x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "webhook secret is not configured")
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", secret):
        raise HTTPException(401, "bad secret")
    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id is None or str(chat_id) not in allowed_chats():
        return {"ok": True}  # not on the allow-list: say nothing, do nothing
    background.add_task(process, update)  # answer Telegram at once; Gemini work continues after the response
    return {"ok": True}
