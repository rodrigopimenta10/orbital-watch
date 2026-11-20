"""The scheduled job: one refresh cycle, then publish.

Run with ``uv run python -m orbital_watch.job``.

In production this is the entry point of a container that Cloud Scheduler
starts every ten minutes as a Cloud Run job. State and the published document
live in two Cloud Storage buckets, named by environment variables. With those
variables unset the same code keeps both in a local directory and also
assembles the site into ``dist/``, so the whole system runs on a laptop with
no cloud account at all.

Exit codes are chosen so that a failed execution means something:

    0  a truthful document is published -- by this run, or by a newer
       overlapping run that this one deferred to. Upstream sources being
       down does NOT make the run fail: the document says they are down,
       and saying so is the job working.
    1  the document could not be published (storage unreachable or refusing)
    2  an unexpected error in our own code
    3  the document failed validation and was withheld; the previous one
       is still published

Anything non-zero pages the owner, so none of them is used for a condition
the page already reports honestly on its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from orbital_watch.config import (
    BUILD_DIR,
    DOCUMENT_KEY,
    LOCAL_DIR,
    LOCAL_LIVE_DOCUMENT_PATH,
    PUBLIC_BUCKET_ENV,
    STATE_BUCKET_ENV,
)
from orbital_watch.document import MalformedDocument, publish
from orbital_watch.pipeline import SOURCE_LABELS, build_document
from orbital_watch.store import GcsStore, LocalStore, Store, StoreError

log = logging.getLogger("orbital_watch.job")

EXIT_OK = 0
EXIT_NOT_PUBLISHED = 1
EXIT_INTERNAL_ERROR = 2
EXIT_MALFORMED = 3


class _CloudLoggingFormatter(logging.Formatter):
    """One JSON object per line, in the form Cloud Logging reads severities from.

    Plain text on stderr would all arrive at one severity. Written this way,
    a WARNING about a degraded source and an ERROR about a failed publish
    can be told apart and filtered in the log viewer.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            entry["message"] += "\n" + self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(verbose: bool = False) -> None:
    """Levelled logging: JSON lines on Cloud Run, readable text elsewhere."""
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("CLOUD_RUN_JOB"):
        handler.setFormatter(_CloudLoggingFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, handlers=[handler], force=True
    )


def stores_from_environment(local_dir: Path) -> tuple[Store, Store, bool]:
    """Pick the state store and the public store. Returns (state, public, local).

    Both bucket variables set means Cloud Storage; neither means a local
    directory. One without the other is a deployment mistake, and is refused
    rather than half-honoured: publishing to a real bucket from throwaway
    local state would break the politeness floor.
    """
    state_bucket = os.environ.get(STATE_BUCKET_ENV, "").strip()
    public_bucket = os.environ.get(PUBLIC_BUCKET_ENV, "").strip()
    if state_bucket and public_bucket:
        # The published document is public and cacheable, so reads of it are
        # pinned to a generation; state is private and never cached.
        return GcsStore(state_bucket), GcsStore(public_bucket, pinned_reads=True), False
    if state_bucket or public_bucket:
        raise ValueError(
            f"set both {STATE_BUCKET_ENV} and {PUBLIC_BUCKET_ENV}, or neither "
            f"(neither runs against the local directory {local_dir})"
        )
    return LocalStore(local_dir / "state"), LocalStore(local_dir / "public"), True


def run(state_store: Store, public_store: Store) -> int:
    """One cycle against the given stores. Returns an exit code; never raises."""
    try:
        document = build_document(state_store)
    except Exception:
        # Every upstream failure is handled deeper in, so anything reaching
        # here is a bug in our own code. Nothing was published; the previous
        # document stands.
        log.exception("run failed: unexpected internal error; nothing published")
        return EXIT_INTERNAL_ERROR

    try:
        outcome = publish(document, public_store, expected_sources=SOURCE_LABELS)
    except MalformedDocument as exc:
        log.error(
            "run failed: the document failed validation and was NOT published; "
            "the previous document is still in place. Problems: %s",
            "; ".join(exc.problems),
        )
        return EXIT_MALFORMED
    except StoreError:
        log.exception("run failed: could not publish the document")
        return EXIT_NOT_PUBLISHED
    except Exception:
        log.exception("run failed: unexpected error while publishing")
        return EXIT_INTERNAL_ERROR

    log.info(
        "run complete outcome=%s health=%s",
        outcome,
        document["health"]["overall"],
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orbital_watch.job",
        description=(
            "Run one refresh cycle and publish the document. Uses Cloud Storage "
            f"when {STATE_BUCKET_ENV} and {PUBLIC_BUCKET_ENV} are set, and a "
            "local directory otherwise."
        ),
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=LOCAL_DIR,
        help="Where a local run keeps its state and its published document.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=BUILD_DIR,
        help="Where a local run assembles the site.",
    )
    parser.add_argument(
        "--no-site",
        action="store_true",
        help="Local runs only: publish the document but do not assemble the site.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    try:
        state_store, public_store, local = stores_from_environment(args.local_dir)
    except Exception:
        log.exception("run failed: storage is not configured correctly")
        return EXIT_NOT_PUBLISHED
    log.info(
        "state in %s, document published to %s",
        state_store.describe(),
        public_store.describe(),
    )

    code = run(state_store, public_store)
    if code != EXIT_OK or not local or args.no_site:
        return code

    # Local convenience only: put the frontend and the document side by side
    # so `python3 -m http.server --directory dist` shows the result.
    from orbital_watch.site import build_site

    try:
        published = public_store.read(DOCUMENT_KEY)
        if published is None:
            raise StoreError("the document just published could not be read back")
        build_site(
            document=published.data,
            live_url=LOCAL_LIVE_DOCUMENT_PATH,
            out_dir=args.out,
            local_live_copy=True,
        )
    except (OSError, ValueError, StoreError):
        log.exception("the document was published, but the local site was not assembled")
        return EXIT_NOT_PUBLISHED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
