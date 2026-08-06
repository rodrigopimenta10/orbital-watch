"""Fake HTTP plumbing shared by the source and refresh test modules.

Kept out of conftest.py because these are helpers to call, not fixtures to
inject, and importing them by name reads better at the call site.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen returns."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def responder(mapping: dict[str, object], *, default=None):
    """Build a fake urlopen that serves payloads by URL substring.

    A mapping value may be bytes/str (served verbatim), a JSON-able object, or
    an Exception instance (raised, to simulate that source failing).
    """

    def _urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        for needle, payload in mapping.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
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


def fixture_bytes(name: str) -> bytes:
    return (Path(__file__).parent / "fixtures" / name).read_bytes()
