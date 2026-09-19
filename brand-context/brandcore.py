"""Brand context core, shared by the brandctx CLI and the telegram-intake service (sync-shared.sh copies this file
into telegram-intake/). Everything works on (file name, bytes) so it does not care where a file came from.

Storage: Firestore `brand/<profile>` and the raw files under gs://smm-agent-assets-<PROJECT>/brand/<profile>/."""
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pydantic import BaseModel

COLLECTION = "brand"
MAX_VOICE_CHARS = 30_000
MAX_EXAMPLE_CHARS = 30_000
MAX_EXAMPLE_IMAGES = 12
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
TEXT_EXT = {".txt", ".md", ".markdown", ".rtf"}
MAX_POST_CHARS = 5_000  # an Instagram caption is at most 2,200; more than this is a whole document, not one post
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
SAFE_FONT = re.compile(r"^[A-Za-z0-9 \-]{1,60}$")  # same rule the render service applies


class BrandError(Exception):
    """A problem the person should read and fix (bad file type, too long, ...)."""


# ---------- what Gemini returns for the visual-guidelines PDF ----------

class Extraction(BaseModel):
    primary_color: str
    accent_color: str
    font: str
    font_notes: str
    palette: list[str]  # "Name #hex: usage"
    logo_rules: list[str]
    layout_rules: list[str]
    imagery_rules: list[str]
    copy_rules: list[str]


VISUAL_PROMPT = """You are reading a brand visual-guidelines PDF. Report only what the document actually states; never invent.
- primary_color: the brand's main colour as a #hex value. If the document only gives CMYK/RGB/Pantone, convert to the closest #hex.
- accent_color: the main highlight / secondary colour as a #hex value (the colour used for buttons and call-outs if stated).
- font: the primary typeface name exactly as the document names it (just the family, e.g. "Inter"). Empty string if none is given.
- font_notes: how fonts are meant to be used (headline vs body fonts, weights), one or two sentences. Empty string if not stated.
- palette: every named brand colour as "Name #hex: intended use".
- logo_rules: rules about the logo (placement, clear space, minimum size, backgrounds it may sit on, forbidden uses).
- layout_rules: rules about layout and composition (text position, margins, grid, hierarchy, overlays, alignment).
- imagery_rules: rules about photography and imagery.
- copy_rules: any rules about writing or wording that appear in the document (capitalisation, punctuation, words to avoid).
Each rule is one short sentence in your own words. Use empty lists when the document has nothing on a topic."""

VOICE_PROMPT = """Transcribe the tone-of-voice guidance in this document as clean Markdown. Keep every rule, principle,
example phrase, do/don't list and vocabulary list exactly as written. Leave out page numbers, headers, footers and
decorative text. Do not summarise, reorder or add anything."""


# ---------- pure helpers (unit-tested) ----------

def split_examples(text: str) -> list[str]:
    """Posts in one text are separated by a line containing only ---."""
    return [p.strip() for p in re.split(r"(?m)^\s*---+\s*$", text) if p.strip()]


def hex_ok(value: str) -> bool:
    return bool(HEX.match(value or ""))


def clean_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "file"


def ext_of(name: str) -> str:
    return Path(name).suffix.lower()


def content_type(name: str, default: str) -> str:
    return mimetypes.guess_type(name)[0] or default


def check_size(name: str, data: bytes, limit: int) -> None:
    if len(data) > limit:
        raise BrandError(f"{name} is larger than {limit // (1024 * 1024)} MB")


def read_text(data: bytes) -> str:
    """Decodes a text file. Rich-text (RTF) files, e.g. saved from TextEdit, are converted to plain text; this is
    detected by content, because such a file is often saved with a .txt extension."""
    text = data.decode("utf-8", errors="replace")
    if text.lstrip().startswith("{\\rtf"):
        from striprtf.striprtf import rtf_to_text
        text = rtf_to_text(text)
    return text.strip()


def build_proposal(ex: Extraction, font_status: bool | None, source: str) -> dict:
    manual: dict = {"logo_rules": ex.logo_rules, "layout_rules": ex.layout_rules, "imagery_rules": ex.imagery_rules}
    if ex.font and font_status is False:
        manual["font"] = (f"'{ex.font}' is not on Google Fonts, so the render service falls back to Helvetica Neue/Arial. "
                          "Templates need the font file added by hand.")
    return {
        "source_pdf": source,
        "brand": {"primary_color": ex.primary_color.strip(), "accent_color": ex.accent_color.strip(),
                  "font": ex.font.strip()},
        "font_check": {True: "available on Google Fonts", False: "NOT on Google Fonts",
                       None: "could not be checked"}[font_status],
        "font_notes": ex.font_notes,
        "palette": ex.palette,
        "copy_rules": ex.copy_rules,
        "manual_template_work": manual,
    }


def validate_proposal(p: dict) -> list[str]:
    problems = []
    brand = p.get("brand") or {}
    for key in ("primary_color", "accent_color"):
        if not hex_ok(brand.get(key, "")):
            problems.append(f"brand.{key} must be a #hex colour like #1a2b3c, got {brand.get(key)!r}")
    font = brand.get("font", "")
    if font and not SAFE_FONT.match(font):
        problems.append(f"brand.font {font!r} may only contain letters, digits, spaces and hyphens (max 60)")
    if not font:
        problems.append("brand.font is empty; give the font family name (e.g. Inter)")
    return problems


def set_field(proposal: dict, key: str, value: str) -> None:
    """Applies a correction to a proposal: key is primary | accent | font."""
    field = {"primary": "primary_color", "accent": "accent_color", "font": "font"}.get(key.lower())
    if not field:
        raise BrandError("use: primary #hex, accent #hex or font Name")
    candidate = {**proposal, "brand": {**proposal["brand"], field: value.strip()}}
    problems = [x for x in validate_proposal(candidate) if field in x]
    if problems:
        raise BrandError(problems[0])  # rejected: the proposal is left untouched
    proposal["brand"][field] = value.strip()


def format_proposal(p: dict) -> str:
    b = p["brand"]
    out = ["DEFAULT BRAND BLOCK (used when a brief has no brand)",
           f"  primary_color  {b['primary_color']}", f"  accent_color   {b['accent_color']}",
           f"  font           {b['font']}" + (f"   [{p['font_check']}]" if p.get("font_check") else "")]
    if b.get("logo_url"):
        out.append(f"  logo_url       {b['logo_url']}")
    for label, key in (("PALETTE", "palette"), ("WRITING RULES (added to the drafter's prompt)", "copy_rules")):
        if p.get(key):
            out += ["", label] + [f"  - {x}" for x in p[key]]
    manual = p.get("manual_template_work") or {}
    if any(manual.values()):
        out += ["", "NEEDS MANUAL TEMPLATE WORK (cannot be applied automatically)"]
        for key, val in manual.items():
            out += [f"  - [{key}] {x}" for x in ([val] if isinstance(val, str) else val)]
    return "\n".join(out)


def format_summary(doc: dict, profile: str, full: bool = False) -> str:
    if not doc:
        return f"No brand context saved for profile {profile!r} yet."
    voice = (doc.get("voice") or {}).get("text", "")
    out = [f"VOICE GUIDE: {'%d characters' % len(voice) if voice else 'not set'}"]
    if voice:
        out.append(voice if full else voice[:400] + ("\n[...]" if len(voice) > 400 else ""))
    ex = doc.get("examples") or []
    out.append(f"\nEXAMPLES: {sum(1 for i in ex if i['type'] == 'text')} posts, "
               f"{sum(1 for i in ex if i['type'] == 'image')} screenshots")
    if full:
        out += [f"  - {i.get('text') or i.get('name')}" for i in ex]
    v = doc.get("visual")
    if v:
        out += ["", format_proposal({"brand": v["brand"], "palette": v.get("palette"), "copy_rules": v.get("copy_rules"),
                                     "manual_template_work": v.get("manual_template_work")})]
    else:
        out.append("\nBRAND BLOCK: not set")
    return "\n".join(out)


# ---------- I/O ----------

class Store:
    """Firestore doc brand/<profile> plus the raw files under brand/<profile>/ in the assets bucket."""

    def __init__(self, project: str, profile: str):
        from google.cloud import firestore, storage
        self.project, self.profile = project, profile
        self.bucket_name = f"smm-agent-assets-{project}"
        self._fs = firestore
        self._db = firestore.Client(project=project)
        self._gcs = storage.Client(project=project)
        self._doc = self._db.collection(COLLECTION).document(profile)

    def get(self) -> dict:
        snap = self._doc.get()
        return snap.to_dict() if snap.exists else {}

    def merge(self, fields: dict, paths: list[str]) -> None:
        """Replaces exactly the listed field paths, leaves the rest of the document alone."""
        self._doc.set({**fields, "updated_at": self._fs.SERVER_TIMESTAMP}, merge=[*paths, "updated_at"])

    def append_examples(self, items: list[dict], check) -> list[dict]:
        """Atomically appends to the saved examples after `check(all_items)` accepts the result, so several files
        arriving at once (a Telegram album, two documents) cannot overwrite each other or slip past the limits."""
        fs = self._fs

        @fs.transactional
        def run(txn):
            snap = self._doc.get(transaction=txn)
            merged = ((snap.to_dict() or {}).get("examples") or []) if snap.exists else []
            merged = merged + items
            check(merged)
            txn.set(self._doc, {"examples": merged, "updated_at": fs.SERVER_TIMESTAMP}, merge=True)
            return merged

        return run(self._db.transaction())

    def upload(self, name: str, data: bytes, ctype: str) -> str:
        path = f"brand/{self.profile}/{name}"
        self._gcs.bucket(self.bucket_name).blob(path).upload_from_string(data, content_type=ctype)
        return f"gs://{self.bucket_name}/{path}"

    def delete_prefix(self, prefix: str) -> int:
        blobs = list(self._gcs.bucket(self.bucket_name).list_blobs(prefix=f"brand/{self.profile}/{prefix}"))
        for b in blobs:
            b.delete()
        return len(blobs)

    def delete_profile(self) -> None:
        self.delete_prefix("")
        self._doc.delete()


class Gemini:
    def __init__(self, project: str):
        import logging

        from google import genai
        logging.getLogger("google_genai").setLevel(logging.ERROR)  # silences a harmless automatic-function-calling notice
        location = os.environ.get("BRAND_MODEL_LOCATION") or os.environ.get("DRAFT_MODEL_LOCATION") or "us-central1"
        self.client = genai.Client(vertexai=True, project=project, location=location)
        self.model = os.environ.get("BRAND_MODEL", "gemini-2.5-flash")

    def _pdf(self, pdf: bytes, prompt: str, config):
        from google.genai import types
        return self.client.models.generate_content(
            model=self.model,
            contents=[types.Part.from_bytes(data=pdf, mime_type="application/pdf"), prompt],
            config=config,
        )

    def transcribe(self, pdf: bytes) -> str:
        from google.genai import types
        return (self._pdf(pdf, VOICE_PROMPT, types.GenerateContentConfig(temperature=0)).text or "").strip()

    def extract_visual(self, pdf: bytes) -> Extraction:
        from google.genai import types
        cfg = types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                          response_schema=Extraction)
        return Extraction.model_validate_json(self._pdf(pdf, VISUAL_PROMPT, cfg).text)


def font_on_google_fonts(name: str) -> bool | None:
    if not name or not SAFE_FONT.match(name):
        return False if name else None
    url = f"https://fonts.googleapis.com/css2?family={urllib.parse.quote(name)}:wght@400;700"
    try:
        with urllib.request.urlopen(url, timeout=8):
            return True
    except urllib.error.HTTPError as e:
        return False if e.code in (400, 404) else None
    except Exception:
        return None


# ---------- operations (each takes a file name and its bytes) ----------

def save_voice(store, ai, name: str, data: bytes) -> str:
    """Stores the voice guide (text as-is, or a PDF transcribed to markdown). Returns the saved text."""
    ext = ext_of(name)
    check_size(name, data, MAX_PDF_BYTES)
    if ext in TEXT_EXT:
        text = read_text(data)
    elif ext == ".pdf":
        text = ai().transcribe(data)
    else:
        raise BrandError("the voice guide must be .md, .txt, .rtf or .pdf (export Word/Google docs to PDF first)")
    return save_voice_text(store, name, text, data)


def save_voice_text(store, name: str, text: str, raw: bytes | None = None) -> str:
    text = text.strip()
    if not text:
        raise BrandError("no text found")
    if len(text) > MAX_VOICE_CHARS:
        raise BrandError(f"the guide is {len(text):,} characters; the limit is {MAX_VOICE_CHARS:,}. "
                         "Trim it to the rules that matter.")
    source = store.upload(f"voice/{clean_name(name)}", raw if raw is not None else text.encode(),
                          content_type(name, "text/plain"))
    store.merge({"voice": {"text": text, "source": source}}, ["voice"])
    return text


def example_items(store, name: str, data: bytes) -> list[dict]:
    """Turns one file into example items: a text file gives one item per post, an image is uploaded."""
    ext = ext_of(name)
    if ext in TEXT_EXT:
        posts = split_examples(read_text(data))
        if not posts:
            raise BrandError(f"{name} has no text")
        return [{"type": "text", "text": t} for t in posts]
    if ext in IMAGE_EXT:
        check_size(name, data, MAX_IMAGE_BYTES)
        uri = store.upload(f"examples/{clean_name(name)}", data, content_type(name, "image/png"))
        return [{"type": "image", "gcs_path": uri, "name": name}]
    raise BrandError(f"{name}: examples must be .txt/.md/.rtf files or .png/.jpg/.webp screenshots")


def text_example_items(text: str) -> list[dict]:
    posts = split_examples(text)
    if not posts:
        raise BrandError("that message has no text")
    return [{"type": "text", "text": t} for t in posts]


def check_examples(items: list[dict]) -> None:
    for i in items:
        if len(i.get("text", "")) > MAX_POST_CHARS:
            raise BrandError(f"one example is {len(i['text']):,} characters, which is a whole document rather than a post "
                             f"(the limit per post is {MAX_POST_CHARS:,}). Put a line containing only --- between posts.")
    images = sum(1 for i in items if i["type"] == "image")
    chars = sum(len(i.get("text", "")) for i in items)
    if images > MAX_EXAMPLE_IMAGES:
        raise BrandError(f"{images} screenshots; the limit is {MAX_EXAMPLE_IMAGES}. Keep the most representative ones.")
    if chars > MAX_EXAMPLE_CHARS:
        raise BrandError(f"{chars:,} characters of example text; the limit is {MAX_EXAMPLE_CHARS:,}.")


def extract_proposal(ai, name: str, data: bytes, source: str) -> dict:
    if ext_of(name) != ".pdf":
        raise BrandError("the visual guidelines must be a PDF")
    check_size(name, data, MAX_PDF_BYTES)
    ex = ai().extract_visual(data)
    return build_proposal(ex, font_on_google_fonts(ex.font.strip()), source)


def apply_proposal(store, p: dict, source_uri: str | None = None) -> bool:
    """Saves a reviewed proposal as the default brand block. Returns True if a logo is set."""
    problems = validate_proposal(p)
    if problems:
        raise BrandError("; ".join(problems))
    brand = dict(p["brand"])
    existing = (store.get().get("visual") or {}).get("brand") or {}
    if not brand.get("logo_url") and existing.get("logo_url"):
        brand["logo_url"] = existing["logo_url"]  # keep a logo uploaded earlier
    visual = {"brand": brand, "palette": p.get("palette") or [], "copy_rules": p.get("copy_rules") or [],
              "font_notes": p.get("font_notes") or "", "manual_template_work": p.get("manual_template_work") or {}}
    if source_uri:
        visual["source"] = source_uri
    store.merge({"visual": visual}, ["visual"])
    return bool(brand.get("logo_url"))


def save_logo(store, name: str, data: bytes) -> str:
    ext = ext_of(name)
    if ext not in IMAGE_EXT | {".svg"}:
        raise BrandError("the logo must be .png, .jpg, .webp or .svg")
    check_size(name, data, MAX_IMAGE_BYTES)
    uri = store.upload(f"logo{ext}", data, content_type(name, "image/png"))
    store.merge({"visual": {"brand": {"logo_url": uri}}}, ["visual.brand.logo_url"])
    return uri
