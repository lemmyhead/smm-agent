import copy
import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import os  # noqa: E402

os.environ["PLATFORM_SPECS"] = str(ROOT.parent / "shared" / "platform_specs.yaml")
import main  # noqa: E402

GOOD = {
    "campaign_name": "Spring Sale", "key_message": "30% off", "tone": "friendly", "cta": "Shop now",
    "link": "https://example.com",
    "brand": {"logo_url": "https://example.com/l.png", "primary_color": "#12355b",
              "accent_color": "#ffb703", "font": "Inter"},
    "background_image_url": "gs://bucket/bg.jpg",
    "platforms": ["facebook", "threads"],
}


class FakeDb:
    def __init__(self):
        self.docs = {}
        self.created_status = {}

    def collection(self, _):
        return self

    def document(self, job_id):
        self.current = job_id
        return self

    def set(self, data):
        self.created_status[self.current] = data["status"]
        self.docs[self.current] = data

    def update(self, data):
        self.docs[self.current].update(data)

    def get(self):
        m = mock.Mock()
        m.to_dict.return_value = self.docs[self.current]
        return m


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(main, "db", lambda: fake)
    monkeypatch.setattr(main, "brand_defaults", lambda: {})
    run = mock.Mock()
    monkeypatch.setattr(main.agent_client, "run_agent", run)
    return TestClient(main.app), fake, run


def test_valid_brief_returns_202_and_creates_job(env):
    client, fake, run = env
    r = client.post("/briefs", json=GOOD)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert fake.created_status[job_id] == "drafting" and fake.docs[job_id]["brief"]["campaign_name"] == "Spring Sale"
    run.assert_called_once()
    assert run.call_args.args[0] == job_id
    # agent never touched the doc -> the safety net marks it failed instead of leaving it stuck
    assert fake.docs[job_id]["status"] == "failed" and "still 'drafting'" in fake.docs[job_id]["error"]


def test_agent_crash_marks_job_failed(env):
    client, fake, run = env
    run.side_effect = RuntimeError("agent query failed (403)")
    job_id = client.post("/briefs", json=GOOD).json()["job_id"]
    assert fake.docs[job_id]["status"] == "failed" and "403" in fake.docs[job_id]["error"]


def test_completed_job_is_left_alone(env):
    client, fake, run = env
    run.side_effect = lambda job_id, brief: fake.docs[job_id].update(status="ready_for_review")
    job_id = client.post("/briefs", json=GOOD).json()["job_id"]
    assert fake.docs[job_id]["status"] == "ready_for_review"


@pytest.mark.parametrize("mutate,field", [
    (lambda b: b.pop("platforms"), "platforms"),
    (lambda b: b.update(platforms=[]), "platforms"),
    (lambda b: b.update(platforms=["myspace"]), "platforms"),
    (lambda b: b.update(platforms=["threads", "threads"]), "platforms"),
    (lambda b: b.update(background_image_url="not a url"), "background_image_url"),
    (lambda b: b.update(background_image_url="http://insecure.example/x.jpg"), "background_image_url"),
    (lambda b: b["brand"].update(primary_color="red"), "brand.primary_color"),
    (lambda b: b.update(link="javascript:alert(1)"), "link"),
    (lambda b: b.pop("cta"), "cta"),
])
def test_invalid_brief_is_400_with_field_name(env, mutate, field):
    client, fake, run = env
    bad = copy.deepcopy(GOOD)
    mutate(bad)
    r = client.post("/briefs", json=bad)
    assert r.status_code == 400
    assert any(d["field"] == field for d in r.json()["details"]), r.json()
    assert fake.docs == {} and not run.called


def test_non_json_body_is_400(env):
    client, *_ = env
    assert client.post("/briefs", content="nope", headers={"content-type": "application/json"}).status_code == 400


def test_link_is_optional(env):
    client, *_ = env
    b = copy.deepcopy(GOOD)
    del b["link"]
    assert client.post("/briefs", json=b).status_code == 202


SAVED = {"voice": {"text": "Friendly. No exclamation marks."},
         "visual": {"brand": {"logo_url": "gs://bucket/brand/default/logo.png", "primary_color": "#111111",
                              "accent_color": "#eeeeee", "font": "Lato"}}}


def test_saved_brand_and_voice_fill_a_minimal_brief(env, monkeypatch):
    client, fake, run = env
    monkeypatch.setattr(main, "brand_defaults", lambda: SAVED)
    b = copy.deepcopy(GOOD)
    del b["brand"], b["tone"]
    r = client.post("/briefs", json=b)
    assert r.status_code == 202, r.text
    saved_brief = fake.docs[r.json()["job_id"]]["brief"]
    assert saved_brief["brand"] == SAVED["visual"]["brand"] and "brand voice guide" in saved_brief["tone"]


def test_fields_sent_in_the_brief_win_over_saved_defaults(env, monkeypatch):
    client, fake, run = env
    monkeypatch.setattr(main, "brand_defaults", lambda: SAVED)
    b = copy.deepcopy(GOOD)
    b["brand"] = {"primary_color": "#abcdef"}  # partial: only the colour is overridden
    r = client.post("/briefs", json=b)
    assert r.status_code == 202, r.text
    brief = fake.docs[r.json()["job_id"]]["brief"]
    assert brief["brand"]["primary_color"] == "#abcdef" and brief["brand"]["font"] == "Lato"
    assert brief["tone"] == "friendly"


def test_tone_is_required_without_a_saved_voice_guide(env):
    client, fake, run = env
    b = copy.deepcopy(GOOD)
    del b["tone"]
    r = client.post("/briefs", json=b)
    assert r.status_code == 400 and {d["field"] for d in r.json()["details"]} == {"tone"}
    assert fake.docs == {} and not run.called


def test_background_image_and_brand_are_optional(env):
    client, fake, run = env
    b = copy.deepcopy(GOOD)
    del b["brand"], b["background_image_url"]
    r = client.post("/briefs", json=b)
    assert r.status_code == 202, r.text
    brief = fake.docs[r.json()["job_id"]]["brief"]
    assert brief["background_image_url"] is None and not any(brief["brand"].values())


def test_a_partial_brand_is_accepted_but_what_is_given_is_still_checked(env):
    client, fake, run = env
    b = copy.deepcopy(GOOD)
    b["brand"] = {"primary_color": "#abcdef"}
    assert client.post("/briefs", json=b).status_code == 202
    b["brand"] = {"logo_url": "ftp://nope"}
    r = client.post("/briefs", json=b)
    assert r.status_code == 400 and r.json()["details"][0]["field"] == "brand.logo_url"
