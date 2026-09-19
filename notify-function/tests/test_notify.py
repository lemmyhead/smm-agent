import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from google.events.cloud import firestore as fe

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402

NAME = "projects/p/databases/(default)/documents/jobs/job1"


def event(old, new):
    def doc(status):
        return fe.Document(name=NAME, fields={"status": fe.Value(string_value=status), "x": fe.Value(integer_value=1)}) \
            if status else fe.Document(name=NAME, fields={"x": fe.Value(integer_value=1)})
    data = fe.DocumentEventData(old_value=doc(old), value=doc(new))
    return SimpleNamespace(data=type(data).serialize(data))


JOB_OK = {
    "brief": {"campaign_name": "Spring & <Sale>"},
    "status": "ready_for_review",
    "drafts": [
        {"platform": "facebook", "headline": "Big news", "body": "30% off <now> & more", "hashtags": ["spring"]},
        {"platform": "instagram_story", "headline": "Story", "body": "", "hashtags": []},
    ],
    "images": [{"platform": "facebook", "gcs_path": "gs://b/renders/a.png", "width": 1200, "height": 630}],
}


@pytest.fixture()
def tg(monkeypatch):
    """Fake Telegram: records (method, data, files) and hands out incrementing message ids."""
    calls = []

    def fake(method, data, files=None):
        calls.append((method, data, files))
        return {"message_id": 100 + len(calls)}

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001")
    monkeypatch.setattr(main, "_telegram", fake)
    monkeypatch.setattr(main, "_project", lambda: "proj")
    monkeypatch.setattr(main, "_download", lambda p: b"PNGDATA")
    return calls


def texts(calls):
    return [d["text"] for m, d, _ in calls if m == "sendMessage"]


def test_should_notify():
    assert main.should_notify("rendering", "ready_for_review")
    assert main.should_notify("drafting", "failed")
    assert main.should_notify(None, "failed")
    assert not main.should_notify("ready_for_review", "ready_for_review")  # unrelated field update
    assert not main.should_notify("drafting", "rendering")
    assert not main.should_notify(None, None)


def test_ready_posts_drafts_then_images_as_replies(tg, monkeypatch):
    monkeypatch.setattr(main, "_read_job", lambda job_id: JOB_OK)
    main.notify_reviewer(event("rendering", "ready_for_review"))
    msgs = texts(tg)
    assert len(msgs) == 3  # header + one per draft
    assert "Ready for review: Spring &amp; &lt;Sale&gt;" in msgs[0]
    assert "30% off &lt;now&gt; &amp; more" in msgs[1] and "#spring" in msgs[1]
    assert "no caption" in msgs[2]
    assert all(d["chat_id"] == "-1001" and d["parse_mode"] == "HTML" for m, d, _ in tg if m == "sendMessage")
    assert all('"message_id": 101' in d["reply_parameters"] for m, d, _ in tg[1:])  # threaded under the header
    method, data, files = tg[-1]
    assert method == "sendPhoto"  # one image: albums need 2+
    assert files["photo"][0] == "facebook.png" and "facebook (1200×630)" in data["caption"]


def test_several_images_go_out_as_one_album(tg, monkeypatch):
    imgs = [{"platform": p, "gcs_path": f"gs://b/{p}.png", "width": 1, "height": 1} for p in ("facebook", "threads")]
    monkeypatch.setattr(main, "_read_job", lambda job_id: {**JOB_OK, "images": imgs})
    main.notify_reviewer(event("rendering", "ready_for_review"))
    method, data, files = tg[-1]
    assert method == "sendMediaGroup" and set(files) == {"f0", "f1"}
    media = json.loads(data["media"])
    assert media[0]["media"] == "attach://f0" and media[1]["caption"] == "threads (1×1)"


def test_failed_posts_error_and_no_images(tg, monkeypatch):
    job = {"brief": {"campaign_name": "X"}, "status": "failed", "error": "render failed for facebook: 422 <bad>"}
    monkeypatch.setattr(main, "_read_job", lambda job_id: job)
    main.notify_reviewer(event("rendering", "failed"))
    assert [m for m, _, _ in tg] == ["sendMessage"]
    assert "render failed for facebook: 422 &lt;bad&gt;" in texts(tg)[0]


def test_failed_job_without_brief_still_notifies(tg, monkeypatch):
    monkeypatch.setattr(main, "_read_job", lambda job_id: {"status": "failed", "error": "boom"})
    main.notify_reviewer(event("drafting", "failed"))
    assert "unknown campaign" in texts(tg)[0]


def test_unchanged_status_does_nothing(tg, monkeypatch):
    read = mock.Mock()
    monkeypatch.setattr(main, "_read_job", read)
    main.notify_reviewer(event("ready_for_review", "ready_for_review"))
    main.notify_reviewer(event("drafting", "rendering"))
    read.assert_not_called()
    assert tg == []


def test_image_failure_is_reported_not_raised(tg, monkeypatch):
    monkeypatch.setattr(main, "_read_job", lambda job_id: JOB_OK)
    monkeypatch.setattr(main, "_download", mock.Mock(side_effect=RuntimeError("gcs down")))
    main.notify_reviewer(event("rendering", "ready_for_review"))
    assert "could not be attached" in texts(tg)[-1]


def test_long_body_is_truncated_under_telegram_limit():
    job = {**JOB_OK, "drafts": [{"platform": "instagram_feed", "headline": "h", "body": "x" * 7000, "hashtags": ["a"]}]}
    msgs = main.build_messages("j", job)
    assert len(msgs) == 2 and len(msgs[1]) <= 4096 and msgs[1].endswith("…\n<i>#a</i>")


def test_telegram_error_text_is_surfaced(monkeypatch):
    monkeypatch.setattr(main, "_token", lambda: "t")
    fake = mock.Mock(content=b"x", status_code=400)
    fake.json.return_value = {"ok": False, "description": "Bad Request: chat not found"}
    monkeypatch.setattr(main.requests, "post", lambda *a, **k: fake)
    with pytest.raises(RuntimeError, match="chat not found"):
        main._telegram("sendMessage", {})
