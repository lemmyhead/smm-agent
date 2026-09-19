"""Brand context for the drafter: the voice guide, example posts and writing rules that
brandctx (brand-context/brandctx.py) saved in Firestore `brand/<profile>`."""
import asyncio
import logging
import os
from dataclasses import dataclass, field

from google.cloud import storage
from google.genai import types

from . import jobs

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 4 * 1024 * 1024
NONE_PROVIDED = "(none provided)"


@dataclass
class BrandContext:
    voice: str = ""
    examples_text: str = ""
    copy_rules: str = ""
    image_uris: list[str] = field(default_factory=list)


def profile_name() -> str:
    return os.environ.get("BRAND_PROFILE", "default")


def from_doc(doc: dict) -> BrandContext:
    """Turns the Firestore document into prompt-ready pieces (pure; unit-tested)."""
    posts = [i["text"] for i in doc.get("examples") or [] if i.get("type") == "text" and i.get("text")]
    return BrandContext(
        voice=((doc.get("voice") or {}).get("text") or "").strip(),
        examples_text="\n\n".join(f"Example {n}:\n{t}" for n, t in enumerate(posts, 1)),
        copy_rules="\n".join(f"- {r}" for r in (doc.get("visual") or {}).get("copy_rules") or []),
        image_uris=[i["gcs_path"] for i in doc.get("examples") or [] if i.get("type") == "image" and i.get("gcs_path")],
    )


async def load_context() -> BrandContext:
    snap = await jobs._db().collection("brand").document(profile_name()).get()
    return from_doc(snap.to_dict() or {}) if snap.exists else BrandContext()


def _download(uri: str) -> tuple[bytes, str]:
    bucket, _, name = uri.removeprefix("gs://").partition("/")
    blob = storage.Client().bucket(bucket).blob(name)
    blob.reload()
    if (blob.size or 0) > MAX_IMAGE_BYTES:
        raise ValueError(f"{uri} is larger than 4 MB")
    return blob.download_as_bytes(), blob.content_type or "image/png"


async def fetch_image_parts(uris: list[str]) -> list[types.Part]:
    """Example screenshots as model input. A missing file fails the job with a clear message."""
    parts = []
    for uri in uris:
        try:
            data, ctype = await asyncio.to_thread(_download, uri)
        except Exception as e:
            raise RuntimeError(f"could not read brand example image {uri}: {e}") from e
        parts.append(types.Part.from_bytes(data=data, mime_type=ctype))
    return parts
