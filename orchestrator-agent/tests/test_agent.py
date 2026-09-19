"""Runs the whole pipeline through ADK's runner with a fake Gemini, a fake render service and
an in-memory stand-in for Firestore."""
import json
import sys
from pathlib import Path

import httpx
import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator_agent import agent as agent_mod  # noqa: E402
from orchestrator_agent import jobs, tools  # noqa: E402
from orchestrator_agent.specs import enforce_limit, load_specs  # noqa: E402

BRIEF = {
    "campaign_name": "Spring Sale", "key_message": "30% off", "tone": "friendly", "cta": "Shop now",
    "link": "https://example.com/sale",
    "brand": {"logo_url": "https://example.com/l.png", "primary_color": "#12355b",
              "accent_color": "#ffb703", "font": "Inter"},
    "background_image_url": "https://example.com/bg.jpg",
    "platforms": ["facebook", "instagram_story", "threads"],
}


class FakeLlm(BaseLlm):
    reply: str = ""
    seen_prompt: str = ""

    async def generate_content_async(self, llm_request, stream=False):
        FakeLlm.seen_prompt = str(llm_request.config.system_instruction)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=self.reply)]))


class FakeDb:
    def __init__(self):
        self.doc: dict = {"images": []}

    async def update_job(self, job_id, **f):
        for k, v in f.items():
            self.doc[k] = v

    async def append_image(self, job_id, image):
        self.doc["images"].append(image)

    async def fail_job(self, job_id, message):
        self.doc.update(status="failed", error=message)


@pytest.fixture()
def db(monkeypatch):
    d = FakeDb()
    monkeypatch.setattr(jobs, "update_job", d.update_job)
    monkeypatch.setattr(jobs, "append_image", d.append_image)
    monkeypatch.setattr(jobs, "fail_job", d.fail_job)
    return d


@pytest.fixture()
def render(monkeypatch):
    calls = []
    monkeypatch.setenv("RENDER_SERVICE_URL", "http://localhost:9999")

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        if "boom" in body["variables"]["headline"]:
            return httpx.Response(422, json={"detail": "could not load image: HTTP 404"})
        return httpx.Response(200, json={"gcs_path": f"gs://b/renders/{body['template_id']}-{len(calls)}.png",
                                         "width": body["width"], "height": body["height"]})

    real = httpx.AsyncClient
    monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return calls


async def run(reply: str, brief=BRIEF, message=None):
    llm = FakeLlm(model="fake", reply=reply)
    drafter = LlmAgent(name="drafter", model=llm, instruction=agent_mod.INSTRUCTION,
                       output_schema=agent_mod.DraftSet, output_key="drafts_raw")
    root = agent_mod.SMMOrchestrator(name="smm_orchestrator", drafter=drafter, sub_agents=[drafter])
    runner = InMemoryRunner(agent=root, app_name="t")
    session = await runner.session_service.create_session(app_name="t", user_id="u")
    text = message if message is not None else json.dumps({"job_id": "job1", "brief": brief})
    async for event in runner.run_async(user_id="u", session_id=session.id,
                                        new_message=types.Content(role="user", parts=[types.Part(text=text)])):
        # Agent Engine's session service rejects events with no invocation_id (found on first deploy)
        assert event.invocation_id, f"event from {event.author} has no invocation_id"


def drafts_json(platforms, body="Big news! 30% off. Shop now https://example.com/sale"):
    return json.dumps({"drafts": [
        {"platform": p, "headline": f"Spring Sale ({p})", "body": body, "hashtags": ["#spring", "sale"]}
        for p in platforms]})


async def test_happy_path(db, render):
    await run(drafts_json(["threads", "facebook", "instagram_story"]))
    assert db.doc["status"] == "ready_for_review" and db.doc["error"] is None
    assert [d["platform"] for d in db.doc["drafts"]] == BRIEF["platforms"]  # brief order preserved
    story = next(d for d in db.doc["drafts"] if d["platform"] == "instagram_story")
    assert story["body"] == "" and story["hashtags"] == []  # copy_limit 0
    fb = next(d for d in db.doc["drafts"] if d["platform"] == "facebook")
    assert fb["hashtags"] == ["spring", "sale"]  # '#' stripped
    assert sorted(i["platform"] for i in db.doc["images"]) == sorted(BRIEF["platforms"])
    sizes = {i["platform"]: (i["width"], i["height"]) for i in db.doc["images"]}
    assert sizes["instagram_story"] == (1080, 1920)
    # the platform rules came from the yaml, not hardcoded in the prompt
    assert "at most 500 characters" in FakeLlm.seen_prompt and "NO caption" in FakeLlm.seen_prompt
    assert len(render) == 3 and render[0]["variables"]["primary_color"] == "#12355b"


async def test_overlong_copy_is_trimmed_to_limit(db, render):
    await run(drafts_json(["threads", "facebook", "instagram_story"], body="word " * 300))
    fb = next(d for d in db.doc["drafts"] if d["platform"] == "facebook")
    assert len(fb["body"]) <= 500 and fb["body"].endswith("…")


async def test_missing_platform_in_model_output_fails_job(db, render):
    await run(drafts_json(["facebook"]))
    assert db.doc["status"] == "failed" and "omitted platforms" in db.doc["error"]
    assert render == []


async def test_render_error_fails_job_with_readable_message(db, render):
    reply = json.loads(drafts_json(["facebook", "instagram_story", "threads"]))
    reply["drafts"][0]["headline"] = "boom"
    await run(json.dumps(reply))
    assert db.doc["status"] == "failed"
    assert "facebook" in db.doc["error"] and "could not load image" in db.doc["error"]
    assert db.doc["drafts"]  # drafts were still saved for debugging


async def test_llm_returning_garbage_fails_job(db, render):
    await run("this is not json")
    assert db.doc["status"] == "failed"


async def test_unsupported_platform_fails_job(db, render):
    await run("{}", brief={**BRIEF, "platforms": ["myspace"]})
    assert db.doc["status"] == "failed" and "unsupported platforms" in db.doc["error"]


def test_enforce_limit():
    assert enforce_limit("short", 500) == "short"
    assert enforce_limit("anything", 0) == ""
    assert len(enforce_limit("a b " * 400, 100)) <= 100
    assert set(load_specs()) == {"facebook", "instagram_feed", "instagram_story", "threads", "youtube_community"}
