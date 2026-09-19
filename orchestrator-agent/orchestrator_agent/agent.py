"""Orchestrator: draft copy for every requested platform (one Gemini call), then render one
cover image per platform, keeping the Firestore job document up to date throughout."""
import asyncio
import json
import logging
import os
from functools import cached_property
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, InvocationContext, LlmAgent
from google.adk.events import Event, EventActions
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import Client, types
from pydantic import BaseModel, Field

from . import jobs
from .specs import enforce_limit, load_specs, rules_text
from .tools import render_tool

log = logging.getLogger(__name__)
MAX_PARALLEL_RENDERS = 3  # render-service concurrency is 4; leave headroom


class PlatformDraft(BaseModel):
    platform: str = Field(description="The platform id exactly as given in the rules")
    headline: str = Field(description="Short punchy headline; it is also printed on the cover image")
    body: str = Field(description="Post copy")
    hashtags: list[str] = Field(default_factory=list, description="Hashtags without the # sign")


class DraftSet(BaseModel):
    drafts: list[PlatformDraft]


INSTRUCTION = """You are a social media copywriter. Write one draft per platform for this campaign brief.

Brief (JSON):
{brief_json}

Platforms and their hard limits:
{platform_rules}

Requirements:
- Return exactly one draft for each listed platform, using the platform id exactly as written.
- Match the brief's tone: follow it, do not invent a different brand voice.
- Work the key message and the call to action into every draft that has body copy. If the brief has a
  link, include it in the body where the platform allows links.
- Adapt style per platform (Facebook: conversational; Instagram: visual, emoji sparingly; Threads: short and
  punchy; YouTube community: inviting, ends with a question or clear next step).
- Headlines are short (under 70 characters) and must not repeat the body word for word.
- Do not invent facts, prices, discounts or dates that are not in the brief.
"""


def _model():
    name = os.environ.get("DRAFT_MODEL", "gemini-2.5-flash")
    location = os.environ.get("DRAFT_MODEL_LOCATION")
    if not location:
        return name

    class LocatedGemini(Gemini):  # some newer models are only served from the `global` location
        @cached_property
        def api_client(self) -> Client:
            return Client(enterprise=True, location=location)

    return LocatedGemini(model=name)


drafter = LlmAgent(
    name="drafter",
    model=_model(),
    instruction=INSTRUCTION,
    output_schema=DraftSet,
    output_key="drafts_raw",
)


def _text(event_text: str) -> Event:
    return Event(author="smm_orchestrator", content=types.Content(role="model", parts=[types.Part(text=event_text)]))


def _parse_drafts(raw, platforms: list[str]) -> list[dict]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if raw is None:
        raise ValueError("the drafting model returned no output")
    items = DraftSet.model_validate(raw).drafts
    by_platform = {d.platform: d for d in items}
    missing = [p for p in platforms if p not in by_platform]
    if missing:
        raise ValueError(f"drafting model omitted platforms: {', '.join(missing)}")
    specs = load_specs()
    out = []
    for p in platforms:  # keep the brief's order, drop anything extra
        d = by_platform[p]
        out.append({
            "platform": p,
            "headline": d.headline.strip(),
            "body": enforce_limit(d.body.strip(), specs[p]["copy_limit"]),
            "hashtags": [h.lstrip("#").strip() for h in d.hashtags if h.strip()] if specs[p]["copy_limit"] else [],
        })
    return out


class SMMOrchestrator(BaseAgent):
    """Deterministic pipeline (draft -> render -> mark ready) around one LLM call.

    The render step is not left to the model's tool-calling: five renders must happen exactly
    once each, so the FunctionTool is invoked directly."""

    drafter: LlmAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        job_id = None
        try:
            message = "".join(p.text or "" for p in (ctx.user_content.parts or []))
            payload = json.loads(message)
            job_id, brief = payload["job_id"], payload["brief"]
            platforms = list(dict.fromkeys(brief["platforms"]))
            unknown = [p for p in platforms if p not in load_specs()]
            if unknown:
                raise ValueError(f"unsupported platforms: {', '.join(unknown)}")

            # 1. Draft
            yield Event(
                author=self.name,
                actions=EventActions(state_delta={
                    "brief": brief,
                    "brief_json": json.dumps(brief, ensure_ascii=False, indent=2),
                    "platform_rules": rules_text(platforms),
                }),
            )
            async for event in self.drafter.run_async(ctx):
                yield event
            drafts = _parse_drafts(ctx.session.state.get("drafts_raw"), platforms)
            await jobs.update_job(job_id, drafts=drafts, status="rendering", error=None)

            # 2. Render, one image per platform
            sem = asyncio.Semaphore(MAX_PARALLEL_RENDERS)

            async def render_one(draft: dict) -> None:
                async with sem:
                    image = await render_tool.run_async(
                        args={"platform": draft["platform"], "headline": draft["headline"]},
                        tool_context=ToolContext(ctx),
                    )
                await jobs.append_image(job_id, image)

            results = await asyncio.gather(*(render_one(d) for d in drafts), return_exceptions=True)
            errors = [f"{d['platform']}: {r}" for d, r in zip(drafts, results) if isinstance(r, BaseException)]
            if errors:
                raise RuntimeError("render failed for " + "; ".join(errors))

            await jobs.update_job(job_id, status="ready_for_review", error=None)
            yield _text(f"Job {job_id}: {len(drafts)} drafts and images ready for review.")
        except Exception as e:  # any failure ends in status=failed so the notify function fires
            log.exception("orchestration failed")
            if job_id:
                await jobs.fail_job(job_id, f"{type(e).__name__}: {e}")
            yield _text(f"Job {job_id or '?'} failed: {e}")


root_agent = SMMOrchestrator(name="smm_orchestrator", drafter=drafter, sub_agents=[drafter])
