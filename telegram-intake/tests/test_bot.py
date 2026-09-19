import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "telegram-intake"))
sys.path.insert(0, str(ROOT / "brand-context"))  # first, so the source of brandcore wins over the deploy-time copy
import brandcore as core  # noqa: E402
import briefs  # noqa: E402
import main  # noqa: E402

CHAT = 440581589
SECRET = "s3cret"
H = {"X-Telegram-Bot-Api-Secret-Token": SECRET}

EXTRACTION = core.Extraction(
    primary_color="#1F4D3A", accent_color="#F2B134", font="Lora", font_notes="",
    palette=["Forest #1F4D3A: main"], logo_rules=["Logo top-left"], layout_rules=["Headline bottom-left"],
    imagery_rules=[], copy_rules=["No exclamation marks in headlines"])


class FakeStore:
    profile = "default"
    bucket_name = "b"

    def __init__(self):
        self.doc, self.uploads, self.deleted = {}, {}, []

    def get(self):
        return json.loads(json.dumps(self.doc))

    def merge(self, fields, paths):
        for path in paths:
            top, *rest = path.split(".")
            if not rest:
                self.doc[top] = fields[top]
            else:
                node, src = self.doc.setdefault(top, {}), fields[top]
                for key in rest[:-1]:
                    node, src = node.setdefault(key, {}), src[key]
                node[rest[-1]] = src[rest[-1]]

    def append_examples(self, items, check):
        merged = self.doc.get("examples", []) + items
        check(merged)  # like the real store: rejected before anything is written
        self.doc["examples"] = merged
        return merged

    def upload(self, name, data, ctype):
        self.uploads[name] = data
        return f"gs://b/brand/default/{name}"

    def upload_path(self, path, data, ctype):
        self.uploads[path] = data
        return f"gs://b/{path}"

    def delete_prefix(self, prefix):
        self.deleted.append(prefix)

    def delete_profile(self):
        self.doc = {}
        self.deleted.append("*")


class FakeModels:
    def __init__(self, ai):
        self.ai = ai

    def generate_content(self, model, contents, config):
        self.ai.prompts.append(contents)
        return type("R", (), {"text": json.dumps(self.ai.replies.pop(0))})()


class FakeAi:
    model = "fake"

    def __init__(self):
        self.calls, self.prompts, self.replies = [], [], []
        self.client = type("C", (), {"models": FakeModels(self)})()

    def transcribe(self, pdf):
        self.calls.append("transcribe")
        return "# Voice\nBe kind. No exclamation marks."

    def extract_visual(self, pdf):
        self.calls.append("extract")
        return EXTRACTION


class Bot:
    def __init__(self, monkeypatch):
        self.store, self.ai, self.sent, self.sessions, self.seen = FakeStore(), FakeAi(), [], {}, set()
        self.files = {}
        self.n = 0
        self.client = TestClient(main.app)
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{CHAT}, 999")
        monkeypatch.setattr(main, "store", lambda: self.store)
        monkeypatch.setattr(main, "ai", lambda: self.ai)
        monkeypatch.setattr(main, "tg_send", lambda chat, text: self.sent.append(text))
        monkeypatch.setattr(main, "tg_download", lambda file_id: self.files[file_id])
        monkeypatch.setattr(main, "session_get", lambda chat: dict(self.sessions.get(chat, {})))
        monkeypatch.setattr(main, "session_put", lambda chat, **f: self.sessions.setdefault(chat, {}).update(f))
        self.submitted = []
        monkeypatch.setattr(briefs, "submit", lambda draft: self.submitted.append(dict(draft)) or "0123456789abcdef")
        monkeypatch.setattr(main, "claim_update", lambda uid: (uid not in self.seen) and not self.seen.add(uid))

    def send(self, chat=CHAT, headers=H, uid=None, **message):
        self.n += 1
        update = {"update_id": uid or self.n, "message": {"chat": {"id": chat}, **message}}
        return self.client.post("/telegram", json=update, headers=headers)

    def say(self, text):
        return self.send(text=text)

    def file(self, name, data):
        fid = f"f{len(self.files)}"
        self.files[fid] = data
        return self.send(document={"file_id": fid, "file_name": name, "file_size": len(data)})

    def photo(self, data, uniq="u1"):
        fid = f"p{len(self.files)}"
        self.files[fid] = data
        return self.send(photo=[{"file_id": "small", "file_unique_id": "x"}, {"file_id": fid, "file_unique_id": uniq}])

    @property
    def last(self):
        return self.sent[-1]


@pytest.fixture()
def bot(monkeypatch):
    return Bot(monkeypatch)


# ---------- security ----------

def test_wrong_or_missing_secret_is_rejected(bot):
    assert bot.send(headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}, text="/help").status_code == 401
    assert bot.send(headers={}, text="/help").status_code == 401
    assert bot.sent == []


def test_webhook_refuses_to_run_without_a_configured_secret(bot, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    assert bot.say("/help").status_code == 503


def test_chats_not_on_the_allow_list_get_no_answer_and_no_processing(bot):
    r = bot.send(chat=12345, text="/clear confirm")
    assert r.status_code == 200 and bot.sent == [] and bot.store.deleted == []


def test_duplicate_update_is_processed_once(bot):
    bot.send(uid=77, text="/help")
    bot.send(uid=77, text="/help")
    assert len(bot.sent) == 1


# ---------- voice ----------

def test_voice_as_pasted_text(bot):
    bot.say("/voice")
    bot.say("Warm and direct. No exclamation marks.")
    assert bot.store.doc["voice"]["text"] == "Warm and direct. No exclamation marks."
    assert "Saved the voice guide" in bot.last and bot.sessions[CHAT]["mode"] is None


def test_voice_as_pdf_is_transcribed_and_previewed(bot):
    bot.say("/voice")
    bot.file("tone.pdf", b"%PDF-fake")
    assert bot.ai.calls == ["transcribe"] and "Be kind" in bot.last
    assert bot.store.doc["voice"]["text"].startswith("# Voice")


def test_file_without_a_mode_is_not_guessed(bot):
    bot.file("mystery.pdf", b"x")
    assert "don't know what it is for" in bot.last and bot.store.doc == {} and bot.ai.calls == []


def test_voice_with_unsupported_file_type_explains(bot):
    bot.say("/voice")
    bot.file("tone.docx", b"x")
    assert "export Word" in bot.last and "voice" not in bot.store.doc


# ---------- examples ----------

def test_examples_from_text_messages_files_and_an_album(bot):
    bot.say("/examples")
    assert bot.store.deleted == ["examples/"]  # replaces the old set
    bot.say("First caption")
    bot.file("more.txt", b"Second\n---\nThird")
    bot.photo(b"jpg1", "aaa")
    bot.photo(b"jpg2", "bbb")
    ex = bot.store.doc["examples"]
    assert [i["type"] for i in ex] == ["text", "text", "text", "image", "image"]
    assert ex[3]["gcs_path"] == "gs://b/brand/default/examples/photo_aaa.jpg"
    assert "3 posts and 2 screenshots so far" in bot.last
    bot.say("/done")
    assert "Saved: 3 posts and 2 screenshots" in bot.last and bot.sessions[CHAT]["mode"] is None


def test_examples_add_keeps_the_existing_ones(bot):
    bot.store.doc["examples"] = [{"type": "text", "text": "old"}]
    bot.say("/examples add")
    assert bot.store.deleted == [] and bot.store.doc["examples"] == [{"type": "text", "text": "old"}]


def test_too_many_screenshots_is_refused(bot):
    bot.store.doc["examples"] = [{"type": "image", "gcs_path": f"gs://b/{i}", "name": "x"}
                                 for i in range(core.MAX_EXAMPLE_IMAGES)]
    bot.say("/examples add")
    bot.photo(b"one-too-many")
    assert "limit is 12" in bot.last and len(bot.store.doc["examples"]) == core.MAX_EXAMPLE_IMAGES


# ---------- guidelines ----------

def test_guidelines_are_shown_for_review_and_only_saved_on_command(bot):
    bot.say("/guidelines")
    bot.file("guidelines.pdf", b"%PDF-fake")
    shown = bot.last
    assert "#1F4D3A" in shown and "Lora" in shown and "NEEDS MANUAL TEMPLATE WORK" in shown
    assert "[layout_rules] Headline bottom-left" in shown
    assert "visual" not in bot.store.doc  # nothing saved yet
    bot.say("/set primary #123456")
    assert "#123456" in bot.last
    bot.say("/save")
    v = bot.store.doc["visual"]
    assert v["brand"]["primary_color"] == "#123456" and v["brand"]["font"] == "Lora"
    assert v["copy_rules"] == ["No exclamation marks in headlines"]
    assert v["source"] == "gs://b/brand/default/guidelines/guidelines.pdf" and "No logo yet" in bot.last
    assert bot.sessions[CHAT]["pending"] is None


def test_bad_correction_is_rejected_and_pending_stays_valid(bot):
    bot.say("/guidelines")
    bot.file("g.pdf", b"%PDF")
    bot.say("/set primary navy")
    assert "primary_color" in bot.last and bot.sessions[CHAT]["pending"]["brand"]["primary_color"] == "#1F4D3A"
    bot.say("/set weight bold")
    assert "use: primary" in bot.last


def test_guidelines_must_be_a_pdf(bot):
    bot.say("/guidelines")
    bot.file("guidelines.png", b"x")
    assert "must be a PDF" in bot.last and bot.ai.calls == []


def test_save_and_set_without_a_pending_proposal(bot):
    bot.say("/save")
    assert "Nothing to save" in bot.last
    bot.say("/set font Inter")
    assert "Nothing to correct" in bot.last


def test_cancel_discards_the_proposal(bot):
    bot.say("/guidelines")
    bot.file("g.pdf", b"%PDF")
    bot.say("/cancel")
    bot.say("/save")
    assert "Nothing to save" in bot.last and "visual" not in bot.store.doc


# ---------- logo, show, clear ----------

def test_logo_as_photo_or_file(bot):
    bot.say("/logo")
    bot.file("logo.png", b"\x89PNG")
    assert bot.store.doc["visual"]["brand"]["logo_url"] == "gs://b/brand/default/logo.png"
    bot.say("/logo")
    bot.photo(b"jpgdata", "lg")
    assert bot.store.doc["visual"]["brand"]["logo_url"].endswith("logo.jpg")


def test_show_summarises_what_is_saved(bot):
    bot.say("/show")
    assert "No brand context saved" in bot.last
    bot.store.doc = {"voice": {"text": "v" * 10}, "examples": [{"type": "text", "text": "hello"}]}
    bot.say("/show")
    assert "VOICE GUIDE: 10 characters" in bot.last and "1 posts, 0 screenshots" in bot.last


def test_clear_needs_confirmation(bot):
    bot.store.doc = {"voice": {"text": "x"}}
    bot.say("/clear")
    assert "confirm" in bot.last and bot.store.doc
    bot.say("/clear confirm")
    assert bot.store.doc == {} and "Deleted" in bot.last


# ---------- robustness ----------

def test_a_crash_becomes_a_readable_reply_not_a_lost_message(bot, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("gemini quota exceeded")

    bot.ai.transcribe = boom
    bot.say("/voice")
    bot.file("tone.pdf", b"%PDF")
    assert "Something went wrong" in bot.last and "gemini quota exceeded" in bot.last


def test_oversized_file_is_refused_before_download(bot):
    bot.say("/voice")
    bot.send(document={"file_id": "big", "file_name": "big.pdf", "file_size": 30 * 1024 * 1024})
    assert "over 20 MB" in bot.last


def test_command_with_bot_name_suffix_and_unknown_command(bot):
    bot.say("/help@my_smm_bot")
    assert "/voice" in bot.last
    bot.say("/dance")
    assert "don't know that command" in bot.last


def test_plain_text_without_a_mode_points_to_help(bot):
    bot.say("hello there")
    assert "/help" in bot.last


def test_long_replies_are_split_under_telegrams_limit():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    parts = main.chunks(text)
    assert len(parts) > 1 and all(len(p) <= main.MESSAGE_LIMIT for p in parts)
    assert "\n".join(parts) == text
    assert main.chunks("short") == ["short"] and main.chunks("y" * 9000) == ["y" * 4000, "y" * 4000, "y" * 1000]


def test_rtf_examples_are_converted_and_a_second_file_over_the_limit_is_refused(bot):
    rtf = (rb"{\rtf1\ansi\ansicpg1252 {\fonttbl{\f0 Times;}}\f0 Post text\par }")
    bot.say("/examples")
    bot.file("carousel1.txt", rtf)  # RTF saved with a .txt extension
    assert bot.store.doc["examples"] == [{"type": "text", "text": "Post text"}]
    big = ("y" * 4000 + "\n---\n") * 8  # 8 posts of 4,000 characters: over the 30,000 total
    bot.file("more.txt", big.encode())
    assert "characters of example text" in bot.last
    assert bot.store.doc["examples"] == [{"type": "text", "text": "Post text"}]  # nothing half-saved


def test_a_whole_document_pasted_as_one_example_is_refused(bot):
    bot.say("/examples")
    bot.file("doc.txt", ("word " * 2000).encode())
    assert "whole document" in bot.last and not bot.store.doc.get("examples")


# ---------- briefs ----------

BRAND = {"visual": {"brand": {"logo_url": "gs://b/logo.png", "primary_color": "#1f4d3a", "accent_color": "#f2b134",
                             "font": "Lora"}}, "voice": {"text": "Be kind."}}
PNG = b"\x89PNG fake"


def test_full_brief_flow_sends_only_the_campaign_fields(bot):
    bot.store.doc = json.loads(json.dumps(BRAND))
    bot.say("/brief")
    assert "Describe the campaign" in bot.last
    bot.ai.replies.append({"campaign_name": "Summer heat", "key_message": "Drink water.", "cta": "Learn more",
                           "link": "https://example.org/heat", "platforms": ["facebook", "instagram_feed", "made_up"]})
    bot.say("Campaign Summer heat, tell people to drink water, CTA learn more, facebook and instagram")
    assert "Platforms: facebook, instagram_feed" in bot.last and "made_up" not in bot.last
    assert "Tone: from the saved voice guide" in bot.last and "Background image: (none, optional)" in bot.last
    bot.file("bg.png", PNG)
    assert "Everything is there" in bot.last
    (path, data), = bot.store.uploads.items()
    assert path.startswith(f"briefs/{CHAT}/") and path.endswith("-bg.png") and data == PNG
    bot.say("/send")
    (sent,) = bot.submitted
    assert sent["campaign_name"] == "Summer heat" and sent["background_image_url"] == f"gs://b/{path}"
    assert "tone" not in sent and "brand" not in sent  # intake fills these from the saved brand context
    assert "Started job 01234567" in bot.last
    assert bot.sessions[CHAT]["brief"] is None and bot.sessions[CHAT]["mode"] is None


def test_a_later_message_changes_only_what_it_mentions(bot):
    bot.store.doc = json.loads(json.dumps(BRAND))
    bot.say("/brief")
    bot.ai.replies += [{"campaign_name": "A", "key_message": "M", "cta": "Go", "platforms": ["threads"]},
                       {"cta": "Read more", "campaign_name": None, "platforms": None}]
    bot.say("first")
    bot.say("make the CTA Read more")
    draft = bot.sessions[CHAT]["brief"]
    assert draft == {"campaign_name": "A", "key_message": "M", "cta": "Read more", "platforms": ["threads"]}
    assert '"campaign_name": "A"' in bot.ai.prompts[1]  # the model sees the current draft


def test_background_and_brand_are_optional(bot):
    bot.say("/brief")  # no saved brand context at all, and no image
    bot.ai.replies.append({"campaign_name": "A", "key_message": "M", "cta": "Go", "platforms": ["threads"],
                           "tone": "calm"})
    bot.say("details")
    assert "Everything is there" in bot.last
    bot.say("/send")
    (sent,) = bot.submitted
    assert sent == {"campaign_name": "A", "key_message": "M", "cta": "Go", "platforms": ["threads"], "tone": "calm"}
    assert "Started job" in bot.last


def test_tone_is_required_when_there_is_no_saved_voice_guide(bot):
    bot.store.doc = {"visual": BRAND["visual"]}
    bot.say("/brief")
    bot.ai.replies.append({"campaign_name": "A", "key_message": "M", "cta": "Go", "platforms": ["threads"]})
    bot.say("details")
    assert "Tone (or save a voice guide" in bot.last


def test_background_must_be_an_image(bot):
    bot.say("/brief")
    bot.file("notes.pdf", b"%PDF")
    assert "must be a .png, .jpg or .webp" in bot.last and bot.store.uploads == {}


def test_brief_survives_a_detour_and_cancel_discards_it(bot):
    bot.store.doc = json.loads(json.dumps(BRAND))
    bot.say("/brief")
    bot.ai.replies.append({"campaign_name": "A"})
    bot.say("name A")
    bot.say("/show")
    bot.say("/brief")
    assert "Campaign: A" in bot.last
    bot.say("/cancel")
    bot.say("/brief")
    assert "Describe the campaign" in bot.last


def test_send_without_a_brief_is_refused(bot):
    bot.say("/send")
    assert "no brief in progress" in bot.last


def test_intake_rejection_is_shown_to_the_user(bot, monkeypatch):
    class R:
        status_code = 400
        def json(self): return {"details": [{"field": "link", "message": "must be a valid http(s) URL"}]}

    monkeypatch.undo()  # use the real submit with a stubbed HTTP layer
    import google.oauth2.id_token as idt
    monkeypatch.setattr(idt, "fetch_id_token", lambda req, aud: "tok")
    monkeypatch.setattr(briefs.requests, "post", lambda *a, **k: R())
    monkeypatch.setenv("BRIEF_INTAKE_URL", "https://intake.example/")
    with pytest.raises(core.BrandError, match="link: must be a valid http"):
        briefs.submit({"campaign_name": "A", "link": "nope"})
