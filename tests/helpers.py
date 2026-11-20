"""Fake HTTP plumbing and a movable clock, shared by the test modules.

Kept out of conftest.py because these are helpers to call, not fixtures to
inject, and importing them by name reads better at the call site.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

#: The moment the committed fixtures were captured, near enough. Tests that
#: propagate the fixture element sets pin their clock here: propagating
#: months-old elements to "today" tests the age of a checked-in file, not the
#: code (see DECISIONS.md §17 for the time that went wrong).
FIXTURE_TIME = datetime(2026, 8, 6, 15, 0, 0, tzinfo=UTC)


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeClock:
    """A clock the test moves by hand. Call it to read the time."""

    def __init__(self, start: datetime = FIXTURE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


#: A healthy answer from every upstream, keyed by a substring of its URL.
ALL_SOURCES_OK = {
    "GROUP=stations": fixture_bytes("celestrak_stations.json"),
    "GROUP=weather": fixture_bytes("celestrak_weather.json"),
    "GROUP=starlink": fixture_bytes("celestrak_starlink.json"),
    "planetary-k-index": fixture_bytes("swpc_kindex.json"),
    "solar-wind-speed": fixture_bytes("swpc_solar_wind_speed.json"),
    "solar-wind-mag-field": fixture_bytes("swpc_solar_wind_mag.json"),
    "solar-cycle": fixture_bytes("swpc_solar_cycle.json"),
}

CELESTRAK_KEYS = [key for key in ALL_SOURCES_OK if key.startswith("GROUP=")]
SWPC_KEYS = [key for key in ALL_SOURCES_OK if not key.startswith("GROUP=")]

FAILURE_MODES = {
    "timeout": TimeoutError("timed out"),
    "dns": urllib.error.URLError("Name or service not known"),
    "http_500": urllib.error.HTTPError(
        "https://example.invalid", 500, "Internal Server Error", {}, None
    ),
    "http_403": urllib.error.HTTPError(
        "https://example.invalid", 403, "Forbidden", {}, None
    ),
}


def responder(mapping: dict[str, object], *, default=None, calls: list | None = None):
    """Build a fake urlopen that serves payloads by URL substring.

    A mapping value may be bytes/str (served verbatim), a JSON-able object, a
    ready-made :class:`FakeResponse`, or an Exception instance (raised, to
    simulate that source failing). Every request made is appended to
    ``calls`` if a list is given, so a test can assert on what was -- and was
    not -- asked for.
    """

    def _urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if calls is not None:
            calls.append(request)
        for needle, payload in mapping.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, FakeResponse):
                    return FakeResponse(payload.getvalue(), payload.headers)
                if isinstance(payload, bytes):
                    return FakeResponse(payload)
                if isinstance(payload, str):
                    return FakeResponse(payload.encode())
                return FakeResponse(json.dumps(payload).encode())
        if default is None:
            raise urllib.error.URLError("no route to host (unmapped in test)")
        if isinstance(default, Exception):
            raise default
        return FakeResponse(json.dumps(default).encode())

    return _urlopen


def urls(calls: list) -> list[str]:
    """The URLs of the requests a ``responder`` recorded."""
    return [request.full_url for request in calls]
