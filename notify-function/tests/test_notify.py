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
def slack(monkeypatch):
    s = mock.Mock()
    s.chat_postMessage.return_value = {"ts": "111.222"}
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
    monkeypatch.setattr(main, "_slack", lambda: s)
    monkeypatch.setattr(main, "_project", lambda: "proj")
    monkeypatch.setattr(main, "_download", lambda p: b"PNGDATA")
    return s


def test_should_notify():
    assert main.should_notify("rendering", "ready_for_review")
    assert main.should_notify("drafting", "failed")
    assert main.should_notify(None, "failed")
    assert not main.should_notify("ready_for_review", "ready_for_review")  # unrelated field update
    assert not main.should_notify("drafting", "rendering")
    assert not main.should_notify(None, None)


def test_ready_posts_drafts_then_images_in_thread(slack, monkeypatch):
    monkeypatch.setattr(main, "_read_job", lambda job_id: JOB_OK)
    main.notify_reviewer(event("rendering", "ready_for_review"))
    slack.chat_postMessage.assert_called_once()
    kw = slack.chat_postMessage.call_args.kwargs
    assert kw["channel"] == "C123"
    body = str(kw["blocks"])
    assert kw["blocks"][0]["text"]["text"].endswith("Spring & <Sale>")  # header is plain_text: no escaping
    assert "30% off &lt;now&gt; &amp; more" in body and "#spring" in body and "no caption" in body
    up = slack.files_upload_v2.call_args.kwargs
    assert up["thread_ts"] == "111.222" and up["file_uploads"][0]["filename"] == "facebook.png"


def test_failed_posts_error_and_no_images(slack, monkeypatch):
    job = {"brief": {"campaign_name": "X"}, "status": "failed", "error": "render failed for facebook: 422"}
    monkeypatch.setattr(main, "_read_job", lambda job_id: job)
    main.notify_reviewer(event("rendering", "failed"))
    assert "render failed for facebook: 422" in str(slack.chat_postMessage.call_args.kwargs["blocks"])
    slack.files_upload_v2.assert_not_called()


def test_failed_job_without_brief_still_notifies(slack, monkeypatch):
    monkeypatch.setattr(main, "_read_job", lambda job_id: {"status": "failed", "error": "boom"})
    main.notify_reviewer(event("drafting", "failed"))
    assert "unknown campaign" in slack.chat_postMessage.call_args.kwargs["text"]


def test_unchanged_status_does_nothing(slack, monkeypatch):
    read = mock.Mock()
    monkeypatch.setattr(main, "_read_job", read)
    main.notify_reviewer(event("ready_for_review", "ready_for_review"))
    main.notify_reviewer(event("drafting", "rendering"))
    read.assert_not_called()
    slack.chat_postMessage.assert_not_called()


def test_image_upload_failure_is_reported_not_raised(slack, monkeypatch):
    monkeypatch.setattr(main, "_read_job", lambda job_id: JOB_OK)
    slack.files_upload_v2.side_effect = RuntimeError("missing_scope")
    main.notify_reviewer(event("rendering", "ready_for_review"))
    assert "could not be attached" in slack.chat_postMessage.call_args_list[-1].kwargs["text"]


def test_long_body_is_split_into_multiple_sections():
    job = {**JOB_OK, "drafts": [{"platform": "instagram_feed", "headline": "h", "body": "x" * 7000, "hashtags": []}]}
    _, blocks = main.build_message("j", job)
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) >= 3 and all(len(s["text"]["text"]) <= 3000 for s in sections)
