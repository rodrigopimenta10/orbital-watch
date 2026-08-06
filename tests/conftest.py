"""Shared test fixtures.

The most important thing in this file is :func:`block_network`, an autouse
fixture that makes any unmocked outbound HTTP call fail loudly. The brief
requires that no test touches the network; this enforces it mechanically
rather than by convention, so a future contributor cannot quietly reintroduce
a live call and have it pass on their machine and flake in CI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkAccessAttempted(AssertionError):
    """Raised when a test tries to reach the network."""


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail any test that attempts real network I/O."""

    def _forbidden(*args, **kwargs):
        target = args[0] if args else "<unknown>"
        raise NetworkAccessAttempted(
            f"Test attempted a live network call to {target!r}. Tests must run "
            f"entirely from committed fixtures."
        )

    monkeypatch.setattr("urllib.request.urlopen", _forbidden)
    return _forbidden


@pytest.fixture(autouse=True)
def isolate_seed(monkeypatch, tmp_path_factory):
    """Point the seed directory at an empty dir unless a test opts in.

    The committed ``seed/`` snapshot is a deployment feature: it keeps a build
    with no persistent cache from publishing an empty site. But it would also
    quietly rescue every degradation test, turning "upstream is down and we
    have nothing" into "upstream is down and we served the snapshot" — so the
    hard-failure paths would stop being covered at all.

    Tests that mean to exercise the seed set ``SEED_DIR`` themselves.
    """
    empty = tmp_path_factory.mktemp("no-seed")
    monkeypatch.setattr("orbital_watch.sources.fetch.SEED_DIR", empty)
    return empty


@pytest.fixture
def celestrak_snapshot(isolate_seed) -> Path:
    """Populate the isolated snapshot with Celestrak groups.

    Builds read Celestrak from the committed snapshot rather than the network,
    so any test that expects a *healthy* build needs one present. SWPC entries
    are deliberately left out, so space-weather degradation paths keep working.
    """
    stamp = datetime.now(UTC).isoformat()
    sources = {}
    for key, fixture in (
        ("celestrak_stations", "celestrak_stations.json"),
        ("celestrak_weather", "celestrak_weather.json"),
        ("celestrak_starlink", "celestrak_starlink.json"),
    ):
        (isolate_seed / f"{key}.json").write_text((FIXTURES / fixture).read_text())
        sources[key] = {"retrieved_at": stamp}
    (isolate_seed / "manifest.json").write_text(json.dumps({"sources": sources}))
    return isolate_seed


@pytest.fixture
def real_seed_dir(monkeypatch) -> Path:
    """Opt back in to the committed seed snapshot."""
    seed = Path(__file__).parent.parent / "seed"
    monkeypatch.setattr("orbital_watch.sources.fetch.SEED_DIR", seed)
    return seed


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


#: How old the seeded cache pretends to be. Chosen to sit past Celestrak's
#: 6h minimum refetch interval, so the rate-limit guard does not short-circuit
#: the fetch -- otherwise we would be testing the guard rather than the
#: upstream-is-down fallback we actually care about.
SEEDED_CACHE_AGE = timedelta(hours=7)


@pytest.fixture
def seeded_cache(tmp_path: Path) -> Path:
    """A cache directory pre-populated from fixtures, as a live run would leave it.

    Lets us exercise the "upstream is down but we have good cached data" path,
    which is the normal degraded state in production.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    retrieved = (datetime.now(UTC) - SEEDED_CACHE_AGE).isoformat()

    mapping = {
        "celestrak_stations": "celestrak_stations.json",
        "celestrak_weather": "celestrak_weather.json",
        "celestrak_starlink": "celestrak_starlink.json",
        "swpc_kp": "swpc_kindex.json",
        "swpc_solar_wind": "swpc_solar_wind_speed.json",
        "swpc_mag_field": "swpc_solar_wind_mag.json",
        "swpc_solar_cycle": "swpc_solar_cycle.json",
    }
    for cache_name, fixture_name in mapping.items():
        payload = (FIXTURES / fixture_name).read_text()
        (cache_dir / f"{cache_name}.json").write_text(payload)
        (cache_dir / f"{cache_name}.json.meta").write_text(
            json.dumps({"retrieved_at": retrieved})
        )
    return cache_dir
