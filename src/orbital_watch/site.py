"""Assemble the static site: the frontend plus a baked copy of the document.

Run with ``uv run python -m orbital_watch.site --document latest.json
--live-url https://storage.googleapis.com/<bucket>/latest.json``.

The site that gets deployed is the contents of ``web/`` and two small
additions:

* ``data/latest.json`` -- the document as it was when the site was built. The
  page reads the live document from the public bucket; if that cannot be
  reached, it shows this copy instead and says so. It is a fallback, and the
  page treats it as one: it is never presented as current.
* ``assets/config.js`` -- where the live document is. The address differs
  between a deployment and a laptop, and is not known to ``web/``.

Nothing here fetches anything. The deploy workflow downloads the document it
wants baked in and passes the file.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from orbital_watch.config import (
    BAKED_DOCUMENT_PATH,
    BUILD_DIR,
    LOCAL_LIVE_DOCUMENT_PATH,
    WEB_DIR,
)
from orbital_watch.document import validate
from orbital_watch.pipeline import SOURCE_LABELS

log = logging.getLogger("orbital_watch.site")

CONFIG_PATH = "assets/config.js"


def _remove_path(path: Path) -> None:
    """Remove ``path`` whether it is a directory, a file, or absent."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def render_config(live_url: str) -> str:
    """The contents of ``assets/config.js``.

    A script file rather than an inline block so the Content-Security-Policy
    can stay ``script-src 'self'``. The values go through ``json.dumps``, so
    whatever is passed in arrives in the browser as a string and nothing else.
    """
    settings = {
        "liveDocumentUrl": live_url,
        "bakedDocumentUrl": BAKED_DOCUMENT_PATH,
    }
    return (
        "/* Written by orbital_watch.site when the site is assembled. */\n"
        f"window.ORBITAL_WATCH_CONFIG = {json.dumps(settings, indent=2)};\n"
    )


def build_site(
    *,
    document: bytes,
    live_url: str = LOCAL_LIVE_DOCUMENT_PATH,
    out_dir: Path = BUILD_DIR,
    web_dir: Path = WEB_DIR,
    local_live_copy: bool = False,
) -> None:
    """Write the site into ``out_dir``, replacing what was there in one step.

    ``local_live_copy`` also places the document at the local stand-in for the
    public bucket, so that a site served from a laptop has a "live" document
    to load and a baked one to fall back to, like the deployed one.

    Raises ``ValueError`` if the document is not one the page can render, and
    ``OSError`` if the output cannot be written. In both cases ``out_dir`` is
    left exactly as it was.
    """
    try:
        parsed = json.loads(document)
    except ValueError as exc:
        raise ValueError(f"the document to bake is not JSON: {exc}") from exc
    problems = validate(parsed, expected_sources=SOURCE_LABELS)
    if problems:
        raise ValueError("refusing to bake a malformed document: " + "; ".join(problems))
    if not web_dir.is_dir():
        raise OSError(f"frontend source not found at {web_dir}")

    # Assemble the whole site in a staging directory beside the target, then
    # swap it into place. Writing into out_dir directly would leave a window
    # in which it holds a mixture of this build and the last, and anything
    # reading it then -- an upload step, a local server -- would publish that
    # mixture.
    out_dir = Path(out_dir)
    staging = out_dir.parent / (out_dir.name + ".staging")
    _remove_path(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(web_dir, staging)

    # Generated files second, so nothing in web/ can shadow them.
    targets = [staging / BAKED_DOCUMENT_PATH]
    if local_live_copy:
        targets.append(staging / LOCAL_LIVE_DOCUMENT_PATH)
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(document)
    (staging / CONFIG_PATH).write_text(render_config(live_url), encoding="utf-8")

    required = [staging / "index.html", staging / CONFIG_PATH, *targets]
    missing = [
        str(path) for path in required if not path.is_file() or not path.stat().st_size
    ]
    if missing:
        _remove_path(staging)
        raise OSError(f"site incomplete, refusing to replace {out_dir}: {missing}")

    # Swap via a retired name rather than delete-then-move, so the window in
    # which out_dir does not exist is the gap between two renames.
    retired = out_dir.parent / (out_dir.name + ".previous")
    _remove_path(retired)
    if out_dir.exists() or out_dir.is_symlink():
        out_dir.replace(retired)
    staging.replace(out_dir)
    _remove_path(retired)
    log.info("site written to %s (live document: %s)", out_dir, live_url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orbital_watch.site",
        description="Assemble the static site around a copy of the published document.",
    )
    parser.add_argument(
        "--document",
        type=Path,
        required=True,
        help="The document to bake into the site as its fallback copy.",
    )
    parser.add_argument(
        "--live-url",
        required=True,
        help="Where the page should load the live document from.",
    )
    parser.add_argument("--out", type=Path, default=BUILD_DIR)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    try:
        build_site(
            document=args.document.read_bytes(),
            live_url=args.live_url,
            out_dir=args.out,
        )
    except (OSError, ValueError):
        log.exception("site build failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
