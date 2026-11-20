"""The one place upstream network I/O happens.

Every upstream read in this project goes through :func:`fetch_source`, which
guarantees the same things regardless of caller:

1. The call has a hard timeout.
2. Failure is caught, logged with source and elapsed time, and falls back to
   the last good payload held in the state store.
3. The outcome is recorded in a result object the health panel can report
   truthfully -- including *why* a source degraded.
4. A source with a politeness floor is not contacted again until the floor
   has cleared, and that is decided from the stored record of the last
   attempt, not from how often the job happens to run.
5. It never raises. A caller cannot accidentally abort the run by forgetting
   a try/except, because there is nothing to catch.

This is deliberately a single choke point rather than scattered try/except
blocks: it means "what happens when upstream is down" has exactly one answer,
and that answer is testable in isolation.

State
-----
Each source has one object in the state store holding its last good payload
together with everything known about it: when those bytes were downloaded,
when upstream last confirmed they were current, when the last attempt was and
how it ended, and the validators (``ETag`` / ``Last-Modified``) upstream sent.
Payload and metadata are one object so they cannot disagree, and it is
written with a generation precondition so two overlapping runs cannot
interleave their writes.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from orbital_watch.config import (
    HTTP_TIMEOUT_SECONDS,
    NETWORK_BUDGET_SECONDS,
    USER_AGENT,
)
from orbital_watch.store import ABSENT, PreconditionFailed, Store, StoreError

log = logging.getLogger(__name__)

#: Prefix of every per-source state object in the state store.
STATE_PREFIX = "state"

#: Version of the state object's shape. A record written with another version
#: is treated as absent rather than trusted.
STATE_SCHEMA = 1


class Outcome(StrEnum):
    """How a fetch actually resolved."""

    #: Fetched new bytes from upstream.
    LIVE = "live"
    #: Upstream said nothing has changed; we reused the stored payload. Not a
    #: degradation: upstream answered, and what we hold is what it has.
    NOT_MODIFIED = "not_modified"
    #: We chose not to call upstream because it answered recently enough that
    #: it cannot have anything newer.
    CACHE_FRESH = "cache_fresh"
    #: Upstream failed (now, or at the last attempt while a retry is not yet
    #: due); we served the stored payload instead. Degraded but serving.
    CACHE_FALLBACK = "cache_fallback"
    #: Upstream failed and there was nothing stored to fall back on.
    FAILED = "failed"


#: Outcomes in which upstream answered for the data we are serving.
CONTACT_OUTCOMES = frozenset({Outcome.LIVE, Outcome.NOT_MODIFIED})


@dataclass(frozen=True)
class Payload:
    """What a parser hands back: the data to keep, plus facts about it.

    ``facts`` is for things that are true of the upstream response but are not
    recoverable from the data we keep -- for example how many objects a group
    offered before it was trimmed to the ones we track.
    """

    data: Any
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSpec:
    """Everything :func:`fetch_source` needs to know about one upstream."""

    #: Stable identifier, used in logs, health output and the state key.
    name: str
    url: str
    #: Applied to a decoded upstream body. Returns the data to keep (or a
    #: :class:`Payload`); raises ``ValueError`` to reject a malformed body.
    parse: Callable[[Any], Any] | None = None
    #: Applied to a payload read back from the state store, which may have
    #: been written by an older revision of this code. Returns the data or
    #: raises ``ValueError``. Defaults to accepting it as stored.
    revalidate: Callable[[Any], Any] | None = None
    #: Given (new data, previously stored data), raises ``ValueError`` if the
    #: new payload is implausible next to the old one.
    plausible: Callable[[Any, Any], None] | None = None
    #: Do not contact upstream again within this long of its last answer.
    min_refetch_interval: timedelta | None = None
    #: Do not retry within this long of a failed attempt. Only meaningful
    #: together with ``min_refetch_interval``.
    retry_interval: timedelta | None = None


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
    #: When upstream last answered for the payload we are serving -- either
    #: by sending it or by confirming it is still current. For a fallback
    #: this is in the past, not "now"; that distinction is the entire point
    #: of the health panel.
    last_success: datetime | None
    elapsed_seconds: float
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    #: When the bytes themselves were downloaded. Older than ``last_success``
    #: whenever upstream has since said "unchanged".
    payload_retrieved_at: datetime | None = None
    #: When upstream was last actually asked, whatever the answer.
    last_attempt: datetime | None = None
    #: Facts the parser recorded about the upstream response.
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.data is not None


# A Celestrak quirk worth spelling out: when a GP group is re-requested before
# its data has been regenerated, the answer is a plaintext sentence reading
# "GP data has not updated since your last successful download of GROUP=... at
# <time> UTC" -- served with an HTTP 403 status. It is not JSON and it is not a
# real refusal. Parsed naively it either crashes the run or, much worse,
# replaces good stored data with a sentence. We detect it and treat it as
# not-modified.
_NOT_UPDATED_MARKER = "has not updated since your last successful"
_NOT_UPDATED_TIME = re.compile(r"at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")

#: Slack allowed between the download time Celestrak quotes and the one we
#: recorded. They stamp when they served the bytes; we stamp when we finished
#: reading and validating them.
_NOT_UPDATED_CLOCK_TOLERANCE = timedelta(minutes=5)

#: Ceiling on a single response body. The largest thing we legitimately fetch
#: is the Starlink GP group at roughly 5 MB, so this leaves ample headroom
#: while keeping a hostile or malfunctioning upstream from exhausting memory.
#: An out-of-memory kill is the one failure this module cannot convert into a
#: fallback, because the process dies before any handler runs.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

#: Error bodies are only inspected for the Celestrak sentinel in their first
#: few hundred characters, so they need far less room.
_ERROR_BODY_BYTES = 64 * 1024


def utcnow() -> datetime:
    return datetime.now(UTC)


class NetworkBudget:
    """A shared wall-clock allowance across every fetch in one run.

    Per-call timeouts bound each request, but with every upstream hanging they
    serialise: N sources x 20s is minutes of dead waiting before propagation
    starts. This caps the *sum*. Each caller asks :meth:`per_call_timeout` for
    the smaller of its own default and what remains; once the budget is spent,
    calls get a 1-second timeout -- enough to succeed against a healthy
    endpoint, short enough that a dead one costs almost nothing further.

    Never blocks a fetch outright: a run with an exhausted budget still
    *attempts* every source that is due, because an attempt is also how the
    health panel learns and reports what is wrong.
    """

    def __init__(self, total_seconds: float = NETWORK_BUDGET_SECONDS) -> None:
        self._deadline = time.monotonic() + total_seconds

    def per_call_timeout(self, default: float = HTTP_TIMEOUT_SECONDS) -> float:
        remaining = self._deadline - time.monotonic()
        return max(1.0, min(default, remaining))


# --------------------------------------------------------------------------
# Stored state
# --------------------------------------------------------------------------


@dataclass
class _State:
    """One source's record in the state store, already validated."""

    data: Any | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    payload_retrieved_at: datetime | None = None
    confirmed_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_outcome: Outcome | None = None
    last_error: str | None = None
    validators: dict[str, str] = field(default_factory=dict)
    #: Generation the record was read at; ``ABSENT`` if there was none. Used
    #: as the precondition when writing it back.
    generation: int = ABSENT


class _StateUnreadable(Exception):
    """The state store could not be read at all (as opposed to being empty)."""


def state_key(name: str) -> str:
    return f"{STATE_PREFIX}/{name}.json"


def _parse_time(raw: Any) -> datetime | None:
    """Parse a stored timestamp, tolerating anything a damaged record holds.

    A naive timestamp (hand-edited, or written by other tooling) is read as
    UTC rather than rejected: comparing it against an aware "now" would raise
    TypeError inside a function documented as never raising.
    """
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _load_state(spec: SourceSpec, store: Store) -> _State:
    """Read and validate a source's stored record.

    Three different things can be wrong and they are handled differently:

    * The store cannot be read: raises :class:`_StateUnreadable`. The caller
      must not treat that as "no state", because for a rate-limited upstream
      "no state" means "ask now".
    * The record is damaged or the wrong shape: whatever can be salvaged is
      kept (in particular the time of the last attempt, which is what
      politeness depends on) and the rest is treated as absent. The record's
      generation is kept so the next write replaces the damaged object.
    * The payload no longer passes validation (it was written by older code):
      the payload alone is dropped. A stored payload that validation would
      reject must degrade to "no data", not crash the stage that uses it.
    """
    try:
        stored = store.read(state_key(spec.name))
    except StoreError as exc:
        raise _StateUnreadable(str(exc)) from exc
    if stored is None:
        return _State()

    state = _State(generation=stored.generation)
    try:
        record = json.loads(stored.data)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning(
            "source=%s stored state is not JSON (%s); ignoring it", spec.name, exc
        )
        return state
    if not isinstance(record, dict) or record.get("schema") != STATE_SCHEMA:
        log.warning("source=%s stored state has an unknown shape; ignoring it", spec.name)
        return state

    state.last_attempt_at = _parse_time(record.get("last_attempt_at"))
    state.last_error = (
        record["last_error"] if isinstance(record.get("last_error"), str) else None
    )
    try:
        state.last_outcome = Outcome(record.get("last_outcome"))
    except ValueError:
        state.last_outcome = None

    payload = record.get("payload")
    if payload is None:
        return state
    try:
        state.data = spec.revalidate(payload) if spec.revalidate else payload
    except Exception as exc:
        log.warning(
            "source=%s stored payload rejected by validator (%s); shape was "
            "likely written by an older revision",
            spec.name,
            exc,
        )
        return state

    state.payload_retrieved_at = _parse_time(record.get("payload_retrieved_at"))
    state.confirmed_at = _parse_time(record.get("confirmed_at"))
    facts = record.get("facts")
    state.facts = facts if isinstance(facts, dict) else {}
    validators = record.get("validators")
    if isinstance(validators, dict):
        state.validators = {
            key: value
            for key, value in validators.items()
            if key in ("etag", "last_modified") and isinstance(value, str)
        }
    return state


def _save_state(spec: SourceSpec, store: Store, state: _State) -> str | None:
    """Write a source's record back. Returns a note if it could not be saved.

    Never raises: a state write that fails must not turn a successful fetch
    into a failed one. The run still publishes what it fetched; the cost is
    that the next run will not know about this one.
    """

    def stamp(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    record = {
        "schema": STATE_SCHEMA,
        "name": spec.name,
        "url": spec.url,
        "last_attempt_at": stamp(state.last_attempt_at),
        "last_outcome": state.last_outcome.value if state.last_outcome else None,
        "last_error": state.last_error,
        "confirmed_at": stamp(state.confirmed_at),
        "payload_retrieved_at": stamp(state.payload_retrieved_at),
        "validators": state.validators,
        "facts": state.facts,
        "payload": state.data,
    }
    try:
        body = json.dumps(record, separators=(",", ":"), allow_nan=False).encode()
        state.generation = store.write(
            state_key(spec.name),
            body,
            content_type="application/json",
            if_generation_match=state.generation,
        )
    except PreconditionFailed:
        # Another run wrote this source's state while we were fetching. Its
        # record is at least as new as ours, so it stays; overwriting it is
        # exactly the interleaving the precondition exists to prevent.
        log.warning(
            "source=%s state changed during this run; keeping the other run's record",
            spec.name,
        )
        return "Another run updated this source's stored state at the same time; its record was kept."
    except (StoreError, ValueError, TypeError) as exc:
        log.warning("source=%s state could not be saved: %s", spec.name, exc)
        return "This result could not be saved to the state store, so the next run will not know about it."
    return None


# --------------------------------------------------------------------------
# The fetch itself
# --------------------------------------------------------------------------


class _NotModified(Exception):
    """Upstream declined to send data because nothing has changed."""


class _Withheld(Exception):
    """Upstream declined to send data, and what we hold may not be current."""


def fetch_source(
    spec: SourceSpec,
    store: Store,
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    clock: Callable[[], datetime] = utcnow,
) -> FetchResult:
    """Read one upstream source, falling back to stored data on any failure.

    Never raises. Always returns a :class:`FetchResult`.

    Args:
        spec: Which upstream, how to validate it, and how often it may be asked.
        store: Where the source's state is kept between runs.
        timeout: Hard per-call timeout in seconds.
        clock: Source of the current time. Injectable so tests can move it.
    """
    started = time.monotonic()
    try:
        return _fetch(spec, store, timeout, clock, started)
    except Exception as exc:
        # Nothing below is expected to raise, and this handler is what makes
        # "never raises" a property rather than a hope. The message is not
        # interpolated: this string is published (see _describe_error).
        log.exception("source=%s unexpected error inside the fetch helper", spec.name)
        return FetchResult(
            name=spec.name,
            url=spec.url,
            data=None,
            outcome=Outcome.FAILED,
            last_success=None,
            elapsed_seconds=time.monotonic() - started,
            error=f"Unexpected {type(exc).__name__} (see job logs)",
        )


def _fetch(
    spec: SourceSpec,
    store: Store,
    timeout: float,
    clock: Callable[[], datetime],
    started: float,
) -> FetchResult:
    def result(
        state: _State,
        outcome: Outcome,
        *,
        error: str | None = None,
        notes: list[str] | None = None,
        serve: bool = True,
    ) -> FetchResult:
        return FetchResult(
            name=spec.name,
            url=spec.url,
            data=state.data if serve else None,
            outcome=outcome,
            last_success=state.confirmed_at if serve else None,
            elapsed_seconds=time.monotonic() - started,
            error=error,
            notes=[note for note in (notes or []) if note],
            payload_retrieved_at=state.payload_retrieved_at if serve else None,
            last_attempt=state.last_attempt_at,
            facts=dict(state.facts) if serve else {},
        )

    # --- Stored state.
    try:
        state = _load_state(spec, store)
    except _StateUnreadable as exc:
        if spec.min_refetch_interval is not None:
            # Fail closed. Without the record of the last attempt there is no
            # way to know whether this upstream may be asked yet, and a
            # storage outage must not turn into a burst of requests against a
            # server that rate-limits.
            log.error(
                "source=%s outcome=failed elapsed=%.3fs error=state-unreadable (%s)",
                spec.name,
                time.monotonic() - started,
                exc,
            )
            return result(
                _State(),
                Outcome.FAILED,
                error=(
                    "The state store could not be read, so it is not known when "
                    "this upstream was last asked; it was not contacted."
                ),
            )
        log.warning(
            "source=%s state unreadable (%s); fetching without it", spec.name, exc
        )
        state = _State()
        state_usable = False
    else:
        state_usable = True

    now = clock()

    # --- Politeness: is this upstream due to be asked at all?
    wait = _time_until_due(spec, state, now)
    if wait is not None:
        return _not_due(spec, state, wait, now, started, result)

    # --- Live attempt.
    state.last_attempt_at = now
    try:
        raw, validators = _http_get(spec, state, timeout)
        payload = _as_payload(
            spec.parse(json.loads(raw)) if spec.parse else json.loads(raw)
        )
        if spec.plausible is not None and state.data is not None:
            spec.plausible(payload.data, state.data)

        fetched = clock()
        state.data = payload.data
        state.facts = payload.facts
        state.payload_retrieved_at = fetched
        state.confirmed_at = fetched
        state.validators = validators
        state.last_outcome = Outcome.LIVE
        state.last_error = None
        note = _save_state(spec, store, state) if state_usable else _UNSAVED_NOTE
        log.info(
            "source=%s outcome=live elapsed=%.3fs chars=%d",
            spec.name,
            time.monotonic() - started,
            len(raw),
        )
        return result(state, Outcome.LIVE, notes=[note] if note else None)

    except _NotModified as exc:
        if state.data is None:
            # Upstream withheld data and we hold nothing. This happens on a
            # first run from an address that pulled the same group recently.
            return _degrade(
                spec,
                store,
                state,
                f"Upstream withheld data and nothing is stored: {exc}",
                state_usable,
                started,
                result,
            )
        state.confirmed_at = clock()
        state.last_outcome = Outcome.NOT_MODIFIED
        state.last_error = None
        note = _save_state(spec, store, state) if state_usable else _UNSAVED_NOTE
        log.info(
            "source=%s outcome=not_modified elapsed=%.3fs (upstream reports no "
            "new data since our last download)",
            spec.name,
            time.monotonic() - started,
        )
        return result(
            state,
            Outcome.NOT_MODIFIED,
            notes=["Upstream reports no new data since our last download.", note],
        )

    except Exception as exc:
        return _degrade(
            spec,
            store,
            state,
            _describe_error(exc, timeout),
            state_usable,
            started,
            result,
        )


_UNSAVED_NOTE = (
    "The state store could not be read this run, so this result was not saved to it."
)


def _time_until_due(spec: SourceSpec, state: _State, now: datetime) -> timedelta | None:
    """How long until this upstream may be asked, or ``None`` if it may be now."""
    if spec.min_refetch_interval is None or state.last_attempt_at is None:
        return None
    answered = state.last_outcome in CONTACT_OUTCOMES
    interval = spec.min_refetch_interval
    if not answered and spec.retry_interval is not None:
        interval = spec.retry_interval

    since = now - state.last_attempt_at
    # A negative age means the recorded attempt is in the future (clock skew,
    # a hand edit). Treating it as recent would block this source until the
    # wall clock caught up with a bogus timestamp; treat it as unknown.
    if since < timedelta(0) or since >= interval:
        return None
    return interval - since


def _not_due(
    spec: SourceSpec,
    state: _State,
    wait: timedelta,
    now: datetime,
    started: float,
    result: Callable[..., FetchResult],
) -> FetchResult:
    """Resolve a source we are deliberately not contacting this run."""
    since = format_duration(now - state.last_attempt_at)
    answered = state.last_outcome in CONTACT_OUTCOMES

    if answered and state.data is not None:
        log.info(
            "source=%s outcome=cache_fresh elapsed=%.3fs (asked %s ago; not "
            "due for another %s)",
            spec.name,
            time.monotonic() - started,
            since,
            format_duration(wait),
        )
        return result(
            state,
            Outcome.CACHE_FRESH,
            notes=[
                f"Upstream not contacted: it last answered {since} ago and is "
                f"asked at most once every "
                f"{format_duration(spec.min_refetch_interval)}."
            ],
        )

    if answered:
        # Upstream answered recently, but what was stored from that answer is
        # missing or no longer passes validation (a record written by an
        # older revision). There is nothing to serve, and asking again inside
        # the floor would only earn a "not updated" reply.
        error = (
            "The stored payload is missing or no longer valid, and this "
            "upstream may not be asked again yet."
        )
        note = f"Upstream last answered {since} ago; the next request is due in {format_duration(wait)}."
        log.error(
            "source=%s outcome=failed elapsed=%.3fs error=stored-payload-unusable "
            "(next request in %s)",
            spec.name,
            time.monotonic() - started,
            format_duration(wait),
        )
        return result(state, Outcome.FAILED, error=error, notes=[note], serve=False)

    # The last attempt failed and the retry is not due. The source stays in
    # the state that attempt left it in: reporting it as a deliberate, healthy
    # skip would make a broken source flip back to "fresh" between retries.
    error = state.last_error or "The last attempt failed."
    note = (
        f"Last attempt was {since} ago and failed; the next one is due in "
        f"{format_duration(wait)}."
    )
    if state.data is not None:
        log.warning(
            "source=%s outcome=cache_fallback elapsed=%.3fs error=%s (retry in %s)",
            spec.name,
            time.monotonic() - started,
            error,
            format_duration(wait),
        )
        return result(state, Outcome.CACHE_FALLBACK, error=error, notes=[note])

    log.error(
        "source=%s outcome=failed elapsed=%.3fs error=%s (retry in %s)",
        spec.name,
        time.monotonic() - started,
        error,
        format_duration(wait),
    )
    return result(state, Outcome.FAILED, error=error, notes=[note], serve=False)


def _degrade(
    spec: SourceSpec,
    store: Store,
    state: _State,
    detail: str,
    state_usable: bool,
    started: float,
    result: Callable[..., FetchResult],
) -> FetchResult:
    """Resolve a failed attempt: serve what is stored, or report failure.

    The stored payload is never touched here. Whatever went wrong -- a
    timeout, an error status, a body that would not parse or validate -- the
    last good copy survives it.
    """
    serving = state.data is not None
    state.last_outcome = Outcome.CACHE_FALLBACK if serving else Outcome.FAILED
    state.last_error = detail
    note = _save_state(spec, store, state) if state_usable else _UNSAVED_NOTE

    elapsed = time.monotonic() - started
    if serving:
        log.warning(
            "source=%s outcome=cache_fallback elapsed=%.3fs error=%s",
            spec.name,
            elapsed,
            detail,
        )
        return result(
            state,
            Outcome.CACHE_FALLBACK,
            error=detail,
            notes=["Upstream unreachable; serving the last stored payload.", note],
        )

    log.error(
        "source=%s outcome=failed elapsed=%.3fs error=%s (nothing stored)",
        spec.name,
        elapsed,
        detail,
    )
    return result(state, Outcome.FAILED, error=detail, notes=[note], serve=False)


def _http_get(
    spec: SourceSpec, state: _State, timeout: float
) -> tuple[str, dict[str, str]]:
    """GET the source. Returns (body, validators) or raises.

    Raises :class:`_NotModified` when upstream says our copy is current,
    :class:`_Withheld` when it says so about a download that was not ours, and
    whatever ``urlopen`` raises otherwise.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    # Conditional request, but only when there is a payload to keep using: a
    # "not modified" answer is useless to a run that holds nothing.
    if state.data is not None:
        if "etag" in state.validators:
            headers["If-None-Match"] = state.validators["etag"]
        if "last_modified" in state.validators:
            headers["If-Modified-Since"] = state.validators["last_modified"]

    request = urllib.request.Request(spec.url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = _read_capped(response).decode("utf-8", errors="replace")
            validators = _validators_from(response)
    except urllib.error.HTTPError as http_error:
        if http_error.code == 304:
            raise _NotModified("HTTP 304 Not Modified") from http_error
        # Celestrak serves the not-updated sentinel with a 403 status, not a
        # 200, so the body has to be inspected on the error path too. Without
        # this, "nothing has changed since your last download" -- a completely
        # benign condition -- is reported as a hard failure.
        _raise_if_not_updated(_read_error_body(http_error), state)
        raise

    _raise_if_not_updated(raw, state)
    return raw, validators


def _raise_if_not_updated(body: str, state: _State) -> None:
    """Recognise Celestrak's "not updated" sentence and decide what it means.

    The sentence names the time of "your last successful download", and
    Celestrak keeps that per requesting address. From a cloud platform's
    shared egress the download it remembers may be someone else's. So the
    quoted time is compared with the time we downloaded what we hold:

    * not later than ours: nothing has changed since a moment at or before
      our download, so what we hold is current. Not modified.
    * later than ours: the data may have been regenerated between our
      download and that one, and upstream will not send it to us. What we
      hold cannot be called confirmed. Withheld.
    * unreadable: same as later. An answer we cannot interpret is not
      evidence that our copy is current.
    """
    if _NOT_UPDATED_MARKER not in body[:400]:
        return
    sentence = " ".join(body.split())[:200]
    match = _NOT_UPDATED_TIME.search(body[:400])
    if match is None or state.payload_retrieved_at is None:
        if state.data is None:
            raise _NotModified(sentence)
        raise _Withheld(
            "Celestrak withheld data without naming a download time that can be "
            "matched to ours"
        )
    quoted = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    if quoted <= state.payload_retrieved_at + _NOT_UPDATED_CLOCK_TOLERANCE:
        raise _NotModified(sentence)
    raise _Withheld(
        f"Celestrak withheld data, citing a download from this address at "
        f"{quoted:%Y-%m-%d %H:%M:%S} UTC that was not ours"
    )


def _validators_from(response: Any) -> dict[str, str]:
    """Pull ETag / Last-Modified off a response, if it has any."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    found = {}
    for key, header in (("etag", "ETag"), ("last_modified", "Last-Modified")):
        value = headers.get(header)
        if isinstance(value, str) and value.strip():
            found[key] = value.strip()
    return found


def _as_payload(parsed: Any) -> Payload:
    return parsed if isinstance(parsed, Payload) else Payload(data=parsed)


def _read_capped(response: Any, limit: int | None = None) -> bytes:
    """Read at most ``limit`` bytes, rejecting anything larger.

    An unbounded ``read()`` is the one failure mode this module's contract
    cannot survive. Every other upstream problem raises an exception that is
    turned into a fallback; a multi-gigabyte body instead gets the process
    killed for memory, which is not catchable. The run dies, no fallback
    happens, and the property this project exists to demonstrate fails -- not
    because of a bug in the logic, but because the logic never got to run.

    Raising ValueError routes an oversized body through the normal rejected-
    payload path, so it degrades to stored data like any other bad response.

    The cap is read from the module at call time rather than bound as a default
    argument, so it stays a single tunable value rather than one frozen when
    this function was defined.
    """
    limit = MAX_RESPONSE_BYTES if limit is None else limit
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError(
            f"response exceeded the {limit // (1024 * 1024)} MB cap; refusing to buffer it"
        )
    return body


def _read_error_body(error: urllib.error.HTTPError) -> str:
    """Best-effort read of an HTTPError's body. Never raises.

    Only the first few hundred characters are ever inspected (for Celestrak's
    not-updated sentinel), so this is capped far tighter than a success body.
    """
    try:
        return error.read(_ERROR_BODY_BYTES).decode("utf-8", errors="replace")
    except Exception:
        # Diagnostic read only; a failure here must not mask the original error.
        return ""


def _describe_error(exc: Exception, timeout: float) -> str:
    """Human-readable one-liner for the health panel.

    Published verbatim in the document, so it must describe what the code
    actually did -- including the timeout actually used, not the default.
    """
    if isinstance(exc, _Withheld):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"Network error: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return f"Timed out after {timeout:.0f}s"
    if isinstance(exc, json.JSONDecodeError):
        return f"Malformed JSON: {exc.msg}"
    if isinstance(exc, ValueError):
        return f"Rejected payload: {exc}"
    # Deliberately does not interpolate the message for unrecognised errors.
    # This string is published verbatim on a public site, and an arbitrary
    # exception text is the one place run-environment detail could leak into
    # it -- a URLError raised through a proxy configured with credentials in
    # the URL would carry them straight through. The type name is enough to
    # categorise the failure; the full message stays in the logs, which are
    # not published.
    log.debug("unclassified fetch error: %s: %s", type(exc).__name__, exc)
    return f"Unexpected {type(exc).__name__} (see job logs)"


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
