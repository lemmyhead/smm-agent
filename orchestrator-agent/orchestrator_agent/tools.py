"""render_cover_image: the ADK FunctionTool that calls the render service."""
import asyncio
import os

import google.auth.transport.requests
import httpx
from google.adk.tools import FunctionTool, ToolContext
from google.oauth2 import id_token

from .specs import load_specs


def _id_token(audience: str) -> str:
    # On Agent Engine (running as sa-agent-orchestrator) this comes from the metadata server.
    return id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)


async def render_cover_image(platform: str, headline: str, tool_context: ToolContext) -> dict:
    """Render the cover image for one platform via the render service.

    Args:
        platform: One of the platforms in platform_specs.yaml (e.g. "facebook").
        headline: The headline text to place on the image.

    Returns:
        {"platform", "gcs_path", "width", "height"} for the rendered PNG.
    """
    specs = load_specs()
    if platform not in specs:
        raise ValueError(f"unknown platform {platform!r}")
    spec = specs[platform]
    brief = tool_context.state["brief"]
    brand = brief.get("brand") or {}

    base_url = os.environ.get("RENDER_SERVICE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("RENDER_SERVICE_URL is not set on the agent")

    payload = {
        "template_id": spec["template"],
        "width": spec["width"],
        "height": spec["height"],
        "variables": {
            "headline": headline,
            "cta": brief.get("cta") or "",
            "campaign_name": brief.get("campaign_name") or "",
            "logo_url": brand.get("logo_url"),
            "background_image_url": brief.get("background_image_url"),
            "primary_color": brand.get("primary_color"),
            "accent_color": brand.get("accent_color"),
            "font": brand.get("font"),
        },
    }
    # Anything the brief and the brand context leave out is omitted, so the render service applies its defaults.
    payload["variables"] = {k: v for k, v in payload["variables"].items() if v is not None}
    headers = {}
    if not base_url.startswith(("http://localhost", "http://127.0.0.1")):
        headers["Authorization"] = f"Bearer {await asyncio.to_thread(_id_token, base_url)}"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{base_url}/render", json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"render service returned {resp.status_code}: {resp.text[:300]}")
    out = resp.json()
    return {"platform": platform, "gcs_path": out["gcs_path"], "width": out["width"], "height": out["height"]}


render_tool = FunctionTool(render_cover_image)
