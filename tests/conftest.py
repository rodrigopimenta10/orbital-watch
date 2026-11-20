"""Shared test fixtures.

The most important thing in this file is :func:`block_network`, an autouse
fixture that makes any unmocked outbound call fail loudly. The brief requires
that no test touches the network; this enforces it mechanically rather than
by convention, so a future contributor cannot quietly reintroduce a live call
and have it pass on their machine and flake in CI.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from orbital_watch.store import LocalStore

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkAccessAttempted(AssertionError):
    """Raised when a test tries to reach the network."""


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail any test that attempts real network I/O.

    Two layers. ``urlopen`` is what this project's own fetch code calls, and
    is the layer tests replace with fakes. The socket layer underneath
    catches everything else -- in particular the Cloud Storage client
    library, which does not go through ``urlopen`` at all.
    """

    def _forbidden(*args, **kwargs):
        target = args[0] if args else "<unknown>"
        raise NetworkAccessAttempted(
            f"Test attempted a live network call to {target!r}. Tests must run "
            f"entirely from committed fixtures."
        )

    def _no_socket(self, address, *args, **kwargs):
        raise NetworkAccessAttempted(
            f"Test attempted to open a socket to {address!r}. Tests must run "
            f"entirely from committed fixtures."
        )

    monkeypatch.setattr("urllib.request.urlopen", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_socket)
    return _forbidden


@pytest.fixture
def state_store(tmp_path: Path) -> LocalStore:
    """An empty state store: the job's first-ever run."""
    return LocalStore(tmp_path / "state")


@pytest.fixture
def public_store(tmp_path: Path) -> LocalStore:
    """An empty public store: nothing has been published yet."""
    return LocalStore(tmp_path / "public")


def load_fixture(name: str):
    """Load a committed fixture captured from the real upstream API."""
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def stations_records() -> list[dict]:
    return load_fixture("celestrak_stations.json")


@pytest.fixture
def weather_records() -> list[dict]:
    return load_fixture("celestrak_weather.json")


@pytest.fixture
def kp_records() -> list:
    return load_fixture("swpc_kindex.json")
