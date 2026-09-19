"""Platform rules live in platform_specs.yaml (copied from shared/ by scripts/sync-shared.sh)."""
from functools import lru_cache
from pathlib import Path

import yaml

SPECS_PATH = Path(__file__).parent / "platform_specs.yaml"


@lru_cache(maxsize=1)
def load_specs() -> dict:
    return yaml.safe_load(SPECS_PATH.read_text())


def rules_text(platforms: list[str]) -> str:
    """Human-readable per-platform rules for the drafting prompt, generated from the yaml."""
    specs = load_specs()
    lines = []
    for p in platforms:
        limit = specs[p]["copy_limit"]
        if limit == 0:
            lines.append(f"- {p}: NO caption. Leave body as an empty string and hashtags as an empty list. "
                         "The headline is the only text (it is overlaid on the image), keep it under 60 characters.")
        else:
            lines.append(f"- {p}: body must be at most {limit} characters (hard limit).")
    return "\n".join(lines)


def enforce_limit(body: str, limit: int) -> str:
    """Safety net if the model overshoots: cut at a word boundary and add an ellipsis."""
    if limit == 0:
        return ""
    if len(body) <= limit:
        return body
    cut = body[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"
