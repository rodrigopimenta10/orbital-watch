"""The fetch helper and the sources built on it.

Everything the project claims about upstream failure starts in
``fetch_source``: it never raises, it never lets a bad response replace good
stored data, it never claims contact it did not have, and it never asks a
rate-limited upstream more often than its floor allows -- however often it is
called. Each of those is pinned here, in isolation, against a real (local)
state store.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from orbital_watch.config import (
    CELESTRAK_MIN_REFETCH_INTERVAL,
    CELESTRAK_RETRY_INTERVAL,
    SPACE_WEATHER_STALENESS,
    TLE_STALENESS,
    TRACKED_GROUPS,
    SatelliteGroup,
    StalenessPolicy,
)
from orbital_watch.sources import celestrak, swpc
from orbital_watch.sources import fetch as fetch_module
from orbital_watch.sources.fetch import (
    Outcome,
    Payload,
    SourceSpec,
    fetch_source,
    format_duration,
    state_key,
)
from orbital_watch.store import PreconditionFailed, StoreError
from tests.helpers import (
    FAILURE_MODES,
    FakeClock,
    FakeMonotonic,
    FakeResponse,
    TricklingResponse,
    fixture_bytes,
    responder,
    urls,
)

DEMO = SourceSpec(name="demo", url="https://example.invalid/data.json")

#: A source with a politeness floor, like a Celestrak group but tiny.
POLITE = SourceSpec(
    name="polite",
    url="https://example.invalid/gp.php",
    min_refetch_interval=timedelta(hours=2),
    retry_interval=timedelta(minutes=30),
)


def stored_record(store, spec) -> dict:
    return json.loads(store.read(state_key(spec.name)).data)


def serve(monkeypatch, mapping, **kwargs) -> list:
    """Install a fake upstream and return the list its requests are logged to."""
    calls: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen", responder(mapping, calls=calls, **kwargs)
    )
    return calls


# --------------------------------------------------------------------------
# The fetch helper itself
# --------------------------------------------------------------------------


def test_successful_fetch_is_stored_with_its_metadata(state_store, monkeypatch):
    clock = FakeClock()
    serve(monkeypatch, {"data.json": {"hello": "world"}})

    result = fetch_source(DEMO, state_store, clock=clock)

    assert result.outcome is Outcome.LIVE
    assert result.data == {"hello": "world"}
    assert result.last_success == clock.now
    record = stored_record(state_store, DEMO)
    assert record["payload"] == {"hello": "world"}
    assert record["last_outcome"] == "live"
    assert record["confirmed_at"] == clock.now.isoformat()
    assert record["payload_retrieved_at"] == clock.now.isoformat()
    assert record["last_attempt_at"] == clock.now.isoformat()


@pytest.mark.parametrize("mode", sorted(FAILURE_MODES))
def test_fetch_falls_back_to_stored_data_on_every_failure_mode(
    state_store, monkeypatch, mode
):
    """Whatever goes wrong upstream, stored data keeps us serving."""
    clock = FakeClock()
    serve(monkeypatch, {"data.json": {"stored": True}})
    fetch_source(DEMO, state_store, clock=clock)
    first_contact = clock.now

    clock.advance(timedelta(minutes=10))
    serve(monkeypatch, {"data.json": FAILURE_MODES[mode]})
    result = fetch_source(DEMO, state_store, clock=clock)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == {"stored": True}
    assert result.error
    # The fallback is dated by the last real contact, not by this run.
    assert result.last_success == first_contact
    assert result.last_attempt == clock.now


@pytest.mark.parametrize("mode", sorted(FAILURE_MODES))
def test_fetch_reports_failure_when_nothing_is_stored(state_store, monkeypatch, mode):
    serve(monkeypatch, {"data.json": FAILURE_MODES[mode]})

    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.FAILED
    assert result.data is None
    assert not result.ok
    assert result.error
    assert result.last_success is None


def test_fetch_never_raises_even_on_unexpected_errors(state_store, monkeypatch):
    """The helper's contract is that callers never need a try/except."""

    def _explode(request, timeout=None):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr("urllib.request.urlopen", _explode)

    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.FAILED
    assert "RuntimeError" in result.error


def test_fetch_never_raises_even_when_its_own_code_breaks(state_store, monkeypatch):
    """The last line of defence: a bug inside the helper is still a result."""

    def _broken(*args, **kwargs):
        raise KeyError("a bug in the helper itself")

    monkeypatch.setattr(fetch_module, "_load_state", _broken)

    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.FAILED
    assert "KeyError" in result.error


def test_unclassified_error_text_is_not_published(state_store, monkeypatch):
    """An unrecognised exception's message must not reach the document.

    That string is served verbatim from a public site. A URLError raised
    through a proxy configured with credentials in its URL would otherwise
    carry them straight into the published output. The exception type is
    enough to categorise the failure; the message belongs in the logs.
    """
    leaked = "http://user:hunter2@proxy.internal:3128 refused"

    def _explode(request, timeout=None):
        raise RuntimeError(leaked)

    monkeypatch.setattr("urllib.request.urlopen", _explode)

    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.FAILED
    assert leaked not in result.error
    assert "hunter2" not in result.error
    assert "proxy.internal" not in result.error
    # Still useful: the caller can see what kind of failure it was.
    assert "RuntimeError" in result.error


def test_malformed_json_does_not_overwrite_good_stored_data(state_store, monkeypatch):
    """A garbage response must not replace the data we would fall back to."""
    serve(monkeypatch, {"data.json": {"good": True}})
    fetch_source(DEMO, state_store)

    serve(monkeypatch, {"data.json": b"<html>not json</html>"})
    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == {"good": True}
    assert "Malformed JSON" in result.error
    assert stored_record(state_store, DEMO)["payload"] == {"good": True}


def test_rejected_payload_does_not_overwrite_good_stored_data(state_store, monkeypatch):
    """Valid JSON of the wrong shape is refused exactly like invalid JSON."""
    spec = swpc.KP
    good = [{"time_tag": "2026-08-06T00:00:00", "Kp": 2.33}]
    serve(monkeypatch, {"planetary-k-index": good})
    fetch_source(spec, state_store)

    serve(monkeypatch, {"planetary-k-index": {"a dict": "where a list belongs"}})
    result = fetch_source(spec, state_store)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert "Rejected payload" in result.error
    assert stored_record(state_store, spec)["payload"] == good


def test_timeout_error_reports_the_timeout_actually_used(state_store, monkeypatch):
    serve(monkeypatch, {"data.json": TimeoutError("timed out")})
    spec = SourceSpec(name="demo", url=DEMO.url, max_seconds=7)

    result = fetch_source(spec, state_store)

    assert result.error == "Timed out after 7s without a response"


def test_oversized_response_is_rejected_not_buffered(state_store, monkeypatch):
    """A body past the cap is refused before it can exhaust memory."""
    monkeypatch.setattr(fetch_module, "MAX_RESPONSE_BYTES", 1024)
    serve(monkeypatch, {"data.json": b"[" + b"1," * 2000 + b"1]"})

    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.FAILED
    assert "cap" in result.error


def test_oversized_response_falls_back_to_stored_data(state_store, monkeypatch):
    serve(monkeypatch, {"data.json": {"good": True}})
    fetch_source(DEMO, state_store)

    monkeypatch.setattr(fetch_module, "MAX_RESPONSE_BYTES", 16)
    serve(monkeypatch, {"data.json": b"[" + b"1," * 2000 + b"1]"})
    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == {"good": True}


# --------------------------------------------------------------------------
# Politeness: the floor is enforced against stored state
# --------------------------------------------------------------------------


def test_upstream_is_not_asked_again_inside_the_floor(state_store, monkeypatch):
    """However often the job runs, a polite source is asked once per interval."""
    clock = FakeClock()
    calls = serve(monkeypatch, {"gp.php": [{"a": 1}]})
    fetch_source(POLITE, state_store, clock=clock)
    contact = clock.now

    for _run in range(11):  # ten-minute runs across most of the two hours
        clock.advance(timedelta(minutes=10))
        result = fetch_source(POLITE, state_store, clock=clock)
        assert result.outcome is Outcome.CACHE_FRESH
        assert result.data == [{"a": 1}]
        assert result.last_success == contact
        assert result.last_attempt == contact

    assert len(calls) == 1, "upstream was contacted inside its floor"


def test_upstream_is_asked_again_once_the_floor_clears(state_store, monkeypatch):
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"a": 1}]})
    fetch_source(POLITE, state_store, clock=clock)

    clock.advance(timedelta(hours=2))  # exactly the floor: due
    calls = serve(monkeypatch, {"gp.php": [{"a": 2}]})
    result = fetch_source(POLITE, state_store, clock=clock)

    assert len(calls) == 1
    assert result.outcome is Outcome.LIVE
    assert result.data == [{"a": 2}]


def test_floor_survives_a_new_process(tmp_path, monkeypatch):
    """The floor lives in the store, not in memory.

    This is the property the first design never had: its floor was enforced
    against a disk cache that every production run started without. Two
    separate store objects over the same directory stand in for two separate
    job executions.
    """
    from orbital_watch.store import LocalStore

    clock = FakeClock()
    calls = serve(monkeypatch, {"gp.php": [{"a": 1}]})
    fetch_source(POLITE, LocalStore(tmp_path / "state"), clock=clock)

    clock.advance(timedelta(minutes=10))
    result = fetch_source(POLITE, LocalStore(tmp_path / "state"), clock=clock)

    assert result.outcome is Outcome.CACHE_FRESH
    assert len(calls) == 1


def test_failed_attempt_backs_off_and_stays_degraded(state_store, monkeypatch):
    """Between retries a broken source must not flip back to looking healthy.

    After a failure the source is not asked again until the retry interval
    has passed -- and in the meantime it keeps reporting the failure. If the
    quiet runs reported a deliberate, healthy skip instead, the panel would
    alternate between stale and fresh while nothing had recovered.
    """
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"a": 1}]})
    fetch_source(POLITE, state_store, clock=clock)
    contact = clock.now

    clock.advance(timedelta(hours=2))
    calls = serve(monkeypatch, {"gp.php": TimeoutError("timed out")})
    failed = fetch_source(POLITE, state_store, clock=clock)
    assert failed.outcome is Outcome.CACHE_FALLBACK

    for _run in range(2):  # +10m, +20m: retry not due
        clock.advance(timedelta(minutes=10))
        waiting = fetch_source(POLITE, state_store, clock=clock)
        assert waiting.outcome is Outcome.CACHE_FALLBACK
        assert waiting.error == failed.error
        assert waiting.last_success == contact
        assert any("next one is due" in note for note in waiting.notes)
    assert len(calls) == 1, "retried before the retry interval had passed"

    clock.advance(timedelta(minutes=10))  # +30m: due
    serve(monkeypatch, {"gp.php": [{"a": 2}]})
    recovered = fetch_source(POLITE, state_store, clock=clock)
    assert recovered.outcome is Outcome.LIVE
    assert recovered.data == [{"a": 2}]


def test_first_ever_failure_also_backs_off(state_store, monkeypatch):
    """With nothing stored, a failing upstream is still not hammered."""
    clock = FakeClock()
    calls = serve(monkeypatch, {"gp.php": TimeoutError("timed out")})
    first = fetch_source(POLITE, state_store, clock=clock)
    assert first.outcome is Outcome.FAILED

    clock.advance(timedelta(minutes=10))
    waiting = fetch_source(POLITE, state_store, clock=clock)

    assert waiting.outcome is Outcome.FAILED
    assert waiting.data is None
    assert waiting.error == first.error
    assert len(calls) == 1


def test_future_attempt_time_does_not_block_a_source_forever(state_store, monkeypatch):
    """A recorded attempt in the future (clock skew, hand edit) is ignored.

    Treating it as "very recent" would keep the source from being asked until
    the wall clock caught up with a bogus timestamp.
    """
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"a": 1}]})
    fetch_source(POLITE, state_store, clock=clock)

    clock.advance(-timedelta(days=3))  # the stored attempt is now 3 days ahead
    calls = serve(monkeypatch, {"gp.php": [{"a": 2}]})
    result = fetch_source(POLITE, state_store, clock=clock)

    assert len(calls) == 1
    assert result.outcome is Outcome.LIVE


def test_unreadable_state_store_fails_closed_for_a_polite_source(monkeypatch):
    """No record of the last attempt means no request, not a free one.

    A storage outage must not turn into a burst of requests against a server
    that rate-limits: without the stored record there is no way to know
    whether the floor has cleared.
    """

    class BrokenStore:
        def describe(self):
            return "a store that is down"

        def read(self, key):
            raise StoreError("backend unavailable")

        def write(self, *args, **kwargs):
            raise StoreError("backend unavailable")

    calls = serve(monkeypatch, {"gp.php": [{"a": 1}], "data.json": {"ok": True}})

    polite = fetch_source(POLITE, BrokenStore())
    assert polite.outcome is Outcome.FAILED
    assert "not contacted" in polite.error
    assert urls(calls) == []

    # A source with no floor has nothing to protect, so it is still fetched --
    # the run can publish live space weather through a state-store outage.
    unfloored = fetch_source(DEMO, BrokenStore())
    assert unfloored.outcome is Outcome.LIVE
    assert unfloored.data == {"ok": True}
    assert any("not saved" in note for note in unfloored.notes)


# --------------------------------------------------------------------------
# Overlapping runs
# --------------------------------------------------------------------------


def test_overlapping_run_does_not_overwrite_newer_state(state_store, monkeypatch):
    """Two runs fetch the same source at once; the slower one must not win.

    Run A reads the state, then is overtaken: run B fetches and stores newer
    data before A finishes. A's write carries the generation it read, which is
    now out of date, so it is refused and B's record stands. A still returns
    what it fetched -- its result is true -- and says its state was not kept.
    """
    clock = FakeClock()
    serve(monkeypatch, {"data.json": {"version": "original"}})
    fetch_source(DEMO, state_store, clock=clock)

    def run_b_overtakes(request, timeout=None):
        # While A is "on the network", B completes a whole fetch-and-store.
        monkeypatch.setattr(
            "urllib.request.urlopen", responder({"data.json": {"version": "from B"}})
        )
        fetch_source(DEMO, state_store, clock=clock)
        return FakeResponse(json.dumps({"version": "from A"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", run_b_overtakes)
    result_a = fetch_source(DEMO, state_store, clock=clock)

    assert result_a.outcome is Outcome.LIVE
    assert result_a.data == {"version": "from A"}
    assert any("Another run" in note for note in result_a.notes)
    assert stored_record(state_store, DEMO)["payload"] == {"version": "from B"}


def test_state_write_failure_does_not_fail_the_fetch(state_store, monkeypatch):
    serve(monkeypatch, {"data.json": {"ok": True}})

    def _refuse(*args, **kwargs):
        raise StoreError("disk full")

    monkeypatch.setattr(state_store, "write", _refuse)
    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.LIVE
    assert result.data == {"ok": True}
    assert any("could not be saved" in note for note in result.notes)


def test_precondition_failure_is_distinguished_from_other_store_errors():
    assert issubclass(PreconditionFailed, StoreError)


# --------------------------------------------------------------------------
# Conditional requests and "not modified"
# --------------------------------------------------------------------------


def test_validators_are_stored_and_sent_back(state_store, monkeypatch):
    """What upstream gives us to ask "has it changed?" is remembered and used."""
    clock = FakeClock()
    served = FakeResponse(
        b'{"v": 1}',
        {"ETag": '"abc123"', "Last-Modified": "Thu, 06 Aug 2026 14:45:00 GMT"},
    )
    serve(monkeypatch, {"data.json": served})
    fetch_source(DEMO, state_store, clock=clock)

    assert stored_record(state_store, DEMO)["validators"] == {
        "etag": '"abc123"',
        "last_modified": "Thu, 06 Aug 2026 14:45:00 GMT",
    }

    clock.advance(timedelta(minutes=10))
    calls = serve(monkeypatch, {"data.json": served})
    fetch_source(DEMO, state_store, clock=clock)

    request = calls[0]
    assert request.get_header("If-none-match") == '"abc123"'
    assert request.get_header("If-modified-since") == "Thu, 06 Aug 2026 14:45:00 GMT"


def test_no_validators_are_sent_when_nothing_is_stored(state_store, monkeypatch):
    """A "not modified" answer is useless to a run that holds no payload."""
    calls = serve(monkeypatch, {"data.json": {"v": 1}})

    fetch_source(DEMO, state_store)

    assert calls[0].get_header("If-none-match") is None
    assert calls[0].get_header("If-modified-since") is None


def test_http_304_confirms_the_stored_payload(state_store, monkeypatch):
    """A 304 is upstream answering: contact is current, the bytes are older."""
    clock = FakeClock()
    serve(monkeypatch, {"data.json": FakeResponse(b'{"v": 1}', {"ETag": '"abc"'})})
    fetch_source(DEMO, state_store, clock=clock)
    downloaded = clock.now

    clock.advance(timedelta(minutes=10))
    not_modified = urllib.error.HTTPError(
        DEMO.url, 304, "Not Modified", {}, io.BytesIO(b"")
    )
    serve(monkeypatch, {"data.json": not_modified})
    result = fetch_source(DEMO, state_store, clock=clock)

    assert result.outcome is Outcome.NOT_MODIFIED
    assert result.data == {"v": 1}
    assert result.last_success == clock.now
    assert result.payload_retrieved_at == downloaded
    # The validators survive, so the next request is conditional too.
    assert stored_record(state_store, DEMO)["validators"] == {"etag": '"abc"'}


def celestrak_sentinel(quoted: datetime) -> bytes:
    """Celestrak's "not updated" sentence, naming a given download time."""
    template = fixture_bytes("celestrak_not_updated.txt").decode()
    assert "2026-08-06 14:52:37 UTC" in template
    return template.replace(
        "2026-08-06 14:52:37 UTC", f"{quoted:%Y-%m-%d %H:%M:%S} UTC"
    ).encode()


def forbidden(body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://celestrak.org/gp.php", 403, "Forbidden", {}, io.BytesIO(body)
    )


def test_not_updated_sentinel_with_an_http_403_is_not_modified(state_store, monkeypatch):
    """Celestrak says "nothing new" with a 403 whose body is a sentence.

    Observed against the live API. Checking the body only on the success path
    misses it, and a benign "nothing changed" gets reported as a hard failure
    while good stored data sits unused. And parsed naively, the sentence is
    not JSON -- it must never be stored in place of the data.
    """
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"OBJECT_NAME": "SAT"}]})
    fetch_source(POLITE, state_store, clock=clock)
    downloaded = clock.now

    clock.advance(timedelta(hours=2, minutes=5))
    serve(monkeypatch, {"gp.php": forbidden(celestrak_sentinel(downloaded))})
    result = fetch_source(POLITE, state_store, clock=clock)

    assert result.outcome is Outcome.NOT_MODIFIED
    assert result.data == [{"OBJECT_NAME": "SAT"}]
    # Contact is confirmed now; the bytes are still from the first download.
    assert result.last_success == clock.now
    assert result.payload_retrieved_at == downloaded
    assert stored_record(state_store, POLITE)["payload"] == [{"OBJECT_NAME": "SAT"}]


def test_not_updated_sentinel_with_an_http_200_is_not_modified(state_store, monkeypatch):
    """The same sentence on a success status is handled the same way."""
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"OBJECT_NAME": "SAT"}]})
    fetch_source(POLITE, state_store, clock=clock)
    downloaded = clock.now

    clock.advance(timedelta(hours=3))
    serve(monkeypatch, {"gp.php": celestrak_sentinel(downloaded)})
    result = fetch_source(POLITE, state_store, clock=clock)

    assert result.outcome is Outcome.NOT_MODIFIED
    assert result.data == [{"OBJECT_NAME": "SAT"}]


def test_sentinel_about_someone_elses_download_is_not_a_confirmation(
    state_store, monkeypatch
):
    """ "Not updated since *your* last download" -- but was it ours?

    Celestrak remembers the last download per requesting address, and a cloud
    platform's egress addresses are shared. If the download it names is later
    than ours, the data may have been regenerated in between and upstream
    will not send it. Calling that "confirmed current" would be claiming
    contact we did not have, so the source degrades instead.
    """
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"OBJECT_NAME": "SAT"}]})
    fetch_source(POLITE, state_store, clock=clock)
    ours = clock.now

    clock.advance(timedelta(hours=2, minutes=5))
    theirs = ours + timedelta(hours=1, minutes=40)
    serve(monkeypatch, {"gp.php": forbidden(celestrak_sentinel(theirs))})
    result = fetch_source(POLITE, state_store, clock=clock)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == [{"OBJECT_NAME": "SAT"}]
    assert result.last_success == ours, "contact must not be advanced"
    assert "was not ours" in result.error


def test_sentinel_without_a_readable_time_is_not_a_confirmation(state_store, monkeypatch):
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"OBJECT_NAME": "SAT"}]})
    fetch_source(POLITE, state_store, clock=clock)

    clock.advance(timedelta(hours=3))
    garbled = b"GP data has not updated since your last successful download."
    serve(monkeypatch, {"gp.php": forbidden(garbled)})
    result = fetch_source(POLITE, state_store, clock=clock)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert "withheld" in result.error.lower()


def test_sentinel_with_nothing_stored_is_a_failure(state_store, monkeypatch):
    """A first run from an address that pulled the group recently gets no data."""
    serve(
        monkeypatch,
        {"gp.php": forbidden(fixture_bytes("celestrak_not_updated.txt"))},
    )

    result = fetch_source(POLITE, state_store)

    assert result.outcome is Outcome.FAILED
    assert result.data is None
    assert "withheld" in result.error.lower()


def test_a_genuine_403_is_still_a_failure(state_store, monkeypatch):
    """Only the sentinel body is forgiven -- a real 403 must still degrade."""
    serve(monkeypatch, {"gp.php": forbidden(b"go away")})

    result = fetch_source(POLITE, state_store)

    assert result.outcome is Outcome.FAILED
    assert "403" in result.error


# --------------------------------------------------------------------------
# Damaged or outdated state
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "damage",
    [
        b"",
        b"not json at all",
        b"[1, 2, 3]",
        b'"a bare string"',
        b'{"schema": 999, "payload": [1]}',
        b'{"schema": 1, "payload": [1], "confirmed_at": 12345}',
        b'{"schema": 1, "payload": [1], "validators": "not a dict"}',
        b"\xff\xfe\x00 not even text",
    ],
)
def test_damaged_state_is_survived_and_then_repaired(state_store, monkeypatch, damage):
    """Any shape of damage degrades to "nothing stored", and heals on success.

    The component whose job is keeping data fresh must never be wedged by a
    malformed state object: a record that crashed the reader would crash
    every later run identically, because nothing would ever replace it.
    """
    state_store.write(state_key(DEMO.name), damage, content_type="application/json")
    serve(monkeypatch, {"data.json": {"fresh": True}})

    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.LIVE
    assert stored_record(state_store, DEMO)["payload"] == {"fresh": True}


def test_stored_payload_of_an_old_shape_is_dropped_not_served(state_store, monkeypatch):
    """State outlives the code that wrote it.

    A record written by an older revision is read by a newer one. If the
    expected shape has changed since, handing the old shape downstream
    unvalidated is how a fallback turns into a crash. Validating on read
    means it degrades to "no data" instead.
    """
    spec = swpc.KP
    old_shape = {
        "schema": 1,
        "payload": {"a dict": "from an older revision"},
        "confirmed_at": "2026-08-06T12:00:00+00:00",
        "last_outcome": "live",
    }
    state_store.write(
        state_key(spec.name),
        json.dumps(old_shape).encode(),
        content_type="application/json",
    )
    serve(monkeypatch, {"planetary-k-index": TimeoutError("timed out")})

    result = fetch_source(spec, state_store)

    assert result.outcome is Outcome.FAILED
    assert result.data is None


def test_naive_timestamp_in_state_does_not_raise(state_store, monkeypatch):
    """A timestamp without a zone is read as UTC, not compared and crashed on."""
    naive = {
        "schema": 1,
        "payload": [{"a": 1}],
        "confirmed_at": "2026-08-06T12:00:00",
        "payload_retrieved_at": "2026-08-06T12:00:00",
        "last_attempt_at": "2026-08-06T12:00:00",
        "last_outcome": "live",
    }
    state_store.write(
        state_key(POLITE.name),
        json.dumps(naive).encode(),
        content_type="application/json",
    )
    clock = FakeClock(datetime(2026, 8, 6, 12, 30, tzinfo=UTC))
    calls = serve(monkeypatch, {"gp.php": [{"a": 2}]})

    result = fetch_source(POLITE, state_store, clock=clock)

    assert result.outcome is Outcome.CACHE_FRESH
    assert result.last_success == datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    assert calls == []


def test_stored_data_without_a_date_is_served_undated(state_store, monkeypatch):
    """Data we cannot date is still served, but never with an invented date."""
    undated = {"schema": 1, "payload": {"good": True}, "last_outcome": "live"}
    state_store.write(
        state_key(DEMO.name),
        json.dumps(undated).encode(),
        content_type="application/json",
    )
    serve(monkeypatch, {"data.json": TimeoutError("timed out")})

    result = fetch_source(DEMO, state_store)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == {"good": True}
    assert result.last_success is None


# --------------------------------------------------------------------------
# Celestrak groups
# --------------------------------------------------------------------------

STATIONS = SatelliteGroup(key="stations", label="Stations", max_objects=100)


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [],
        [{"OBJECT_NAME": "MISSING EVERYTHING ELSE"}],
        ["not a dict"],
    ],
)
def test_unusable_gp_payloads_are_rejected(state_store, monkeypatch, payload):
    """Structurally wrong GP data is refused rather than propagated."""
    serve(monkeypatch, {"gp.php": payload})

    result = fetch_source(celestrak.group_spec(STATIONS), state_store)

    assert result.outcome is Outcome.FAILED
    assert result.data is None


def test_partially_valid_gp_payload_keeps_the_good_records(state_store, monkeypatch):
    good = json.loads(fixture_bytes("celestrak_stations.json"))
    serve(monkeypatch, {"gp.php": [{"OBJECT_NAME": "JUNK"}, *good]})

    result = fetch_source(celestrak.group_spec(STATIONS), state_store)

    assert result.ok
    assert len(result.data) == len(good)


def test_select_objects_caps_group_size():
    records = json.loads(fixture_bytes("celestrak_starlink.json"))
    group = SatelliteGroup(key="starlink", label="Starlink", max_objects=5)

    selected = celestrak.select_objects(records, group)

    assert len(selected) == 5
    # We keep the most recent epochs, which carry the least propagation error.
    epochs = [r["EPOCH"] for r in selected]
    assert epochs == sorted(epochs, reverse=True)


def test_group_is_stored_trimmed_with_its_full_size_on_record(state_store, monkeypatch):
    """What is kept is the sample; how big the group was is kept beside it.

    The page says "5 of 30 starlink". The 30 is not recoverable from the five
    records stored, so it has to survive as a fact -- including on later runs
    that serve the stored copy without contacting upstream.
    """
    records = json.loads(fixture_bytes("celestrak_starlink.json"))
    group = SatelliteGroup(key="starlink", label="Starlink", max_objects=5)
    spec = celestrak.group_spec(group)
    clock = FakeClock()
    serve(monkeypatch, {"GROUP=starlink": records})

    first = fetch_source(spec, state_store, clock=clock)
    clock.advance(timedelta(minutes=10))
    later = fetch_source(spec, state_store, clock=clock)

    assert len(first.data) == 5
    assert first.facts == {"available": len(records)}
    assert later.outcome is Outcome.CACHE_FRESH
    assert len(later.data) == 5
    assert later.facts == {"available": len(records)}


def test_collapsed_group_does_not_replace_a_substantial_one(state_store, monkeypatch):
    """A valid but tiny response is upstream trouble, not a real change."""
    good = json.loads(fixture_bytes("celestrak_stations.json"))
    assert len(good) >= celestrak.MIN_PLAUSIBLE_RECORDS
    spec = celestrak.group_spec(STATIONS)
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": good})
    fetch_source(spec, state_store, clock=clock)

    clock.advance(CELESTRAK_MIN_REFETCH_INTERVAL)
    serve(monkeypatch, {"gp.php": good[:2]})
    result = fetch_source(spec, state_store, clock=clock)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert len(result.data) == len(good)
    assert "shrank" in result.error
    assert len(stored_record(state_store, spec)["payload"]) == len(good)


def test_celestrak_groups_carry_the_configured_floor():
    spec = celestrak.group_spec(STATIONS)

    assert spec.min_refetch_interval == CELESTRAK_MIN_REFETCH_INTERVAL
    assert spec.retry_interval == CELESTRAK_RETRY_INTERVAL
    assert spec.url.endswith("GROUP=stations&FORMAT=json")


def test_space_weather_sources_have_no_floor():
    for spec in (swpc.KP, swpc.SOLAR_WIND, swpc.MAG_FIELD, swpc.SOLAR_CYCLE):
        assert spec.min_refetch_interval is None


def test_parser_may_return_a_bare_value_or_a_payload(state_store, monkeypatch):
    bare = SourceSpec(name="bare", url="https://example.invalid/a.json", parse=sorted)
    wrapped = SourceSpec(
        name="wrapped",
        url="https://example.invalid/b.json",
        parse=lambda data: Payload(data=sorted(data), facts={"count": len(data)}),
    )
    serve(monkeypatch, {"a.json": [3, 1, 2], "b.json": [3, 1, 2]})

    assert fetch_source(bare, state_store).data == [1, 2, 3]
    result = fetch_source(wrapped, state_store)
    assert result.data == [1, 2, 3]
    assert result.facts == {"count": 3}


# --------------------------------------------------------------------------
# Space weather parsing and interpretation
# --------------------------------------------------------------------------


def test_kp_series_parses_the_live_dict_shape(kp_records):
    readings = swpc.parse_kp_series(kp_records)

    assert readings
    assert all(0 <= r.kp <= 9 for r in readings)
    assert readings == sorted(readings, key=lambda r: r.time_tag)


def test_kp_series_parses_the_legacy_list_of_lists_shape():
    """SWPC has shipped both shapes; we accept either rather than assume."""
    legacy = [
        ["time_tag", "Kp", "a_running", "station_count"],
        ["2026-08-06T00:00:00", "3.33", "18", "8"],
        ["2026-08-06T03:00:00", "4.67", "22", "8"],
    ]
    readings = swpc.parse_kp_series(legacy)

    assert [r.kp for r in readings] == [3.33, 4.67]


@pytest.mark.parametrize(
    "payload",
    [
        [{"totally": "different"}],
        [["unknown", "header"], ["a", "b"]],
        [],
        {"a dict": "where a list belongs"},
        "a bare string",
        [1, 2, 3],
    ],
)
def test_kp_series_survives_an_unrecognised_shape(payload):
    assert swpc.parse_kp_series(payload) == []


@pytest.mark.parametrize(
    ("kp", "label"),
    [
        (0.0, "quiet"),
        (2.67, "quiet"),
        (3.0, "unsettled"),
        (3.99, "unsettled"),
        (4.0, "active"),
        (5.0, "minor storm"),
        (6.0, "moderate"),
        (7.0, "strong"),
        (8.0, "severe"),
        (9.0, "extreme"),
    ],
)
def test_kp_classification_boundaries(kp, label):
    assert swpc.classify_kp(kp)[0] == label


def test_kp_trend_direction():
    def series(values):
        return [
            swpc.KpReading(datetime(2026, 8, 6, h, tzinfo=UTC), v)
            for h, v in enumerate(values)
        ]

    assert swpc.kp_trend(series([1.0, 2.0, 3.0, 4.0])) == "rising"
    assert swpc.kp_trend(series([5.0, 4.0, 3.0, 2.0])) == "falling"
    assert swpc.kp_trend(series([3.0, 3.3, 3.0, 3.3])) == "steady"
    assert swpc.kp_trend([]) == "unknown"


@pytest.mark.parametrize(
    "records",
    [None, [], "text", [["a list"]], [{"proton_speed": "not a number"}]],
)
def test_latest_value_survives_any_shape(records):
    value, _stamp = swpc.latest_value(records, "proton_speed")
    assert value is None


# --------------------------------------------------------------------------
# Small things the panel prints
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=0), "0s"),
        (timedelta(seconds=45), "45s"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=3, minutes=12), "3h 12m"),
        (timedelta(days=2), "2d"),
        (timedelta(days=1, hours=6), "1d 6h"),
    ],
)
def test_duration_formatting(delta, expected):
    assert format_duration(delta) == expected


def test_staleness_policy_is_explicit_not_magic():
    """Thresholds must be declared constants the panel can publish."""
    for policy in (TLE_STALENESS, SPACE_WEATHER_STALENESS):
        assert isinstance(policy, StalenessPolicy)
        assert policy.fresh_within < policy.stale_within


# --------------------------------------------------------------------------
# Review findings before the first deploy (DECISIONS.md §30)
# --------------------------------------------------------------------------
#
# Each test below first reproduced a defect an independent review found, and
# now pins its fix.


@pytest.fixture
def monotonic(monkeypatch) -> FakeMonotonic:
    """Replace the monotonic clock, so "ninety seconds pass" takes no time."""
    import time

    fake = FakeMonotonic()
    monkeypatch.setattr(time, "monotonic", fake)
    return fake


def trickle(monkeypatch, monotonic, body: bytes, **kwargs) -> list[TricklingResponse]:
    """Install an upstream that sends ``body`` slowly; returns the responses made."""
    made: list[TricklingResponse] = []

    def _urlopen(request, timeout=None):
        made.append(TricklingResponse(body, monotonic, **kwargs))
        return made[-1]

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return made


FIVE_MEGABYTES = b"[" + b"1," * (5 * 1024 * 1024 // 2) + b"1]"


def test_throttled_body_is_abandoned_at_the_ceiling_not_read_to_the_end(
    state_store, monkeypatch, monotonic
):
    """Defect 1: a slow body never tripped the "hard timeout".

    Seen live: Celestrak served the 5 MB Starlink group at about 8 KB/s. No
    single socket read stalled, so the per-read timeout never fired, and the
    job read on for minutes -- past the point where it could still publish.
    The request as a whole now has a ceiling.
    """
    made = trickle(monkeypatch, monotonic, FIVE_MEGABYTES)  # 8 KB/s: 640 s in all
    spec = SourceSpec(
        name="slow", url="https://example.invalid/slow.json", max_seconds=20
    )
    started = monotonic.now

    result = fetch_source(spec, state_store)

    waited = monotonic.now - started
    assert result.outcome is Outcome.FAILED
    assert waited <= 21, f"read for {waited:.0f}s against a 20s ceiling"
    assert made[0].pieces_sent <= 21
    assert "incomplete" in result.error
    assert "20s" in result.error


def test_throttled_body_falls_back_to_stored_data(state_store, monkeypatch, monotonic):
    spec = SourceSpec(
        name="slow", url="https://example.invalid/slow.json", max_seconds=20
    )
    serve(monkeypatch, {"slow.json": [1, 2, 3]})
    fetch_source(spec, state_store)

    trickle(monkeypatch, monotonic, FIVE_MEGABYTES)
    result = fetch_source(spec, state_store)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == [1, 2, 3]
    assert stored_record(state_store, spec)["payload"] == [1, 2, 3]


def test_slow_but_adequate_transfer_still_completes(state_store, monkeypatch, monotonic):
    """The ceiling must not cut off a transfer that is merely unhurried."""
    body = json.dumps(list(range(50_000))).encode()
    trickle(monkeypatch, monotonic, body, piece=64 * 1024, seconds_per_piece=2.0)
    spec = SourceSpec(
        name="slow", url="https://example.invalid/slow.json", max_seconds=20
    )

    result = fetch_source(spec, state_store)

    assert result.outcome is Outcome.LIVE
    assert len(result.data) == 50_000


def test_error_body_is_read_against_the_same_ceiling(state_store, monkeypatch, monotonic):
    """An error response can trickle too; its body is only read for a sentence."""

    def _urlopen(request, timeout=None):
        body = TricklingResponse(b"x" * 10_000_000, monotonic, piece=16)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, body)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    spec = SourceSpec(
        name="slow", url="https://example.invalid/slow.json", max_seconds=20
    )
    started = monotonic.now

    result = fetch_source(spec, state_store)

    assert result.outcome is Outcome.FAILED
    assert "403" in result.error
    assert monotonic.now - started <= 21


def test_budget_caps_the_sum_of_slow_bodies(state_store, monkeypatch, monotonic):
    """Three throttled sources in a row cost the budget, not three ceilings."""
    from orbital_watch.sources.fetch import NetworkBudget

    trickle(monkeypatch, monotonic, FIVE_MEGABYTES)
    budget = NetworkBudget(total_seconds=30)
    started = monotonic.now

    for index in range(3):
        spec = SourceSpec(
            name=f"slow{index}", url="https://example.invalid/slow.json", max_seconds=20
        )
        fetch_source(spec, state_store, deadline=budget.deadline_for(spec.max_seconds))

    assert monotonic.now - started <= 33, "the budget did not bound the body reads"


def test_socket_timeout_never_exceeds_what_is_left(state_store, monkeypatch, monotonic):
    """A connection that goes silent is cut by the socket; it gets no more than remains."""
    from orbital_watch.config import HTTP_STALL_TIMEOUT_SECONDS

    seen: list[float] = []

    def _urlopen(request, timeout=None):
        seen.append(timeout)
        return FakeResponse(b"[1]")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    roomy = SourceSpec(name="a", url="https://example.invalid/a.json", max_seconds=45)
    tight = SourceSpec(name="b", url="https://example.invalid/b.json", max_seconds=3)

    fetch_source(roomy, state_store)
    fetch_source(tight, state_store)

    assert seen == [HTTP_STALL_TIMEOUT_SECONDS, 3]


@pytest.mark.parametrize(
    "body",
    [
        b'[{"time_tag": "2026-08-06T00:00:00", "Kp": NaN}]',
        b'[{"time_tag": "2026-08-06T00:00:00", "Kp": Infinity}]',
        b'[{"time_tag": "2026-08-06T00:00:00", "Kp": -Infinity}]',
        b'[{"time_tag": "2026-08-06T00:00:00", "Kp": 1e999}]',
    ],
)
def test_non_finite_numbers_from_upstream_are_refused_at_the_boundary(
    state_store, monkeypatch, body
):
    """Defect 2: NaN and Infinity were accepted, stored, and then blocked publishing.

    Python's JSON parser accepts the bare words NaN and Infinity, and turns a
    number too large for a float into infinity. Stored, such a value later
    failed the document's validation, and the run published nothing at all
    until upstream changed. It is refused where every other malformed
    response is refused, and the stored copy survives.
    """
    good = [{"time_tag": "2026-08-05T21:00:00", "Kp": 2.33}]
    serve(monkeypatch, {"planetary-k-index": good})
    fetch_source(swpc.KP, state_store)

    serve(monkeypatch, {"planetary-k-index": body})
    result = fetch_source(swpc.KP, state_store)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == good
    assert stored_record(state_store, swpc.KP)["payload"] == good


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-inf", "nan"])
def test_one_non_finite_reading_costs_that_reading_not_the_series(bad):
    """A number spelt as text ("NaN") gets past JSON; it stops at the parser."""
    readings = swpc.parse_kp_series(
        [
            {"time_tag": "2026-08-06T00:00:00", "Kp": "2.33"},
            {"time_tag": "2026-08-06T03:00:00", "Kp": bad},
            {"time_tag": "2026-08-06T06:00:00", "Kp": "3.00"},
        ]
    )

    assert [reading.kp for reading in readings] == [2.33, 3.0]
    value, _stamp = swpc.latest_value([{"proton_speed": bad}], "proton_speed")
    assert value is None


def test_overlapping_failure_does_not_displace_a_fresh_payload(state_store, monkeypatch):
    """Defect 3: the slower run's good download was thrown away.

    Run A is downloading. Run B, started meanwhile, fails and saves its
    failure record first. A's save then met the precondition and gave up --
    so the store kept "failed" and lost the payload A had just fetched. A
    success now looks again and writes over a record that is not a newer
    success.
    """
    clock = FakeClock()
    serve(monkeypatch, {"data.json": {"version": "original"}})
    fetch_source(DEMO, state_store, clock=clock)
    clock.advance(timedelta(minutes=10))

    def run_b_fails_meanwhile(request, timeout=None):
        monkeypatch.setattr(
            "urllib.request.urlopen", responder({"data.json": TimeoutError("timed out")})
        )
        b = fetch_source(DEMO, state_store, clock=clock)
        assert b.outcome is Outcome.CACHE_FALLBACK
        clock.advance(timedelta(seconds=5))
        return FakeResponse(json.dumps({"version": "from A"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", run_b_fails_meanwhile)
    result_a = fetch_source(DEMO, state_store, clock=clock)

    record = stored_record(state_store, DEMO)
    assert result_a.outcome is Outcome.LIVE
    assert record["payload"] == {"version": "from A"}
    assert record["last_outcome"] == "live"
    assert record["confirmed_at"] == clock.now.isoformat()


def test_overlapping_runs_ask_a_polite_source_once(state_store, monkeypatch):
    """Defects 3 and 4: two runs at once must not both ask Celestrak.

    The attempt is claimed in the store *before* the request is made. A run
    that finds the claim -- or loses the race to write it -- leaves the
    upstream alone.
    """
    clock = FakeClock()
    calls = serve(monkeypatch, {"gp.php": [{"a": 1}]})
    fetch_source(POLITE, state_store, clock=clock)
    clock.advance(timedelta(hours=2))
    seen_by_b: list = []

    def run_b_starts_meanwhile(request, timeout=None):
        calls.append(request)
        inner = responder({"gp.php": [{"a": "B asked too"}]}, calls=calls)
        monkeypatch.setattr("urllib.request.urlopen", inner)
        seen_by_b.append(fetch_source(POLITE, state_store, clock=clock))
        return FakeResponse(json.dumps([{"a": 2}]).encode())

    monkeypatch.setattr("urllib.request.urlopen", run_b_starts_meanwhile)
    result_a = fetch_source(POLITE, state_store, clock=clock)

    assert len(calls) == 2, "one request at the start, one from run A, none from run B"
    assert result_a.outcome is Outcome.LIVE
    assert stored_record(state_store, POLITE)["payload"] == [{"a": 2}]
    (b,) = seen_by_b
    assert b.outcome is Outcome.CACHE_FALLBACK
    assert b.data == [{"a": 1}]
    assert "has not recorded a result" in " ".join(b.notes)


class ReadOnlyStore:
    """A state store that can be read and refuses every write."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def describe(self) -> str:
        return "a store that has gone read-only"

    def read(self, key):
        return self._inner.read(key)

    def write(self, *args, **kwargs):
        raise StoreError("permission denied")


def test_unwritable_state_does_not_turn_into_a_request_every_run(
    state_store, monkeypatch
):
    """Defect 4: the floor depended on a write made *after* the request.

    With state readable but not writable, nothing recorded that Celestrak had
    been asked, so every run asked again: six requests in an hour. The
    attempt is now recorded first, and if it cannot be, the request is not
    made.
    """
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"a": 1}]})
    fetch_source(POLITE, state_store, clock=clock)
    clock.advance(timedelta(hours=2))

    calls = serve(monkeypatch, {"gp.php": [{"a": 2}]})
    read_only = ReadOnlyStore(state_store)
    for _run in range(6):
        result = fetch_source(POLITE, read_only, clock=clock)
        assert result.outcome is Outcome.CACHE_FALLBACK
        assert result.data == [{"a": 1}]
        assert "not contacted" in result.error
        clock.advance(timedelta(minutes=10))

    assert urls(calls) == []


def test_unwritable_state_on_a_first_run_makes_no_request(state_store, monkeypatch):
    calls = serve(monkeypatch, {"gp.php": [{"a": 1}]})

    for _run in range(3):
        result = fetch_source(POLITE, ReadOnlyStore(state_store))
        assert result.outcome is Outcome.FAILED
        assert "not contacted" in result.error

    assert urls(calls) == []


def test_run_killed_mid_request_is_not_repeated_by_the_next_run(state_store, monkeypatch):
    """Defects 1 and 4: an attempt that never finished still counts as asked.

    When the platform killed a run that was stuck reading a throttled body,
    no record of the attempt existed, so the next run asked again, and the
    next. The claim written before the request survives the kill.
    """
    clock = FakeClock()

    def killed(request, timeout=None):
        raise SystemExit("the platform stopped the task")

    monkeypatch.setattr("urllib.request.urlopen", killed)
    with pytest.raises(SystemExit):
        fetch_source(POLITE, state_store, clock=clock)

    clock.advance(timedelta(minutes=10))
    calls = serve(monkeypatch, {"gp.php": [{"a": 1}]})
    waiting = fetch_source(POLITE, state_store, clock=clock)

    assert urls(calls) == []
    assert waiting.outcome is Outcome.FAILED
    assert "has not recorded a result" in " ".join(waiting.notes)

    clock.advance(timedelta(minutes=20))  # thirty minutes after the claim: due
    retried = fetch_source(POLITE, state_store, clock=clock)
    assert retried.outcome is Outcome.LIVE
    assert len(calls) == 1


def test_claiming_an_attempt_keeps_the_stored_payload(state_store, monkeypatch):
    """The record written before the request must not lose what is stored."""
    clock = FakeClock()
    serve(monkeypatch, {"gp.php": [{"a": 1}]})
    fetch_source(POLITE, state_store, clock=clock)
    first_contact = clock.now
    clock.advance(timedelta(hours=2))
    seen: list[dict] = []

    def look_at_the_claim(request, timeout=None):
        seen.append(stored_record(state_store, POLITE))
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", look_at_the_claim)
    fetch_source(POLITE, state_store, clock=clock)

    (claim,) = seen
    assert claim["payload"] == [{"a": 1}]
    assert claim["confirmed_at"] == first_contact.isoformat()
    assert claim["last_attempt_at"] == clock.now.isoformat()
    assert claim["attempt_open"] is True
    assert stored_record(state_store, POLITE)["attempt_open"] is False


def test_truncated_group_is_refused_even_when_the_sample_is_full(
    state_store, monkeypatch
):
    """A group cut from thousands to hundreds still fills the sample of five.

    Comparing the kept objects would see five and five. The collapse is
    judged on how many objects the group *offered*.
    """
    records = json.loads(fixture_bytes("celestrak_starlink.json"))
    group = SatelliteGroup(key="starlink", label="Starlink", max_objects=5)
    spec = celestrak.group_spec(group)
    clock = FakeClock()
    serve(monkeypatch, {"GROUP=starlink": records})
    fetch_source(spec, state_store, clock=clock)

    clock.advance(CELESTRAK_MIN_REFETCH_INTERVAL)
    truncated = records[: len(records) // 4]
    assert len(truncated) >= group.max_objects
    serve(monkeypatch, {"GROUP=starlink": truncated})
    result = fetch_source(spec, state_store, clock=clock)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert "shrank" in result.error
    assert result.facts == {"available": len(records)}


def test_starlink_comes_from_the_small_recent_launches_group(state_store, monkeypatch):
    """Sixty Starlinks from an 86 KB response, not from the 5 MB Starlink group.

    The fixture is the real ``GROUP=last-30-days`` response of 2026-11-20: 204
    objects from every operator, 107 of them Starlink. The tracked group asks
    for that group by name, keeps the objects named STARLINK, and reports the
    107 as what was available -- the number the page's "60 of 107" is about.
    """
    group = next(g for g in TRACKED_GROUPS if g.key == "starlink")
    spec = celestrak.group_spec(group)
    assert "GROUP=last-30-days" in spec.url
    assert "GROUP=starlink" not in spec.url

    everything = json.loads(fixture_bytes("celestrak_last_30_days.json"))
    assert len(everything) == 204
    serve(monkeypatch, {"GROUP=last-30-days": everything})

    result = fetch_source(spec, state_store, clock=FakeClock())

    assert result.outcome is Outcome.LIVE
    assert len(result.data) == 60
    assert all(record["OBJECT_NAME"].startswith("STARLINK") for record in result.data)
    assert result.facts == {"available": 107}


def test_a_recent_launches_group_with_no_starlink_keeps_the_stored_group(
    state_store, monkeypatch
):
    """Thirty days without a Starlink launch must not empty the group."""
    group = next(g for g in TRACKED_GROUPS if g.key == "starlink")
    spec = celestrak.group_spec(group)
    everything = json.loads(fixture_bytes("celestrak_last_30_days.json"))
    serve(monkeypatch, {"GROUP=last-30-days": everything})
    clock = FakeClock()
    first = fetch_source(spec, state_store, clock=clock)
    assert first.outcome is Outcome.LIVE

    others = [r for r in everything if not r["OBJECT_NAME"].startswith("STARLINK")]
    serve(monkeypatch, {"GROUP=last-30-days": others})
    clock.advance(spec.min_refetch_interval + timedelta(minutes=1))
    second = fetch_source(spec, state_store, clock=clock)

    assert second.outcome is Outcome.CACHE_FALLBACK
    assert len(second.data) == 60
    assert "no object named STARLINK" in second.error


def test_celestrak_is_given_its_whole_ceiling_to_start_answering(
    state_store, monkeypatch
):
    """A slow first byte from Celestrak is waited for, up to the request's ceiling.

    Seen live on 2026-11-20: Celestrak took more than ten seconds to begin
    answering for a 90 KB group, and the ten-second silence limit cut each
    attempt short of an answer that was on its way. The socket timeout handed
    to the request must be the group's ceiling, not the default.
    """
    seen: list[float] = []

    def urlopen(request, timeout=None):
        seen.append(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    starlink = next(g for g in TRACKED_GROUPS if g.key == "starlink")
    stations = next(g for g in TRACKED_GROUPS if g.key == "stations")

    slow = fetch_source(celestrak.group_spec(starlink), state_store, clock=FakeClock())
    fetch_source(celestrak.group_spec(stations), state_store, clock=FakeClock())
    fetch_source(swpc.KP, state_store, clock=FakeClock())

    assert seen[0] == pytest.approx(starlink.fetch_seconds, abs=0.5)
    assert seen[1] == pytest.approx(stations.fetch_seconds, abs=0.5)
    # Everything else keeps the short default.
    assert seen[2] == pytest.approx(fetch_module.HTTP_STALL_TIMEOUT_SECONDS, abs=0.5)
    assert slow.outcome is Outcome.FAILED
    assert (
        slow.error == f"Timed out after {starlink.fetch_seconds:.0f}s without a response"
    )
