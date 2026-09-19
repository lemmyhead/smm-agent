"""Brief schema (the input contract) and the platform list, read from shared/platform_specs.yaml."""
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator

SPECS_PATH = Path(os.environ.get("PLATFORM_SPECS", Path(__file__).parent / "shared" / "platform_specs.yaml"))
PLATFORMS: list[str] = list(yaml.safe_load(SPECS_PATH.read_text()))
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _http_url(v: str) -> str:
    p = urlparse(v)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("must be a valid http(s) URL")
    return v


def _media_url(v: str) -> str:
    p = urlparse(v)
    if p.scheme not in ("https", "gs") or not p.netloc:
        raise ValueError("must be an https:// or gs:// URL")
    return v


class Brand(BaseModel):
    """Every part is optional; whatever is missing falls back to the render service's neutral defaults
    (no logo, dark background, white accent, Helvetica Neue). What is given must still be valid."""
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    accent_color: Optional[str] = None
    font: Optional[str] = Field(default=None, min_length=1, max_length=60)

    @field_validator("logo_url")
    @classmethod
    def _logo(cls, v: Optional[str]) -> Optional[str]:
        return _media_url(v) if v else None

    @field_validator("primary_color", "accent_color")
    @classmethod
    def _hex(cls, v: Optional[str]) -> Optional[str]:
        if v and not HEX.match(v):
            raise ValueError("must be a #hex colour like #1a2b3c")
        return v or None


class BriefIn(BaseModel):
    """What callers send. `brand` and `tone` may be left out when a default brand context has been saved
    (brand-context/brandctx.py); intake fills the gaps and then validates the result as a Brief."""
    brand: Optional[dict] = None
    tone: Optional[str] = None

    model_config = {"extra": "allow"}


class Brief(BaseModel):
    campaign_name: str = Field(min_length=1, max_length=200)
    key_message: str = Field(min_length=1, max_length=2000)
    tone: str = Field(min_length=1, max_length=200)
    cta: str = Field(min_length=1, max_length=80)
    link: Optional[str] = None
    brand: Brand = Brand()
    background_image_url: Optional[str] = None
    platforms: list[str] = Field(min_length=1)

    @field_validator("link")
    @classmethod
    def _link(cls, v: Optional[str]) -> Optional[str]:
        return _http_url(v) if v else None

    @field_validator("background_image_url")
    @classmethod
    def _bg(cls, v: Optional[str]) -> Optional[str]:
        return _media_url(v) if v else None

    @field_validator("platforms")
    @classmethod
    def _platforms(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if p not in PLATFORMS]
        if bad:
            raise ValueError(f"unknown platform(s) {bad}; allowed: {PLATFORMS}")
        if len(set(v)) != len(v):
            raise ValueError("platforms must not contain duplicates")
        return v
