"""The published document: its shape, the gate it must pass, and publication.

Everything the page shows comes from one JSON object. One object, because one
object write is atomic and five are not: a reader can never see this run's
passes beside the last run's health, or half a file.

Before a document is published it is checked against the shape the page
depends on and against the project's own honesty rules. A document that
fails is not published -- the previous one stays, and the run fails loudly.
Publishing something malformed is worse than publishing nothing: the previous
document is still true, just older, and the page already tells readers how
old.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from orbital_watch.config import DOCUMENT_KEY, REFRESH_CADENCE
from orbital_watch.health import State
from orbital_watch.sources.fetch import CONTACT_OUTCOMES, Outcome
from orbital_watch.store import ABSENT, PreconditionFailed, Store, StoreError

log = logging.getLogger(__name__)

#: Identifies the object to anything that fetches it, including the uptime
#: check, which looks for this value rather than settling for a 200.
DOCUMENT_KIND = "orbital-watch.latest"

#: Bumped when the shape changes in a way an older page could not read. The
#: page refuses a document whose version it does not know.
SCHEMA_VERSION = 2

#: How long browsers and Google's edge may reuse the published document. It
#: has to be short next to the cadence, or a reader's "latest" lags the real
#: one; it does not need to be zero, because the page computes every age from
#: timestamps inside the document rather than from when it was fetched.
DOCUMENT_CACHE_CONTROL = "public, max-age=60"

#: How far ahead of this run's document an already-published one may be dated
#: and still be believed to come from an overlapping run. A run cannot outlast
#: one cadence (the platform stops it well before), so nothing honest is
#: further ahead than that.
SUPERSEDE_HORIZON = REFRESH_CADENCE

_SEVERITIES = {"quiet", "elevated", "storm", "unknown"}

#: Outcomes for which a source may be reported FRESH: upstream answered, or
#: answered recently enough that it was deliberately not asked again.
_MAY_BE_FRESH = CONTACT_OUTCOMES | {Outcome.CACHE_FRESH}


class MalformedDocument(Exception):
    """The document failed validation and was not published."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _non_finite_paths(value: Any, path: str = "$") -> Iterable[str]:
    """Yield the path of every NaN or infinity anywhere in the document."""
    if isinstance(value, float) and not math.isfinite(value):
        yield path
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _non_finite_paths(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _non_finite_paths(child, f"{path}[{index}]")


def validate(document: Any, *, expected_sources: Iterable[str]) -> list[str]:
    """Return everything wrong with ``document``; an empty list means publish.

    Checks two kinds of thing. Shape: every field the page reads is present
    and of the type it expects, so a rendering fault cannot be caused by this
    side. Honesty: the rules the README promises are re-checked on the
    finished document, independently of the code that is supposed to uphold
    them -- no source is fresh without upstream contact or without a date,
    and every source is accounted for.
    """
    problems: list[str] = []

    def need(condition: bool, message: str) -> bool:
        if not condition:
            problems.append(message)
        return condition

    if not need(isinstance(document, dict), "document is not an object"):
        return problems

    problems.extend(
        f"non-finite number at {path}" for path in _non_finite_paths(document)
    )

    need(document.get("kind") == DOCUMENT_KIND, f"kind is not {DOCUMENT_KIND!r}")
    need(
        document.get("schema_version") == SCHEMA_VERSION,
        f"schema_version is not {SCHEMA_VERSION}",
    )
    need(_is_time(document.get("generated_at")), "generated_at is not a timestamp")

    sections = {}
    for name in ("meta", "sky", "passes", "space_weather", "health"):
        section = document.get(name)
        if need(isinstance(section, dict), f"{name} is missing or not an object"):
            sections[name] = section
    if len(sections) < 5:
        return problems

    _validate_meta(sections["meta"], need)
    _validate_sky(sections["sky"], need)
    _validate_passes(sections["passes"], need)
    _validate_weather(sections["space_weather"], need)
    _validate_health(sections["health"], list(expected_sources), need)
    return problems


def _validate_meta(meta: dict, need) -> None:
    observer = meta.get("observer")
    if need(isinstance(observer, dict), "meta.observer is missing"):
        need(isinstance(observer.get("name"), str), "meta.observer.name is not text")
        for key in ("latitude_deg", "longitude_deg"):
            need(_is_number(observer.get(key)), f"meta.observer.{key} is not a number")

    pipeline = meta.get("pipeline")
    if need(isinstance(pipeline, dict), "meta.pipeline is missing"):
        for key in ("cadence_seconds", "silence_limit_seconds"):
            value = pipeline.get(key)
            need(_is_number(value) and value > 0, f"meta.pipeline.{key} is not positive")

    need(isinstance(meta.get("coverage"), list), "meta.coverage is not a list")
    need(isinstance(meta.get("counts"), dict), "meta.counts is missing")
    need(isinstance(meta.get("parameters"), dict), "meta.parameters is missing")


def _validate_sky(sky: dict, need) -> None:
    need(_is_number(sky.get("horizon_elevation_deg")), "sky.horizon_elevation_deg")
    ephemeris = sky.get("ephemeris")
    satellites = sky.get("satellites")
    if not need(isinstance(ephemeris, dict), "sky.ephemeris is missing"):
        return
    if not need(isinstance(satellites, list), "sky.satellites is not a list"):
        return

    need(_is_time(ephemeris.get("start")), "sky.ephemeris.start is not a timestamp")
    need(_is_time(ephemeris.get("end")), "sky.ephemeris.end is not a timestamp")
    step = ephemeris.get("step_seconds")
    need(_is_number(step) and step > 0, "sky.ephemeris.step_seconds is not positive")
    samples = ephemeris.get("samples")
    if not need(
        isinstance(samples, int) and samples >= 2,
        "sky.ephemeris.samples must be at least 2 to interpolate between",
    ):
        return

    for index, satellite in enumerate(satellites):
        where = f"sky.satellites[{index}]"
        if not need(isinstance(satellite, dict), f"{where} is not an object"):
            continue
        need(isinstance(satellite.get("name"), str), f"{where}.name is not text")
        need(isinstance(satellite.get("group"), str), f"{where}.group is not text")
        need(
            _is_time(satellite.get("tle_epoch")), f"{where}.tle_epoch is not a timestamp"
        )
        track = satellite.get("track")
        if not need(
            isinstance(track, list) and len(track) == samples,
            f"{where}.track does not have {samples} samples",
        ):
            continue
        need(
            all(
                isinstance(sample, list)
                and len(sample) == 4
                and all(_is_number(value) for value in sample)
                for sample in track
            ),
            f"{where}.track has a sample that is not four numbers",
        )


def _validate_passes(passes: dict, need) -> None:
    need(isinstance(passes.get("complete"), bool), "passes.complete is not a boolean")
    need(_is_number(passes.get("min_elevation_deg")), "passes.min_elevation_deg")
    need(_is_number(passes.get("search_hours")), "passes.search_hours")
    items = passes.get("passes")
    if not need(isinstance(items, list), "passes.passes is not a list"):
        return
    for index, item in enumerate(items):
        where = f"passes.passes[{index}]"
        if not need(isinstance(item, dict), f"{where} is not an object"):
            continue
        times = [item.get(key) for key in ("start", "peak", "end")]
        if need(all(_is_time(t) for t in times), f"{where} has a bad timestamp"):
            start, peak, end = (datetime.fromisoformat(t) for t in times)
            need(start <= peak <= end, f"{where} is not ordered start, peak, end")
        need(isinstance(item.get("name"), str), f"{where}.name is not text")
        need(
            _is_number(item.get("peak_elevation_deg")),
            f"{where}.peak_elevation_deg is not a number",
        )
        need(
            _is_number(item.get("duration_minutes")),
            f"{where}.duration_minutes is not a number",
        )


def _validate_weather(block: dict, need) -> None:
    weather = block.get("weather")
    correlation = block.get("correlation")
    if need(isinstance(weather, dict), "space_weather.weather is missing"):
        need(
            isinstance(weather.get("available"), bool),
            "space_weather.weather.available is not a boolean",
        )
        need(
            isinstance(weather.get("kp_history"), list),
            "space_weather.weather.kp_history is not a list",
        )
        if weather.get("available"):
            need(
                _is_number(weather.get("kp")),
                "space_weather.weather.kp is missing although marked available",
            )
    if need(isinstance(correlation, dict), "space_weather.correlation is missing"):
        need(
            correlation.get("severity") in _SEVERITIES,
            "space_weather.correlation.severity is not a known value",
        )
        need(
            isinstance(correlation.get("headline"), str),
            "space_weather.correlation.headline is not text",
        )


def _validate_health(health: dict, expected: list[str], need) -> None:
    sources = health.get("sources")
    if not need(isinstance(sources, list), "health.sources is not a list"):
        return

    names = [s.get("name") for s in sources if isinstance(s, dict)]
    need(
        sorted(names) == sorted(expected),
        f"health.sources does not account for exactly the expected sources "
        f"(expected {sorted(expected)}, found {sorted(map(str, names))})",
    )

    states = []
    for index, source in enumerate(sources):
        where = f"health.sources[{index}]"
        if not need(isinstance(source, dict), f"{where} is not an object"):
            continue
        state, outcome = source.get("state"), source.get("outcome")
        state_ok = need(state in set(State), f"{where}.state is not a known state")
        outcome_ok = need(
            outcome in set(Outcome), f"{where}.outcome is not a known outcome"
        )
        need(isinstance(source.get("detail"), str), f"{where}.detail is not text")
        last_success = source.get("last_success")
        need(
            last_success is None or _is_time(last_success),
            f"{where}.last_success is neither null nor a timestamp",
        )
        thresholds = source.get("thresholds")
        need(
            isinstance(thresholds, dict)
            and _is_number(thresholds.get("fresh_within_seconds"))
            and _is_number(thresholds.get("stale_within_seconds")),
            f"{where}.thresholds are missing",
        )
        if state_ok:
            states.append(state)
        # The honesty rules, checked on the output itself.
        if state_ok and outcome_ok and state == State.FRESH:
            need(
                Outcome(outcome) in _MAY_BE_FRESH,
                f"{where} claims fresh with outcome {outcome!r}; a fallback is "
                f"never fresh",
            )
            need(last_success is not None, f"{where} claims fresh without a date")

    worst = (
        State.FAILED
        if State.FAILED in states
        else State.STALE
        if State.STALE in states
        else State.FRESH
    )
    need(
        health.get("overall") == worst,
        "health.overall does not match the worst source state",
    )
    counts = health.get("counts")
    need(
        isinstance(counts, dict)
        and all(counts.get(state.value) == states.count(state) for state in State),
        "health.counts does not match the source states",
    )


# --------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------


def serialize(document: dict) -> bytes:
    """Encode a document. Raises ValueError on anything JSON cannot express."""
    return json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")


def publish(document: dict, store: Store, *, expected_sources: Iterable[str]) -> str:
    """Validate and publish. Returns ``"published"`` or ``"superseded"``.

    ``"superseded"`` means a document newer than this one is already
    published -- an overlapping run got there first -- and it was left alone.
    That is a success: what readers see is truthful and at least as recent.

    Raises :class:`MalformedDocument` if validation fails, in which case
    nothing was written, and :class:`~orbital_watch.store.StoreError` if the
    store could not be read or written.
    """
    problems = validate(document, expected_sources=expected_sources)
    if problems:
        raise MalformedDocument(problems)
    try:
        body = serialize(document)
    except (ValueError, TypeError) as exc:
        raise MalformedDocument([f"document cannot be encoded as JSON: {exc}"]) from exc

    ours = datetime.fromisoformat(document["generated_at"])

    # Two attempts. The write is conditional on the object being unchanged
    # since we looked at it, so that "is there something newer?" and "replace
    # it" cannot be separated by another run's publish. Losing that race once
    # is ordinary when two runs overlap; losing it twice in one run is not,
    # and is reported rather than retried indefinitely.
    for _attempt in range(2):
        current = store.read(DOCUMENT_KEY)
        if current is not None:
            theirs = _generated_at(current.data)
            if theirs is not None and ours < theirs <= ours + SUPERSEDE_HORIZON:
                log.warning(
                    "not publishing: a newer document (generated %s) is already "
                    "published",
                    theirs.isoformat(),
                )
                return "superseded"
            if theirs is not None and theirs > ours:
                # Dated further ahead than any overlapping run could be: a
                # bad clock somewhere, or not one of ours. Standing down for
                # it would mean publishing nothing, successfully, until the
                # wall clock caught up with it.
                log.warning(
                    "the published document is dated %s, which no overlapping "
                    "run could have produced; replacing it",
                    theirs.isoformat(),
                )
        try:
            store.write(
                DOCUMENT_KEY,
                body,
                content_type="application/json",
                cache_control=DOCUMENT_CACHE_CONTROL,
                if_generation_match=current.generation if current else ABSENT,
            )
        except PreconditionFailed:
            log.warning("the published document changed while publishing; looking again")
            continue
        except StoreError:
            # A write can land and its answer be lost. Before calling this a
            # failed run -- which alerts the owner that readers got nothing
            # new -- look at what is actually published.
            if not _is_published(store, body):
                raise
            log.warning(
                "the store reported a failed write, but the document is published"
            )
        log.info(
            "published %s (%d bytes) to %s", DOCUMENT_KEY, len(body), store.describe()
        )
        return "published"

    raise StoreError("the published document kept changing; gave up after two attempts")


def _is_published(store: Store, body: bytes) -> bool:
    """Whether exactly these bytes are what is published now. Never raises."""
    try:
        current = store.read(DOCUMENT_KEY)
    except StoreError:
        return False
    return current is not None and current.data == body


def _generated_at(body: bytes) -> datetime | None:
    """The generation time of an already-published document, if it has one."""
    try:
        stamp = datetime.fromisoformat(json.loads(body)["generated_at"])
    except (ValueError, KeyError, TypeError):
        return None
    return stamp if stamp.tzinfo else None
