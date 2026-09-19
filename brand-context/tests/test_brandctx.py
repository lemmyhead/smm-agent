import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brandcore as core  # noqa: E402
import brandctx  # noqa: E402


class FakeStore:
    profile = "default"
    bucket_name = "b"

    def __init__(self, doc=None):
        self.doc = doc or {}
        self.uploads = {}
        self.deleted_prefixes = []
        self.profile_deleted = False

    def get(self):
        return json.loads(json.dumps(self.doc))

    def merge(self, fields, paths):
        for path in paths:  # emulate Firestore's field-path merge (dotted paths create nested maps)
            top, *rest = path.split(".")
            if not rest:
                self.doc[top] = fields[top]
                continue
            node, src = self.doc.setdefault(top, {}), fields[top]
            for key in rest[:-1]:
                node, src = node.setdefault(key, {}), src[key]
            node[rest[-1]] = src[rest[-1]]

    def append_examples(self, items, check):
        merged = self.doc.get("examples", []) + items
        check(merged)
        self.doc["examples"] = merged
        return merged

    def upload(self, name, data, content_type):
        self.uploads[name] = data
        return f"gs://b/brand/default/{name}"

    def delete_prefix(self, prefix):
        self.deleted_prefixes.append(prefix)

    def delete_profile(self):
        self.profile_deleted = True


class FakeAi:
    def __init__(self, text="# Voice\nBe kind.", extraction=None):
        self.text, self.extraction = text, extraction
        self.calls = []

    def transcribe(self, pdf):
        self.calls.append("transcribe")
        return self.text

    def extract_visual(self, pdf):
        self.calls.append("extract")
        return self.extraction


EXTRACTION = core.Extraction(
    primary_color="#12355b", accent_color="#ffb703", font="Inter", font_notes="Headlines bold.",
    palette=["Navy #12355b: main", "Sun #ffb703: buttons"],
    logo_rules=["Keep 20px clear space around the logo"],
    layout_rules=["Headline sits bottom-left"], imagery_rules=["Warm natural light"],
    copy_rules=["Sentence case in headlines"])


def die_message(capsys, fn, *a, **k):
    with pytest.raises(core.BrandError) as e:
        fn(*a, **k)
    return str(e.value)


def test_split_examples():
    assert core.split_examples("one\n\n---\n two \n---\n\n") == ["one", "two"]
    assert core.split_examples("just one post") == ["just one post"]


def test_hex_helpers():
    assert core.hex_ok("#abc") and core.hex_ok("#A1B2C3")
    assert not core.hex_ok("red") and not core.hex_ok("") and not core.hex_ok("#12345")


def test_voice_from_text_file_is_stored_and_editable_by_rerunning(tmp_path):
    f = tmp_path / "voice.md"
    f.write_text("Warm. Direct.\nNo jargon.")
    store, ai = FakeStore(), FakeAi()
    brandctx.cmd_voice(store, lambda: ai, str(f))
    assert store.doc["voice"]["text"] == "Warm. Direct.\nNo jargon." and ai.calls == []
    assert store.doc["voice"]["source"].endswith("voice/voice.md") and "voice/voice.md" in store.uploads


def test_voice_from_pdf_is_transcribed_by_gemini(tmp_path):
    f = tmp_path / "tone.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    store, ai = FakeStore(), FakeAi()
    brandctx.cmd_voice(store, lambda: ai, str(f))
    assert ai.calls == ["transcribe"] and store.doc["voice"]["text"].startswith("# Voice")


def test_voice_rejects_unknown_type_and_oversized_text(tmp_path, capsys):
    doc = tmp_path / "v.docx"
    doc.write_bytes(b"x")
    assert "export Word" in die_message(capsys, brandctx.cmd_voice, FakeStore(), lambda: FakeAi(), str(doc))
    big = tmp_path / "big.txt"
    big.write_text("x" * (core.MAX_VOICE_CHARS + 1))
    store = FakeStore()
    assert "limit" in die_message(capsys, brandctx.cmd_voice, store, lambda: FakeAi(), str(big))
    assert "voice" not in store.doc  # nothing half-saved


def test_examples_text_and_screenshots_replace_then_add(tmp_path):
    posts = tmp_path / "posts.txt"
    posts.write_text("Post one\n---\nPost two")
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG")
    store = FakeStore({"examples": [{"type": "text", "text": "old"}]})
    brandctx.cmd_examples(store, [str(posts), str(shot)], add=False)
    assert store.deleted_prefixes == ["examples/"]  # replaced
    assert [i["type"] for i in store.doc["examples"]] == ["text", "text", "image"]
    assert store.doc["examples"][2]["gcs_path"] == "gs://b/brand/default/examples/shot.png"
    brandctx.cmd_examples(store, [str(posts)], add=True)
    assert len(store.doc["examples"]) == 5 and store.deleted_prefixes == ["examples/"]


def test_examples_enforces_limits_and_types(tmp_path, capsys):
    shots = []
    for i in range(core.MAX_EXAMPLE_IMAGES + 1):
        f = tmp_path / f"s{i}.png"
        f.write_bytes(b"x")
        shots.append(str(f))
    store = FakeStore()
    assert "limit" in die_message(capsys, brandctx.cmd_examples, store, shots, False)
    assert "examples" not in store.doc
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"x")
    assert "must be" in die_message(capsys, brandctx.cmd_examples, FakeStore(), [str(pdf)], False)


def test_build_proposal_flags_manual_work_and_missing_font():
    p = core.build_proposal(EXTRACTION, font_status=False, source="/x/g.pdf")
    manual = p["manual_template_work"]
    assert manual["layout_rules"] == ["Headline sits bottom-left"] and manual["logo_rules"]
    assert "Google Fonts" in manual["font"] and p["font_check"] == "NOT on Google Fonts"
    assert "font" not in core.build_proposal(EXTRACTION, True, "/x/g.pdf")["manual_template_work"]


def test_extract_writes_reviewable_proposal(tmp_path, monkeypatch, capsys):
    pdf = tmp_path / "g.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(core, "font_on_google_fonts", lambda name: True)
    out = tmp_path / "proposal.json"
    brandctx.cmd_extract(FakeStore(), lambda: FakeAi(extraction=EXTRACTION), str(pdf), str(out))
    proposal = json.loads(out.read_text())
    assert proposal["brand"] == {"primary_color": "#12355b", "accent_color": "#ffb703", "font": "Inter"}
    shown = capsys.readouterr().out
    assert "NEEDS MANUAL TEMPLATE WORK" in shown and "[layout_rules] Headline sits bottom-left" in shown


def test_apply_saves_brand_block_and_keeps_an_earlier_logo(tmp_path):
    pdf = tmp_path / "g.pdf"
    pdf.write_bytes(b"%PDF")
    proposal = core.build_proposal(EXTRACTION, True, str(pdf))
    f = tmp_path / "p.json"
    f.write_text(json.dumps(proposal))
    store = FakeStore({"visual": {"brand": {"logo_url": "gs://b/brand/default/logo.png"}}})
    brandctx.cmd_apply(store, str(f))
    v = store.doc["visual"]
    assert v["brand"] == {"primary_color": "#12355b", "accent_color": "#ffb703", "font": "Inter",
                          "logo_url": "gs://b/brand/default/logo.png"}
    assert v["copy_rules"] == ["Sentence case in headlines"] and v["source"].endswith("guidelines/g.pdf")


def test_apply_rejects_a_bad_colour_and_saves_nothing(tmp_path, capsys):
    proposal = core.build_proposal(EXTRACTION, True, "/none.pdf")
    proposal["brand"]["primary_color"] = "navy"
    f = tmp_path / "p.json"
    f.write_text(json.dumps(proposal))
    store = FakeStore()
    assert "primary_color" in die_message(capsys, brandctx.cmd_apply, store, str(f))
    assert store.doc == {}


def test_logo_sets_only_the_logo_url(tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG")
    store = FakeStore({"visual": {"brand": {"font": "Inter"}, "copy_rules": ["x"]}})
    brandctx.cmd_logo(store, str(logo))
    assert store.doc["visual"]["brand"] == {"font": "Inter", "logo_url": "gs://b/brand/default/logo.png"}
    assert store.doc["visual"]["copy_rules"] == ["x"]


def test_clear_needs_confirmation(capsys):
    store = FakeStore()
    die_message(capsys, brandctx.cmd_clear, store, False)
    assert not store.profile_deleted
    brandctx.cmd_clear(store, True)
    assert store.profile_deleted


def test_font_check_handles_unusable_names():
    assert core.font_on_google_fonts("") is None
    assert core.font_on_google_fonts("Bad;Name") is False


def _rtf(body: str) -> bytes:
    """A minimal RTF document; non-ASCII characters are written as \\uNNNN? escapes, as TextEdit does."""
    esc = "".join(c if ord(c) < 128 else "\\u%d?" % ord(c) for c in body)
    return ("{\\rtf1\\ansi\\ansicpg1252 {\\fonttbl{\\f0 Times;}}\\f0\\fs24 " + esc + "}").encode("ascii")


RTF = _rtf("Never shout.\\par \u041f\u0440\u0438\u0432\u0456\u0442 friend")


def test_rtf_is_converted_to_plain_text_even_when_named_txt():
    text = core.read_text(RTF)
    assert "Never shout." in text and "\u041f\u0440\u0438\u0432\u0456\u0442 friend" in text and "rtf" not in text and "{" not in text
    assert core.read_text("plain text".encode()) == "plain text"


def test_voice_and_examples_accept_rtf(tmp_path):
    f = tmp_path / "voice.txt"  # a rich-text file saved with a .txt extension (this happened for real)
    f.write_bytes(RTF)
    store = FakeStore()
    brandctx.cmd_voice(store, lambda: FakeAi(), str(f))
    assert store.doc["voice"]["text"].startswith("Never shout.")
    g = tmp_path / "post.rtf"
    g.write_bytes(RTF)
    brandctx.cmd_examples(store, [str(g)], add=False)
    assert store.doc["examples"][0]["text"].startswith("Never shout.")


def test_one_giant_example_is_refused_as_a_document_not_a_post(tmp_path, capsys):
    f = tmp_path / "all.txt"
    f.write_text("x" * (core.MAX_POST_CHARS + 1))
    store = FakeStore()
    msg = die_message(capsys, brandctx.cmd_examples, store, [str(f)], False)
    assert "whole document" in msg and "---" in msg and "examples" not in store.doc
