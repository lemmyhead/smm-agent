"""brandctx: manage the brand context that the drafter and brief-intake read from Firestore (`brand/<profile>`).

  voice <file>          tone-of-voice guide (.md/.txt, or a PDF that Gemini transcribes to markdown)
  examples <files...>   example posts: text files (posts separated by a line of ---) and/or screenshots
  extract <pdf>         Gemini reads the visual-guidelines PDF and writes a proposal.json to review
  apply <proposal.json> saves the reviewed proposal as the default brand block
  logo <image>          uploads the logo and sets it as the default logo
  show / clear

The same operations are available in the Telegram bot (telegram-intake). Raw files go to
gs://smm-agent-assets-<PROJECT>/brand/<profile>/. Runs on your machine with your own Application Default Credentials."""
import argparse
import json
import os
import sys
from pathlib import Path

import brandcore as core
from brandcore import BrandError, Gemini, Store  # noqa: F401  (Store/Gemini are patched or built in main)


def read_file(path: str) -> tuple[str, bytes]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise BrandError(f"{path} is not a file")
    return p.name, p.read_bytes()


def cmd_voice(store, ai, path: str) -> None:
    name, data = read_file(path)
    if core.ext_of(name) == ".pdf":
        print("Gemini is transcribing the PDF ...")
    text = core.save_voice(store, ai, name, data)
    print(f"Saved the voice guide ({len(text):,} characters). Start of what the drafter will read:\n")
    print(text[:800] + ("\n[...]" if len(text) > 800 else ""))
    print("\nRun `show --full` to read all of it. To fix anything, edit your file and run this command again.")


def cmd_examples(store, paths: list[str], add: bool) -> None:
    items = (store.get().get("examples") or []) if add else []
    new = []
    if not add:
        store.delete_prefix("examples/")
    for path in paths:
        new += core.example_items(store, *read_file(path))
    items += new
    core.check_examples(items)
    store.merge({"examples": items}, ["examples"])
    images = sum(1 for i in items if i["type"] == "image")
    print(f"Saved {len(items) - images} example posts and {images} screenshots.")


def cmd_extract(store, ai, path: str, out: str) -> None:
    name, data = read_file(path)
    print("Gemini is reading the guidelines ...")
    proposal = core.extract_proposal(ai, name, data, str(Path(path).expanduser().resolve()))
    Path(out).write_text(json.dumps(proposal, indent=2, ensure_ascii=False) + "\n")
    print("\n" + core.format_proposal(proposal))
    print(f"\nWrote {out}. Fix anything that is wrong in that file, then run:  apply {out}")
    for p in core.validate_proposal(proposal):
        print(f"  ! {p}")


def cmd_apply(store, path: str) -> None:
    p = json.loads(read_file(path)[1])
    source_uri = None
    src = Path(p.get("source_pdf") or "")
    if src.is_file():
        source_uri = store.upload(f"guidelines/{core.clean_name(src.name)}", src.read_bytes(), "application/pdf")
    has_logo = core.apply_proposal(store, p, source_uri)
    print("Saved. Briefs without a brand block now use these colours and font.")
    if not has_logo:
        print("No logo yet: run `logo <file>`. Until then a brief must supply brand.logo_url itself.")


def cmd_logo(store, path: str) -> None:
    print(f"Logo saved as the default: {core.save_logo(store, *read_file(path))}")


def cmd_show(store, full: bool) -> None:
    print(core.format_summary(store.get(), store.profile, full))


def cmd_clear(store, yes: bool) -> None:
    if not yes:
        raise BrandError(f"this deletes brand/{store.profile} and its files under gs://{store.bucket_name}/brand/{store.profile}/. "
                         "Add --yes to confirm.")
    store.delete_profile()
    print(f"Deleted brand context {store.profile!r}.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="brandctx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=os.environ.get("PROJECT_ID"), help="GCP project id (default: $PROJECT_ID)")
    ap.add_argument("--profile", default=os.environ.get("BRAND_PROFILE", "default"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("voice").add_argument("file")
    e = sub.add_parser("examples")
    e.add_argument("files", nargs="+")
    e.add_argument("--add", action="store_true", help="keep the existing examples and add these")
    x = sub.add_parser("extract")
    x.add_argument("pdf")
    x.add_argument("--out", default="brand-proposal.json")
    sub.add_parser("apply").add_argument("proposal")
    sub.add_parser("logo").add_argument("file")
    s = sub.add_parser("show")
    s.add_argument("--full", action="store_true")
    sub.add_parser("clear").add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if not a.project:
        ap.error("no project: set PROJECT_ID or pass --project")

    store = Store(a.project, a.profile)
    ai = lambda: Gemini(a.project)  # noqa: E731  (only built for commands that call Gemini)
    try:
        match a.cmd:
            case "voice": cmd_voice(store, ai, a.file)
            case "examples": cmd_examples(store, a.files, a.add)
            case "extract": cmd_extract(store, ai, a.pdf, a.out)
            case "apply": cmd_apply(store, a.proposal)
            case "logo": cmd_logo(store, a.file)
            case "show": cmd_show(store, a.full)
            case "clear": cmd_clear(store, a.yes)
    except BrandError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
