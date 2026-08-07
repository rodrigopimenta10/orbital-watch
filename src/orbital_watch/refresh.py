"""Refresh the committed data snapshot.

Run with ``uv run python -m orbital_watch.refresh``.

This is the **only** thing in the project that talks to Celestrak. The build
does not; it reads what this wrote (see
:func:`orbital_watch.sources.celestrak.fetch_group`).

That inversion is the point. The refetch floor in ``fetch`` was never real in
production: Cloudflare Pages build containers are ephemeral, so every build
started with a cold cache, the floor never applied, and each build hit
Celestrak three times. Celestrak throttled it intermittently. Concentrating
all traffic into one scheduled owner is what makes the floor mean something,
because there is finally something for it to be enforced *against*.

The snapshot is committed to the repository, so builds become reproducible
offline and the committed history doubles as a coarse time series.

Exit codes:
    0  snapshot updated, or already fresh enough to leave alone
    1  could not write the snapshot
    2  no group could be refreshed and the existing snapshot is now stale
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orbital_watch.build import configure_logging
from orbital_watch.config import (
    CACHE_DIR,
    CELESTRAK_MIN_REFETCH_INTERVAL,
    SEED_DIR,
    TLE_STALENESS,
    TRACKED_GROUPS,
)
from orbital_watch.sources import celestrak
from orbital_watch.sources.fetch import Outcome, format_duration

log = logging.getLogger("orbital_watch.refresh")

MANIFEST_NAME = "manifest.json"

#: A refreshed group must keep at least this fraction of its previous record
#: count, once the previous snapshot is big enough for the ratio to mean
#: anything. Guards against a truncated-but-valid upstream response silently
#: replacing a full snapshot.
SHRINKAGE_FLOOR = 0.5
MIN_PLAUSIBLE_RECORDS = 10


def snapshot_age(seed_dir: Path, name: str) -> timedelta | None:
    """How long ago ``name`` was captured, per the manifest."""
    captured = _captured_at(seed_dir, name)
    if captured is None:
        return None
    return datetime.now(UTC) - captured


def _captured_at(seed_dir: Path, name: str) -> datetime | None:
    try:
        manifest = json.loads((seed_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        raw = manifest["sources"][name]["retrieved_at"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def refresh_snapshot(
    *,
    seed_dir: Path = SEED_DIR,
    cache_dir: Path = CACHE_DIR,
    min_age: timedelta = CELESTRAK_MIN_REFETCH_INTERVAL,
    force: bool = False,
) -> dict:
    """Refresh every tracked Celestrak group whose snapshot is old enough.

    Returns a summary dict. Never raises on upstream failure: a group that
    cannot be refreshed keeps its previous snapshot, which then simply ages —
    and the health panel reports that age. Partial success is normal and
    correct; refusing to write the groups that *did* succeed because one
    failed would make the whole snapshot as stale as its worst member.
    """
    seed_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(seed_dir)
    _prewarm_cache(seed_dir, cache_dir, manifest)
    summary = {"refreshed": [], "skipped": [], "failed": []}

    for group in TRACKED_GROUPS:
        name = f"celestrak_{group.key}"
        age = snapshot_age(seed_dir, name)

        # A negative age means the recorded capture time is in the future
        # (clock skew, hand edit). Treating it as young would block
        # refresh until wall-clock catches up with the bogus timestamp;
        # treat it as unknown and refresh instead.
        if not force and age is not None and timedelta(0) <= age < min_age:
            log.info(
                "source=%s skipped age=%s (below the %s refetch floor)",
                name,
                format_duration(age),
                format_duration(min_age),
            )
            summary["skipped"].append(name)
            continue

        result = celestrak.fetch_group(group, cache_dir, allow_network=True)

        # Only genuine upstream contact counts as a refresh. `fetch_json`
        # helpfully falls back to cache and then to the seed, which is right
        # for a build but catastrophic here: the seed *is* the snapshot, so a
        # failed refresh would read its own output, write it back, and advance
        # the timestamp. A refresher that could never reach Celestrak again
        # would go on reporting a fresh snapshot forever — the precise species
        # of dishonesty this project exists to argue against.
        confirmed_upstream = result.outcome in (Outcome.LIVE, Outcome.NOT_MODIFIED)

        if not result.ok or not confirmed_upstream:
            log.warning(
                "source=%s refresh failed (outcome=%s, %s); keeping the "
                "existing snapshot and its original timestamp",
                name,
                result.outcome.value,
                result.error or "no upstream contact",
            )
            summary["failed"].append(name)
            continue

        # Trim before writing. The snapshot only needs what the build will
        # actually propagate, and Starlink is ~11k objects / 5 MB unfiltered --
        # far too much to commit on every refresh cycle.
        records = celestrak.select_objects(result.data, group)

        # A payload that validates but has collapsed in size -- 2 stations
        # where the snapshot holds 22 -- is upstream trouble, not a real
        # constellation change. Constellations do not lose most of their
        # objects between refreshes; refuse to overwrite substantial data
        # with a fraction of itself, keep the old snapshot, and let its age
        # tell the story if the shrinkage persists.
        previous = _previous_record_count(manifest, name)
        if (
            previous is not None
            and previous >= MIN_PLAUSIBLE_RECORDS
            and len(records) < previous * SHRINKAGE_FLOOR
        ):
            log.warning(
                "source=%s payload shrank from %d to %d records "
                "(below the %d%% floor); keeping the existing snapshot",
                name,
                previous,
                len(records),
                int(SHRINKAGE_FLOOR * 100),
            )
            summary["failed"].append(name)
            continue

        _write_source(seed_dir, name, records)

        # Record when we last *confirmed currency with upstream*, not when the
        # bytes were first obtained. Celestrak answers NOT_MODIFIED when we
        # already hold its newest element sets, and in that case the payload
        # timestamp is the original download -- possibly many hours old. Using
        # it would age a snapshot that upstream has just told us is current,
        # and the site would report STALE while holding the freshest data that
        # exists. What the health panel is really asking is "how long since we
        # knew this was current", and that answer is now.
        #
        # This does not paper over genuinely old elements: the sky view reports
        # each satellite's own element-set age from its EPOCH field separately.
        confirmed = datetime.now(UTC)
        unchanged = result.outcome is Outcome.NOT_MODIFIED
        manifest["sources"][name] = {
            "retrieved_at": confirmed.isoformat(),
            "records": len(records),
            "available": len(result.data),
            "upstream_unchanged": unchanged,
            "payload_first_seen": (
                result.last_success.isoformat() if result.last_success else None
            ),
        }
        log.info(
            "source=%s refreshed records=%d of %d available (%s)",
            name,
            len(records),
            len(result.data),
            "upstream unchanged since last download" if unchanged else "new payload",
        )
        summary["refreshed"].append(name)

    if summary["refreshed"]:
        manifest["captured_at"] = datetime.now(UTC).isoformat()
        _write_manifest(seed_dir, manifest)

    return summary


def _previous_record_count(manifest: dict, name: str) -> int | None:
    entry = manifest["sources"].get(name)
    if not isinstance(entry, dict):
        return None
    count = entry.get("records")
    return count if isinstance(count, int) and count >= 0 else None


def _load_manifest(seed_dir: Path) -> dict:
    """Load the manifest, tolerating any shape of damage.

    A manifest that is valid JSON but the wrong shape (a list, a string, a
    ``sources`` that is not a dict) must degrade to empty exactly like invalid
    JSON does. Without the isinstance checks, a hand-edit or partial write
    crashed the refresher with a bare TypeError -- and because nothing repairs
    the file, *every subsequent scheduled run crashed identically* while the
    snapshot aged toward FAILED. The one component whose job is keeping data
    fresh must never be wedged permanently by a malformed state file.
    """
    try:
        manifest = json.loads((seed_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    if not isinstance(manifest, dict):
        log.warning("manifest is %s, not a dict; starting fresh", type(manifest).__name__)
        manifest = {}
    if not isinstance(manifest.get("sources"), dict):
        if "sources" in manifest:
            log.warning("manifest 'sources' is not a dict; discarding it")
        manifest["sources"] = {}
    return manifest


def _prewarm_cache(seed_dir: Path, cache_dir: Path, manifest: dict) -> None:
    """Seed the fetch cache from the committed snapshot.

    CI runners are ephemeral, so the fetch cache is cold on every run -- and
    Celestrak's "has not updated since your last successful download" sentinel
    is tracked per IP, which for shared CI egress can fire even on this
    runner's first contact. Against a cold cache that sentinel has nothing to
    serve and resolves to FAILED, turning "you already hold the newest data"
    into a dropped refresh cycle while seed/ holds exactly the data upstream
    just confirmed. Pre-warming makes the sentinel resolve to NOT_MODIFIED,
    which the loop below correctly counts as confirmation.

    The cache timestamps use the manifest's payload_first_seen (or
    retrieved_at) so nothing gets younger than it truly is.
    """
    for group in TRACKED_GROUPS:
        name = f"celestrak_{group.key}"
        src = seed_dir / f"{name}.json"
        dst = cache_dir / f"{name}.json"
        if not src.exists() or dst.exists():
            continue
        entry = manifest["sources"].get(name)
        stamp = None
        if isinstance(entry, dict):
            stamp = entry.get("payload_first_seen") or entry.get("retrieved_at")
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            if stamp:
                dst.with_suffix(".json.meta").write_text(
                    json.dumps({"retrieved_at": stamp}), encoding="utf-8"
                )
        except OSError as exc:
            log.warning("cache pre-warm failed for %s: %s", name, exc)


def _write_source(seed_dir: Path, name: str, records: list) -> None:
    # Compact separators: this is committed on a schedule, so every byte shows
    # up in the repository forever.
    path = seed_dir / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _write_manifest(seed_dir: Path, manifest: dict) -> None:
    path = seed_dir / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _worst_age(seed_dir: Path) -> timedelta | None:
    ages = [snapshot_age(seed_dir, f"celestrak_{group.key}") for group in TRACKED_GROUPS]
    known = [a for a in ages if a is not None]
    return max(known) if known else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orbital_watch.refresh",
        description="Refresh the committed Celestrak snapshot that builds read.",
    )
    parser.add_argument("--seed-dir", type=Path, default=SEED_DIR)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh even if the snapshot is inside the refetch floor.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    try:
        summary = refresh_snapshot(
            seed_dir=args.seed_dir, cache_dir=args.cache_dir, force=args.force
        )
    except OSError:
        log.exception("refresh failed: could not write the snapshot")
        return 1

    log.info(
        "refresh complete refreshed=%d skipped=%d failed=%d",
        len(summary["refreshed"]),
        len(summary["skipped"]),
        len(summary["failed"]),
    )

    # Only a problem if nothing was refreshed *and* what we have has gone stale.
    # A run where everything was skipped for being young is a success.
    if not summary["refreshed"] and summary["failed"]:
        worst = _worst_age(args.seed_dir)
        if worst is not None and worst > TLE_STALENESS.fresh_within:
            log.error(
                "no group could be refreshed and the snapshot is now %s old, "
                "past the %s freshness window",
                format_duration(worst),
                format_duration(TLE_STALENESS.fresh_within),
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
