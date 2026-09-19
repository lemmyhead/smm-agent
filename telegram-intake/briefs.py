"""Turns a chat message into brief fields, and submits the finished brief to brief-intake."""
import json
import os
from pathlib import Path

import requests
import yaml
from pydantic import BaseModel

import brandcore as core

_HERE = Path(__file__).parent
_SPECS = next((p for p in (_HERE / "shared" / "platform_specs.yaml", _HERE.parent / "shared" / "platform_specs.yaml")
               if p.exists()), _HERE / "shared" / "platform_specs.yaml")  # deployed copy, else the repo's own
PLATFORMS: list[str] = list(yaml.safe_load(Path(os.environ.get("PLATFORM_SPECS", _SPECS)).read_text()))
FIELDS = ("campaign_name", "key_message", "cta", "link", "tone", "platforms")
LABELS = {"campaign_name": "Campaign", "key_message": "Key message", "cta": "Call to action", "link": "Link",
          "tone": "Tone", "platforms": "Platforms"}

PROMPT = """You fill in a social-media campaign brief from what the user writes in chat.
Current draft (JSON): {current}
Allowed platform names: {platforms}
(facebook = Facebook post, instagram_feed = Instagram feed post, instagram_story = Instagram story,
threads = Threads, youtube_community = YouTube community post.)

Rules:
- Return only the fields the user's message states or changes; use null for everything else. Never invent a value.
- Keep the user's own wording and language. Do not translate or embellish.
- campaign_name is a short label (max 200 characters). cta is a short call to action such as "Learn more"
  (max 80 characters). key_message is what the posts must say (max 2000 characters). link must be a full URL.
- tone: only if the user describes one (e.g. "calm, no jargon"); otherwise null.
- platforms: map what the user names to the allowed platform names; "all platforms" means every allowed name.

User message:
{text}"""


class BriefFields(BaseModel):
    campaign_name: str | None = None
    key_message: str | None = None
    cta: str | None = None
    link: str | None = None
    tone: str | None = None
    platforms: list[str] | None = None


def parse(ai, text: str, current: dict) -> dict:
    """Merges what `text` says into the draft. Fields the message does not mention are left alone."""
    from google.genai import types
    prompt = PROMPT.format(current=json.dumps(current, ensure_ascii=False), platforms=", ".join(PLATFORMS), text=text)
    cfg = types.GenerateContentConfig(temperature=0, response_mime_type="application/json", response_schema=BriefFields)
    got = BriefFields.model_validate_json(ai.client.models.generate_content(model=ai.model, contents=prompt, config=cfg).text)
    out = dict(current)
    for key, value in got.model_dump().items():
        if value in (None, "", []):
            continue
        out[key] = value.strip() if isinstance(value, str) else [p for p in value if p in PLATFORMS]
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def missing(draft: dict, saved: dict) -> list[str]:
    """What must still be provided before the brief can be sent, given the saved brand context."""
    need = [LABELS[k] for k in ("campaign_name", "key_message", "cta", "platforms") if not draft.get(k)]
    if not draft.get("background_image_url"):
        need.append("Background image (send it as a photo, or as a file for full quality)")
    if not draft.get("tone") and not (saved.get("voice") or {}).get("text"):
        need.append("Tone (or save a voice guide with /voice)")
    brand = (saved.get("visual") or {}).get("brand") or {}
    if not all(brand.get(k) for k in ("logo_url", "primary_color", "accent_color", "font")):
        need.append("Brand colours, font and logo (send /guidelines and /logo)")
    return need


def format_draft(draft: dict, saved: dict) -> str:
    voice = (saved.get("voice") or {}).get("text")
    lines = ["Draft brief"]
    for key in FIELDS:
        value = draft.get(key)
        if key == "platforms" and value:
            value = ", ".join(value)
        if key == "tone" and not value and voice:
            value = "from the saved voice guide"
        lines.append(f"{LABELS[key]}: {value or '(not set)'}")
    lines.append(f"Background image: {'attached' if draft.get('background_image_url') else '(not set)'}")
    need = missing(draft, saved)
    if need:
        lines.append("\nStill needed:\n- " + "\n- ".join(need))
        lines.append("\nSend the details as a message (any language) or the image. /cancel discards the draft.")
    else:
        lines.append("\nEverything is there. /send to start drafting, or tell me what to change.")
    return "\n".join(lines)


def submit(draft: dict) -> str:
    """POSTs the brief to brief-intake as this service's own identity. Returns the job id."""
    import google.auth.transport.requests
    from google.oauth2 import id_token
    base = os.environ["BRIEF_INTAKE_URL"].rstrip("/")
    token = id_token.fetch_id_token(google.auth.transport.requests.Request(), base)
    body = {k: draft[k] for k in (*FIELDS, "background_image_url") if draft.get(k)}
    r = requests.post(f"{base}/briefs", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 400:
        details = "; ".join(f"{d['field']}: {d['message']}" for d in r.json().get("details", []))
        raise core.BrandError(f"the brief was not accepted: {details}")
    r.raise_for_status()
    return r.json()["job_id"]
