"""render-service: fills a Jinja2 template and screenshots it to a PNG in GCS."""
import asyncio
import base64
import ipaddress
import logging
import os
import re
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from google.cloud import storage
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

log = logging.getLogger("render-service")
logging.basicConfig(level=logging.INFO)

BASE = Path(__file__).parent
SHARED_DIR = Path(os.environ.get("SHARED_DIR", BASE / "shared"))
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
MAX_IMAGE_BYTES = 15 * 1024 * 1024
DEFAULT_PRIMARY, DEFAULT_ACCENT, DEFAULT_FONT = "#1f2933", "#ffffff", "Helvetica Neue"  # for briefs with no brand
HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
SAFE_FONT = re.compile(r"^[A-Za-z0-9 \-]{1,60}$")

SPECS: dict = yaml.safe_load((SHARED_DIR / "platform_specs.yaml").read_text())
KNOWN_TEMPLATES = {s["template"] for s in SPECS.values()}
jinja = Environment(
    loader=FileSystemLoader(SHARED_DIR / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
)


class Variables(BaseModel):
    headline: str = Field(min_length=1, max_length=300)
    cta: str = Field(default="", max_length=80)
    campaign_name: str = Field(default="", max_length=120)
    logo_url: str | None = None
    background_image_url: str | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    font: str | None = None


class RenderRequest(BaseModel):
    template_id: str
    variables: Variables
    width: int = Field(ge=100, le=4000)
    height: int = Field(ge=100, le=4000)


class RenderResponse(BaseModel):
    gcs_path: str
    width: int
    height: int


_state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # One long-lived Chromium; each request gets its own isolated context.
    pw = await async_playwright().start()
    _state["browser"] = await pw.chromium.launch(
        args=["--no-sandbox", "--disable-dev-shm-usage"]  # Cloud Run has no user namespaces
    )
    _state["gcs"] = storage.Client()
    yield
    await _state["browser"].close()
    await pw.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _check_public_host(host: str) -> None:
    """Refuse loopback/private/link-local targets (e.g. the metadata server)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve host {host!r}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"refusing to fetch from non-public address ({host})")


async def _fetch_https(url: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for _ in range(4):  # follow a few redirects, re-checking each hop
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError(f"only https:// image URLs are allowed: {url}")
            await asyncio.to_thread(_check_public_host, parsed.hostname)
            resp = await client.get(url)
            if resp.is_redirect:
                url = str(resp.next_request.url) if resp.next_request else url
                continue
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
            if len(resp.content) > MAX_IMAGE_BYTES:
                raise ValueError("image is larger than 15 MB")
            return resp.content, ctype
    raise ValueError("too many redirects")


def _fetch_gcs(uri: str) -> tuple[bytes, str]:
    bucket, _, name = uri.removeprefix("gs://").partition("/")
    blob = _state["gcs"].bucket(bucket).blob(name)
    blob.reload()
    if (blob.size or 0) > MAX_IMAGE_BYTES:
        raise ValueError("image is larger than 15 MB")
    return blob.download_as_bytes(), blob.content_type or ""


async def to_data_uri(url: str) -> str:
    """Fetch an image server-side so a bad URL fails the render clearly, and Chromium
    never needs network access to arbitrary hosts."""
    if url.startswith("gs://"):
        data, ctype = await asyncio.to_thread(_fetch_gcs, url)
    else:
        data, ctype = await _fetch_https(url)
    if not ctype.startswith("image/"):
        guess = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}
        ctype = guess.get(Path(urlparse(url).path).suffix.lower(), "")
    if not ctype.startswith("image/"):
        raise ValueError(f"not an image: {url}")
    return f"data:{ctype};base64,{base64.b64encode(data).decode()}"


def _upload(png: bytes) -> str:
    if not ASSETS_BUCKET:
        raise RuntimeError("ASSETS_BUCKET env var is not set")
    name = f"renders/{uuid.uuid4()}.png"
    _state["gcs"].bucket(ASSETS_BUCKET).blob(name).upload_from_string(png, content_type="image/png")
    return f"gs://{ASSETS_BUCKET}/{name}"


async def render_html(html: str, width: int, height: int) -> bytes:
    context = await _state["browser"].new_context(
        viewport={"width": width, "height": height}, device_scale_factor=1
    )
    try:
        # Everything is inlined except Google Fonts; block all other network access.
        async def guard(route):
            url = route.request.url
            if url.startswith("data:") or urlparse(url).hostname in (
                "fonts.googleapis.com", "fonts.gstatic.com"
            ):
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", guard)
        page = await context.new_page()
        await page.set_content(html, wait_until="load", timeout=30_000)
        await page.evaluate("document.fonts.ready.then(() => true)")
        return await page.screenshot(type="png", full_page=False)
    finally:
        await context.close()


@app.post("/render", response_model=RenderResponse)
async def render(req: RenderRequest):
    if req.template_id not in KNOWN_TEMPLATES:
        raise HTTPException(400, f"unknown template_id {req.template_id!r}; known: {sorted(KNOWN_TEMPLATES)}")
    v = req.variables
    primary, accent = v.primary_color or DEFAULT_PRIMARY, v.accent_color or DEFAULT_ACCENT
    for label, color in (("primary_color", primary), ("accent_color", accent)):
        if not HEX_COLOR.match(color):
            raise HTTPException(400, f"{label} must be a #hex colour, got {color!r}")
    font = v.font if v.font and SAFE_FONT.match(v.font) else DEFAULT_FONT

    try:
        bg = await to_data_uri(v.background_image_url) if v.background_image_url else ""
        logo = await to_data_uri(v.logo_url) if v.logo_url else ""
    except Exception as e:  # bad/unreachable/oversized image -> a clear 422, not a 500
        log.warning("asset fetch failed: %s", e)
        raise HTTPException(422, f"could not load image: {e}") from e

    html = jinja.get_template(f"{req.template_id}.html.j2").render(
        headline=v.headline, cta=v.cta, campaign_name=v.campaign_name,
        primary_color=primary, accent_color=accent,
        font_family=font,
        font_url=f"https://fonts.googleapis.com/css2?family={quote(font)}:wght@400;700;800&display=swap",
        background_image_data=bg, logo_data=logo,
        width=req.width, height=req.height,
    )
    try:
        png = await render_html(html, req.width, req.height)
        gcs_path = await asyncio.to_thread(_upload, png)
    except Exception as e:
        log.exception("render failed")
        raise HTTPException(500, f"render failed: {e}") from e
    log.info("rendered %s -> %s", req.template_id, gcs_path)
    return RenderResponse(gcs_path=gcs_path, width=req.width, height=req.height)
