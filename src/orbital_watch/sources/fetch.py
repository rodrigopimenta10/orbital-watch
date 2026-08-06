"""The one place network I/O happens.

Every upstream read in this project goes through :func:`fetch_json`, which
guarantees the same four things regardless of caller:

1. The call has a hard timeout.
2. Failure is caught, logged with source and elapsed time, and falls back to
   the on-disk cache.
3. The outcome is recorded in a result object the health panel can report
   truthfully -- including *why* a source degraded.
4. It never raises. A caller cannot accidentally abort the build by forgetting
   a try/except, because there is nothing to catch.

This is deliberately a single choke point rather than scattered try/except
blocks: it means "what happens when upstream is down" has exactly one answer,
and that answer is testable in isolation.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from orbital_watch.config import HTTP_TIMEOUT_SECONDS, USER_AGENT

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    """How a fetch actually resolved."""

    #: Fetched fresh bytes from upstream.
    LIVE = "live"
    #: Upstream said nothing has changed; we reused cache. Not a degradation.
    NOT_MODIFIED = "not_modified"
    #: We chose not to call upstream because the cache was young enough.
    CACHE_FRESH = "cache_fresh"
    #: Upstream failed; we served cache instead. Degraded but serving.
    CACHE_FALLBACK = "cache_fallback"
    #: Upstream failed and there was no cache. No data at all.
    FAILED = "failed"

    @property
    def is_degraded(self) -> bool:
        return self in (Outcome.CACHE_FALLBACK, Outcome.FAILED)


@dataclass
class FetchResult:
    """Everything downstream code needs to know about one upstream read.

    ``data`` is ``None`` only in the :attr:`Outcome.FAILED` case. Callers are
    expected to check it and degrade rather than assume.
    """

    name: str
    url: str
    data: Any | None
    outcome: Outcome
    #: When the payload we are actually serving was retrieved from upstream.
    #: For a cache hit this is the cache's write time, not "now" -- that
    #: distinction is the entire point of the health panel.
    last_success: datetime | None
    elapsed_seconds: float
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.data is not None

    @property
    def age(self) -> timedelta | None:
        if self.last_success is None:
            return None
        return datetime.now(UTC) - self.last_success


# A Celestrak quirk worth spelling out: when you re-request a GP group inside
# its 2-hour refresh window, it answers HTTP 200 with a plaintext body reading
# "GP data has not updated since your last successful download...". It is not
# JSON and it is not an error status. Parsed naively it either crashes the
# build or -- much worse -- overwrites a good cache with a sentence. We detect
# it and treat it as not-modified.
_NOT_UPDATED_MARKER = "has not updated since your last successful"


class _CacheEntry:
    """A cached payload plus the time it was originally retrieved."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.meta_path = path.with_suffix(path.suffix + ".meta")

    def read(self) -> tuple[Any | None, datetime | None]:
        if not self.path.exists():
            return None, None
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cache read failed path=%s error=%s", self.path, exc)
            return None, None
        return data, self._read_timestamp()

    def _read_timestamp(self) -> datetime | None:
        try:
            meta = json.loads(self.meta_path.read_text())
            return datetime.fromisoformat(meta["retrieved_at"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            # Fall back to mtime; less precise but better than claiming nothing.
            try:
                ts = self.path.stat().st_mtime
            except OSError:
                return None
            return datetime.fromtimestamp(ts, tz=UTC)

    def write(self, data: Any, retrieved_at: datetime) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write via a temp file so an interrupted build cannot leave a
            # half-written cache that poisons the next run.
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.path)
            self.meta_path.write_text(
                json.dumps({"retrieved_at": retrieved_at.isoformat()})
            )
        except OSError as exc:
            log.warning("cache write failed path=%s error=%s", self.path, exc)

    def age(self) -> timedelta | None:
        ts = self._read_timestamp() if self.path.exists() else None
        if ts is None:
            return None
        return datetime.now(UTC) - ts


def fetch_json(
    name: str,
    url: str,
    cache_dir: Path,
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    min_refetch_interval: timedelta | None = None,
    parser: Any = None,
) -> FetchResult:
    """Fetch JSON from ``url``, falling back to disk cache on any failure.

    Never raises. Always returns a :class:`FetchResult`.

    Args:
        name: Stable identifier for this source, used in logs and health output.
        url: Absolute URL to GET.
        cache_dir: Directory holding the disk cache.
        timeout: Hard per-call timeout in seconds.
        min_refetch_interval: If the cache is younger than this, skip the
            network call entirely. This is how we respect Celestrak's rate
            limits independently of how often the build runs.
        parser: Optional callable applied to the decoded body. Raise
            :class:`ValueError` inside it to reject a malformed payload.
    """
    cache = _CacheEntry(cache_dir / f"{name}.json")
    started = time.monotonic()

    # --- Rate-limit guard: don't even call upstream if cache is young enough.
    if min_refetch_interval is not None:
        cached_age = cache.age()
        if cached_age is not None and cached_age < min_refetch_interval:
            data, retrieved = cache.read()
            if data is not None:
                log.info(
                    "source=%s outcome=cache_fresh age=%s elapsed=%.3fs "
                    "(within min refetch interval %s, no upstream call)",
                    name,
                    format_duration(cached_age),
                    time.monotonic() - started,
                    format_duration(min_refetch_interval),
                )
                return FetchResult(
                    name=name,
                    url=url,
                    data=data,
                    outcome=Outcome.CACHE_FRESH,
                    last_success=retrieved,
                    elapsed_seconds=time.monotonic() - started,
                    notes=[
                        f"Upstream not contacted; cache is {format_duration(cached_age)} "
                        f"old and the minimum refetch interval is "
                        f"{format_duration(min_refetch_interval)}."
                    ],
                )

    # --- Live attempt.
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")

        if _NOT_UPDATED_MARKER in raw[:400]:
            raise _NotModified(raw.strip().splitlines()[0] if raw.strip() else "")

        data = json.loads(raw)
        if parser is not None:
            data = parser(data)

        elapsed = time.monotonic() - started
        now = datetime.now(UTC)
        cache.write(data, now)
        log.info("source=%s outcome=live elapsed=%.3fs bytes=%d", name, elapsed, len(raw))
        return FetchResult(
            name=name,
            url=url,
            data=data,
            outcome=Outcome.LIVE,
            last_success=now,
            elapsed_seconds=elapsed,
        )

    except _NotModified as exc:
        elapsed = time.monotonic() - started
        data, retrieved = cache.read()
        if data is not None:
            log.info(
                "source=%s outcome=not_modified elapsed=%.3fs (upstream reports "
                "no new data since our last download)",
                name,
                elapsed,
            )
            return FetchResult(
                name=name,
                url=url,
                data=data,
                outcome=Outcome.NOT_MODIFIED,
                last_success=retrieved,
                elapsed_seconds=elapsed,
                notes=["Upstream reports no new data since our last download."],
            )
        # Upstream withheld data and we have no cache to fall back on. This
        # happens on a first run against a group we recently pulled elsewhere.
        log.warning(
            "source=%s outcome=failed elapsed=%.3fs error=not-modified-without-cache",
            name,
            elapsed,
        )
        return FetchResult(
            name=name,
            url=url,
            data=None,
            outcome=Outcome.FAILED,
            last_success=None,
            elapsed_seconds=elapsed,
            error=f"Upstream withheld data and no cache exists: {exc}",
        )

    except Exception as exc:
        elapsed = time.monotonic() - started
        detail = _describe_error(exc)
        data, retrieved = cache.read()
        if data is not None:
            log.warning(
                "source=%s outcome=cache_fallback elapsed=%.3fs error=%s cache_age=%s",
                name,
                elapsed,
                detail,
                format_duration(datetime.now(UTC) - retrieved)
                if retrieved
                else "unknown",
            )
            return FetchResult(
                name=name,
                url=url,
                data=data,
                outcome=Outcome.CACHE_FALLBACK,
                last_success=retrieved,
                elapsed_seconds=elapsed,
                error=detail,
                notes=["Upstream unreachable; serving last cached payload."],
            )

        log.error(
            "source=%s outcome=failed elapsed=%.3fs error=%s (no cache available)",
            name,
            elapsed,
            detail,
        )
        return FetchResult(
            name=name,
            url=url,
            data=None,
            outcome=Outcome.FAILED,
            last_success=None,
            elapsed_seconds=elapsed,
            error=detail,
        )


class _NotModified(Exception):
    """Upstream answered 200 but declined to send new data."""


def _describe_error(exc: Exception) -> str:
    """Human-readable one-liner for the health panel."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"Network error: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return f"Timed out after {HTTP_TIMEOUT_SECONDS:.0f}s"
    if isinstance(exc, json.JSONDecodeError):
        return f"Malformed JSON: {exc.msg}"
    if isinstance(exc, ValueError):
        return f"Rejected payload: {exc}"
    return f"{type(exc).__name__}: {exc}"


def format_duration(delta: timedelta) -> str:
    """Compact human duration, e.g. '3h 12m'."""
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
