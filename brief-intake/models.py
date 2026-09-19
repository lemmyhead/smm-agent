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
    logo_url: str
    primary_color: str
    accent_color: str
    font: str = Field(min_length=1, max_length=60)

    _logo = field_validator("logo_url")(lambda cls, v: _media_url(v))

    @field_validator("primary_color", "accent_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not HEX.match(v):
            raise ValueError("must be a #hex colour like #1a2b3c")
        return v


class Brief(BaseModel):
    campaign_name: str = Field(min_length=1, max_length=200)
    key_message: str = Field(min_length=1, max_length=2000)
    tone: str = Field(min_length=1, max_length=200)
    cta: str = Field(min_length=1, max_length=80)
    link: Optional[str] = None
    brand: Brand
    background_image_url: str
    platforms: list[str] = Field(min_length=1)

    @field_validator("link")
    @classmethod
    def _link(cls, v: Optional[str]) -> Optional[str]:
        return _http_url(v) if v else None

    _bg = field_validator("background_image_url")(lambda cls, v: _media_url(v))

    @field_validator("platforms")
    @classmethod
    def _platforms(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if p not in PLATFORMS]
        if bad:
            raise ValueError(f"unknown platform(s) {bad}; allowed: {PLATFORMS}")
        if len(set(v)) != len(v):
            raise ValueError("platforms must not contain duplicates")
        return v
