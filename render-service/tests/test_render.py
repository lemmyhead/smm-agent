"""Renders every template in real Chromium (network + GCS stubbed) and checks PNG sizes."""
import base64
import struct
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 8x8 solid PNG so the background has something to draw
PIXEL = "data:image/png;base64," + base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360f8cfc000000301010018dd8db00000000049454e44ae426082"
    )
).decode()

SPECS = yaml.safe_load((ROOT / "shared" / "platform_specs.yaml").read_text())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_DIR", str(ROOT / "shared"))
    import importlib

    import main

    importlib.reload(main)
    saved = {}

    def fake_upload(png: bytes) -> str:
        p = tmp_path / f"{len(saved)}.png"
        p.write_bytes(png)
        saved[p.name] = p
        return f"gs://bucket/renders/{p.name}"

    async def fake_data_uri(url: str) -> str:
        if "missing" in url:
            raise ValueError("HTTP 404")
        return PIXEL

    with mock.patch.object(main.storage, "Client"), \
         mock.patch.object(main, "_upload", fake_upload), \
         mock.patch.object(main, "to_data_uri", fake_data_uri):
        with TestClient(main.app) as c:
            c.saved = saved
            c.tmp = tmp_path
            yield c


def body(template_id, w, h, **over):
    v = {
        "headline": "Spring sale: everything you love, 30% off this weekend only",
        "cta": "Shop now", "campaign_name": "Spring Sale",
        "logo_url": "https://example.com/logo.png",
        "background_image_url": "https://example.com/bg.jpg",
        "primary_color": "#12355b", "accent_color": "#ffb703", "font": "Inter",
    }
    v.update(over)
    return {"template_id": template_id, "variables": v, "width": w, "height": h}


@pytest.mark.parametrize("platform", list(SPECS))
def test_each_platform_renders_exact_size(client, platform):
    s = SPECS[platform]
    r = client.post("/render", json=body(s["template"], s["width"], s["height"]))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["gcs_path"].startswith("gs://") and (out["width"], out["height"]) == (s["width"], s["height"])
    png = client.saved[out["gcs_path"].rsplit("/", 1)[1]].read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (s["width"], s["height"])


def test_unknown_template_is_400(client):
    assert client.post("/render", json=body("nope", 1080, 1080)).status_code == 400


def test_template_id_cannot_traverse(client):
    assert client.post("/render", json=body("../../etc/passwd", 1080, 1080)).status_code == 400


def test_background_logo_and_brand_are_optional(client):
    b = body("square_1x1", 1080, 1080)
    for k in ("logo_url", "background_image_url", "primary_color", "accent_color", "font"):
        del b["variables"][k]
    r = client.post("/render", json=b)
    assert r.status_code == 200, r.text
    png = client.saved[r.json()["gcs_path"].rsplit("/", 1)[1]].read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_bad_colour_is_400(client):
    r = client.post("/render", json=body("square_1x1", 1080, 1080, primary_color="red;}</style>"))
    assert r.status_code == 400


def test_unloadable_image_is_422(client):
    r = client.post("/render", json=body("square_1x1", 1080, 1080, background_image_url="https://x/missing.jpg"))
    assert r.status_code == 422 and "could not load image" in r.text


def test_headline_html_is_escaped(client):
    # would break rendering / inject markup if autoescape were off; must still produce a PNG
    r = client.post("/render", json=body("square_1x1", 1080, 1080, headline="<script>alert(1)</script> hi"))
    assert r.status_code == 200
