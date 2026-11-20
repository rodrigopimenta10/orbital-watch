"""The storage interface and its two implementations.

The property that matters is the conditional write: it is what stops two
overlapping runs from interleaving. It is tested against the local store
directly, and against the Cloud Storage store through a fake client that
behaves as the real service is documented to -- no test here reaches Google.
"""

from __future__ import annotations

import threading

import pytest
from google.api_core import exceptions as gcs_errors

from orbital_watch.store import (
    ABSENT,
    GcsStore,
    LocalStore,
    PreconditionFailed,
    StoreError,
)

JSON = "application/json"


# --------------------------------------------------------------------------
# Local directory
# --------------------------------------------------------------------------


def test_absent_object_reads_as_none(tmp_path):
    assert LocalStore(tmp_path).read("state/missing.json") is None


def test_write_then_read_round_trips(tmp_path):
    store = LocalStore(tmp_path)
    generation = store.write("state/a.json", b'{"v":1}', content_type=JSON)

    stored = store.read("state/a.json")

    assert stored.data == b'{"v":1}'
    assert stored.generation == generation
    assert generation != ABSENT


def test_create_only_write_refuses_to_replace(tmp_path):
    """``if_generation_match=ABSENT`` means "only if there is nothing there"."""
    store = LocalStore(tmp_path)
    store.write("a.json", b"first", content_type=JSON, if_generation_match=ABSENT)

    with pytest.raises(PreconditionFailed):
        store.write("a.json", b"second", content_type=JSON, if_generation_match=ABSENT)

    assert store.read("a.json").data == b"first"


def test_stale_generation_is_refused_and_nothing_is_written(tmp_path):
    """The lost-update case: two runs read the same object, both write.

    The second writer's generation is out of date. It must be told so, and
    the first writer's bytes must survive.
    """
    store = LocalStore(tmp_path)
    store.write("a.json", b"original", content_type=JSON)
    seen_by_both = store.read("a.json").generation

    store.write("a.json", b"run one", content_type=JSON, if_generation_match=seen_by_both)
    with pytest.raises(PreconditionFailed):
        store.write(
            "a.json", b"run two", content_type=JSON, if_generation_match=seen_by_both
        )

    assert store.read("a.json").data == b"run one"


def test_matching_generation_is_accepted(tmp_path):
    store = LocalStore(tmp_path)
    first = store.write("a.json", b"one", content_type=JSON)
    second = store.write("a.json", b"two", content_type=JSON, if_generation_match=first)

    assert store.read("a.json").generation == second


def test_only_one_of_many_concurrent_conditional_writers_wins(tmp_path):
    """Racing writers that all read the same generation: exactly one succeeds."""
    store = LocalStore(tmp_path)
    store.write("a.json", b"start", content_type=JSON)
    generation = store.read("a.json").generation

    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def writer(index: int) -> None:
        barrier.wait()
        try:
            store.write(
                "a.json",
                f"writer {index}".encode(),
                content_type=JSON,
                if_generation_match=generation,
            )
            outcomes.append("won")
        except PreconditionFailed:
            outcomes.append("lost")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("won") == 1
    assert outcomes.count("lost") == 7
    assert store.read("a.json").data.startswith(b"writer ")


def test_write_leaves_no_temporary_files_behind(tmp_path):
    store = LocalStore(tmp_path)
    store.write("state/a.json", b"data", content_type=JSON)

    leftovers = [p.name for p in (tmp_path / "state").iterdir() if p.name != "a.json"]
    assert leftovers == []


@pytest.mark.parametrize(
    "key",
    ["", "/etc/passwd", "../outside.json", "state/../../outside.json", "state/.hidden"],
)
def test_unsafe_keys_are_refused(tmp_path, key):
    """Keys are constants today; this keeps a future mistake from escaping."""
    store = LocalStore(tmp_path / "root")
    with pytest.raises(StoreError):
        store.write(key, b"x", content_type=JSON)
    with pytest.raises(StoreError):
        store.read(key)
    assert not (tmp_path / "outside.json").exists()


def test_unwritable_root_is_a_store_error_not_an_oserror(tmp_path):
    """Callers catch StoreError; a raw OSError would slip past them."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the store's root should be")

    with pytest.raises(StoreError):
        LocalStore(blocker).write("a.json", b"x", content_type=JSON)


# --------------------------------------------------------------------------
# Cloud Storage, against a fake client
# --------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str, generation=None) -> None:
        self._bucket = bucket
        self.name = name
        self._pinned = generation
        self.generation = generation
        self.cache_control = None

    def reload(self, timeout=None, retry=None):
        """Metadata is never served from a cache: this is always the live object."""
        self._bucket.timeouts.append(timeout)
        if self._bucket.fail_with is not None:
            raise self._bucket.fail_with
        if self.name not in self._bucket.objects:
            raise gcs_errors.NotFound(self.name)
        self.generation = self._bucket.objects[self.name][1]

    def download_as_bytes(self, timeout=None, retry=None):
        self._bucket.timeouts.append(timeout)
        self._bucket.retries.append(("read", retry))
        if self._bucket.fail_with is not None:
            raise self._bucket.fail_with
        if self._pinned is not None:
            # A named generation is immutable: there is no stale copy of it.
            if self._pinned not in self._bucket.history.get(self.name, {}):
                raise gcs_errors.NotFound(self.name)
            return self._bucket.history[self.name][self._pinned]
        # An unpinned read of a publicly cacheable object may be answered
        # from Cloud Storage's own cache, with an older generation.
        cached = self._bucket.cached.get(self.name)
        if cached is not None:
            data, self.generation = cached
            return data
        if self.name not in self._bucket.objects:
            raise gcs_errors.NotFound(self.name)
        data, generation, _meta = self._bucket.objects[self.name]
        self.generation = generation
        return data

    def upload_from_string(
        self, data, content_type=None, if_generation_match=None, timeout=None, retry=None
    ):
        self._bucket.timeouts.append(timeout)
        self._bucket.retries.append(("write", retry))
        if self._bucket.fail_with is not None:
            raise self._bucket.fail_with
        current = self._bucket.objects.get(self.name)
        found = current[1] if current else 0
        if if_generation_match is not None and if_generation_match != found:
            raise gcs_errors.PreconditionFailed("conditionNotMet")
        self._bucket.counter += 1
        self.generation = self._bucket.counter
        self._bucket.objects[self.name] = (
            data,
            self.generation,
            {"content_type": content_type, "cache_control": self.cache_control},
        )
        # No versioning on this fake bucket: only the live generation exists.
        self._bucket.history[self.name] = {self.generation: data}


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, tuple] = {}
        self.history: dict[str, dict[int, bytes]] = {}
        self.cached: dict[str, tuple[bytes, int]] = {}
        self.counter = 1000
        self.fail_with: Exception | None = None
        self.timeouts: list = []
        self.retries: list = []

    def blob(self, name: str, generation=None) -> FakeBlob:
        return FakeBlob(self, name, generation)

    def serve_from_cache(self, name: str) -> None:
        """Freeze what unpinned reads of ``name`` return at its current state."""
        data, generation, _meta = self.objects[name]
        self.cached[name] = (data, generation)


class FakeClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket())


@pytest.fixture
def gcs():
    client = FakeClient()
    return GcsStore("example-bucket", client=client), client.bucket("example-bucket")


def test_gcs_absent_object_reads_as_none(gcs):
    store, _bucket = gcs
    assert store.read("latest.json") is None


def test_gcs_round_trip_carries_generation_and_headers(gcs):
    store, bucket = gcs
    generation = store.write(
        "latest.json", b"{}", content_type=JSON, cache_control="public, max-age=60"
    )

    stored = store.read("latest.json")
    _data, _generation, meta = bucket.objects["latest.json"]

    assert stored.data == b"{}"
    assert stored.generation == generation
    assert meta == {"content_type": JSON, "cache_control": "public, max-age=60"}


def test_gcs_precondition_failure_is_translated(gcs):
    """The library's 412 becomes this project's PreconditionFailed."""
    store, _bucket = gcs
    first = store.write("state/a.json", b"one", content_type=JSON)
    store.write("state/a.json", b"two", content_type=JSON, if_generation_match=first)

    with pytest.raises(PreconditionFailed):
        store.write(
            "state/a.json", b"three", content_type=JSON, if_generation_match=first
        )

    assert store.read("state/a.json").data == b"two"


def test_gcs_create_only_write(gcs):
    store, _bucket = gcs
    store.write("a.json", b"first", content_type=JSON, if_generation_match=ABSENT)
    with pytest.raises(PreconditionFailed):
        store.write("a.json", b"again", content_type=JSON, if_generation_match=ABSENT)


def test_gcs_every_call_carries_a_timeout(gcs):
    """No unbounded waits on storage either."""
    store, bucket = gcs
    store.write("a.json", b"x", content_type=JSON)
    store.read("a.json")

    assert bucket.timeouts and all(t is not None and t > 0 for t in bucket.timeouts)


def test_gcs_retries_are_bounded_and_only_for_writes_that_are_safe_to_repeat(gcs):
    """A storage outage must fail the run promptly, and never write twice.

    Reads and conditional writes are retried, for a bounded time far shorter
    than the client library's default. An unconditional write is not retried
    at all: if its first attempt landed and the answer was lost, a retry
    would write again.
    """
    from orbital_watch.store import STORAGE_RETRY_SECONDS

    store, bucket = gcs
    generation = store.write("a.json", b"one", content_type=JSON)
    store.read("a.json")
    store.write("a.json", b"two", content_type=JSON, if_generation_match=generation)

    (_, unconditional), (_, read), (_, conditional) = bucket.retries
    assert unconditional is None
    for policy in (read, conditional):
        assert policy is not None
        assert policy.deadline == STORAGE_RETRY_SECONDS < 60


@pytest.mark.parametrize(
    "failure",
    [gcs_errors.ServiceUnavailable("backend error"), gcs_errors.Forbidden("denied")],
)
def test_gcs_other_failures_become_store_errors(gcs, failure):
    store, bucket = gcs
    bucket.fail_with = failure

    with pytest.raises(StoreError) as read_error:
        store.read("a.json")
    with pytest.raises(StoreError) as write_error:
        store.write("a.json", b"x", content_type=JSON)

    assert not isinstance(read_error.value, PreconditionFailed)
    assert not isinstance(write_error.value, PreconditionFailed)


def test_gcs_store_refuses_unsafe_keys(gcs):
    store, _bucket = gcs
    with pytest.raises(StoreError):
        store.read("../other-bucket/secret")


# --------------------------------------------------------------------------
# Reads of an object that Cloud Storage may serve from its own cache
# --------------------------------------------------------------------------


def test_pinned_read_sees_the_live_object_when_a_plain_read_would_be_stale():
    """A public, cacheable object can be read back stale -- even by its writer.

    The published document is public with a cache lifetime, so Cloud Storage
    may answer a read of it from its built-in cache. A stale generation there
    would make the conditional publish fail against an object that changed a
    minute ago, twice, and the run exit non-zero although a newer document
    was already up. Reading the metadata first (never cached) and then that
    exact generation gives the bytes and the generation that belong together
    and are current.
    """
    client = FakeClient()
    bucket = client.bucket("public")
    plain = GcsStore("public", client=client)
    pinned = GcsStore("public", client=client, pinned_reads=True)

    first = plain.write("latest.json", b"older", content_type=JSON)
    bucket.serve_from_cache("latest.json")
    second = plain.write("latest.json", b"newer", content_type=JSON)

    stale = plain.read("latest.json")
    assert (stale.data, stale.generation) == (b"older", first), "the fake cache works"

    live = pinned.read("latest.json")
    assert (live.data, live.generation) == (b"newer", second)
    # And the generation it returns is one a conditional write will accept.
    pinned.write("latest.json", b"next", content_type=JSON, if_generation_match=second)


def test_pinned_read_of_an_absent_object_is_none():
    assert (
        GcsStore("public", client=FakeClient(), pinned_reads=True).read("x.json") is None
    )


def test_pinned_read_looks_again_if_the_object_is_replaced_between_its_two_calls():
    client = FakeClient()
    bucket = client.bucket("public")
    store = GcsStore("public", client=client, pinned_reads=True)
    store.write("latest.json", b"one", content_type=JSON)

    real_blob = bucket.blob
    raced = []

    def blob_with_a_race(name, generation=None):
        if generation is not None and not raced:
            raced.append(True)
            real_blob(name).upload_from_string(b"two", content_type=JSON)
        return real_blob(name, generation)

    bucket.blob = blob_with_a_race

    assert store.read("latest.json").data == b"two"
