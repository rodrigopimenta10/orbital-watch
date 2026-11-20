"""Object storage behind a small interface.

The job keeps two things outside its own process: per-source state (what was
last fetched, and when) and the one document it publishes. In production both
live in Cloud Storage buckets; on a laptop, in CI and in tests they live in a
directory. Everything else in the project talks to :class:`Store` and does not
know which.

The interface is three operations, and the one that matters is the
*conditional* write. Every object has a generation that changes on each write.
A caller that read generation ``g`` and then writes with
``if_generation_match=g`` succeeds only if nobody wrote in between; otherwise
it gets :class:`PreconditionFailed` and the object is untouched. That is what
stops two overlapping runs from interleaving their writes: the loser finds out
and backs off, instead of silently replacing newer state with older.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: Generation that means "the object does not exist". Writing with
#: ``if_generation_match=ABSENT`` creates an object only if there is none.
#: Cloud Storage uses the same value for the same purpose.
ABSENT = 0

#: Timeout for one attempt at a storage operation, in seconds. Storage is in
#: the same region as the job and answers in tens of milliseconds; an attempt
#: that takes longer than this is not going to succeed usefully.
STORAGE_TIMEOUT_SECONDS = 10.0

#: How long one storage operation may go on being retried before it is
#: reported as failed. The client library's own default is two minutes, which
#: across the seven state reads of one run would be longer than the run is
#: allowed to take: a storage outage has to surface as a failed run promptly,
#: not as a run that hangs until the platform kills it.
STORAGE_RETRY_SECONDS = 15.0


class StoreError(Exception):
    """A storage operation failed for a reason other than a lost race."""


class PreconditionFailed(StoreError):
    """The object changed since it was read; nothing was written."""


@dataclass(frozen=True)
class StoredObject:
    """An object's bytes together with the generation they were read at."""

    data: bytes
    generation: int


class Store(Protocol):
    """What the job needs from storage. Keys are ``/``-separated names."""

    def describe(self) -> str:
        """Where this store keeps its objects, for log lines."""
        ...

    def read(self, key: str) -> StoredObject | None:
        """Return the object, or ``None`` if there is none. Raises StoreError."""
        ...

    def write(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str | None = None,
        if_generation_match: int | None = None,
    ) -> int:
        """Replace the object in one step and return its new generation.

        A reader sees the old bytes or the new bytes, never a mixture.
        ``if_generation_match`` makes the write conditional (see module
        docstring); ``None`` writes unconditionally. Raises
        :class:`PreconditionFailed` or :class:`StoreError`.
        """
        ...


def _check_key(key: str) -> PurePosixPath:
    """Reject keys that could escape the store or hide in it.

    Keys are built from constants in this project, never from upstream data.
    This exists so that stays true: a future caller that interpolates
    something external into a key gets an error instead of a path traversal.
    """
    path = PurePosixPath(key)
    if (
        not key
        or path.is_absolute()
        or any(part in ("", ".", "..") or part.startswith(".") for part in path.parts)
    ):
        raise StoreError(f"refusing unsafe storage key {key!r}")
    return path


# --------------------------------------------------------------------------
# Local directory
# --------------------------------------------------------------------------


class LocalStore:
    """A directory standing in for a bucket.

    Honours the same contract as Cloud Storage, including conditional writes,
    so the code paths the tests exercise are the ones production runs.

    Generations are derived from content: the generation of an object is a
    digest of its bytes. That needs no side file that could fall out of step
    with the data, and it has the property the contract needs -- if the bytes
    are what the caller last read, the write may proceed. (Two different
    writers producing identical bytes are indistinguishable, and harmless.)

    ``content_type`` and ``cache_control`` are accepted and ignored: a plain
    directory has nowhere to keep them, and whatever serves it decides its own
    headers.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def describe(self) -> str:
        return f"directory {self._root}"

    def _path(self, key: str) -> Path:
        return self._root.joinpath(*_check_key(key).parts)

    @staticmethod
    def _generation(data: bytes) -> int:
        # 63 bits, and never ABSENT, so "exists" and "absent" cannot collide.
        digest = int.from_bytes(hashlib.sha256(data).digest()[:8], "big") >> 1
        return digest or 1

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise compare-and-write across processes sharing this directory."""
        self._root.mkdir(parents=True, exist_ok=True)
        with open(self._root / ".lock", "a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self, key: str) -> StoredObject | None:
        path = self._path(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"could not read {path}: {exc}") from exc
        return StoredObject(data=data, generation=self._generation(data))

    def write(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str | None = None,
        if_generation_match: int | None = None,
    ) -> int:
        path = self._path(key)
        try:
            with self._locked():
                if if_generation_match is not None:
                    current = self.read(key)
                    found = current.generation if current else ABSENT
                    if found != if_generation_match:
                        raise PreconditionFailed(
                            f"{key} changed since it was read "
                            f"(expected generation {if_generation_match}, found {found})"
                        )
                path.parent.mkdir(parents=True, exist_ok=True)
                # Write beside the target and rename over it. The rename is
                # atomic, so a reader sees the old file or the new one and a
                # crash mid-write leaves the old one in place.
                handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
                try:
                    with os.fdopen(handle, "wb") as tmp:
                        tmp.write(data)
                    # mkstemp creates the file private to its owner. These are
                    # not secrets -- one of the two stores stands in for a
                    # public bucket -- and a web server running as another
                    # user has to be able to read what is published.
                    os.chmod(tmp_name, 0o644)
                    os.replace(tmp_name, path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_name)
                    raise
        except OSError as exc:
            raise StoreError(f"could not write {path}: {exc}") from exc
        return self._generation(data)


# --------------------------------------------------------------------------
# Cloud Storage
# --------------------------------------------------------------------------


class GcsStore:
    """A Cloud Storage bucket.

    Uses the official client library, which is imported here and nowhere else
    (see DECISIONS.md for why this is not hand-written HTTP). Authentication is
    whatever the environment provides: the job's own service account on Cloud
    Run, the developer's application-default credentials on a laptop. No key
    is ever read from a file by this code.
    """

    def __init__(
        self,
        bucket_name: str,
        *,
        client: Any = None,
        timeout: float = STORAGE_TIMEOUT_SECONDS,
        retry_seconds: float = STORAGE_RETRY_SECONDS,
        pinned_reads: bool = False,
    ) -> None:
        from google.cloud.storage.retry import DEFAULT_RETRY

        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self._bucket_name = bucket_name
        self._bucket = client.bucket(bucket_name)
        self._timeout = timeout
        # The library's standard policy (which errors are worth retrying, and
        # how to back off), with this project's limit on how long to keep at it.
        self._retry = DEFAULT_RETRY.with_deadline(retry_seconds)
        # For a bucket whose objects are public and cacheable. See read().
        self._pinned_reads = pinned_reads

    def describe(self) -> str:
        return f"bucket gs://{self._bucket_name}"

    def read(self, key: str) -> StoredObject | None:
        from google.api_core import exceptions as gcs_errors

        name = str(_check_key(key))
        if self._pinned_reads:
            return self._read_pinned(key, name)

        blob = self._bucket.blob(name)
        try:
            data = blob.download_as_bytes(timeout=self._timeout, retry=self._retry)
        except gcs_errors.NotFound:
            return None
        except Exception as exc:
            raise StoreError(
                f"could not read gs://{self._bucket_name}/{key}: {type(exc).__name__}"
            ) from exc
        # The client fills in the generation from the headers of the same
        # response that carried the bytes, so the two always belong together.
        if blob.generation is None:
            raise StoreError(
                f"gs://{self._bucket_name}/{key} was read without a generation; "
                f"refusing to guess one"
            )
        return StoredObject(data=data, generation=int(blob.generation))

    def _read_pinned(self, key: str, name: str) -> StoredObject | None:
        """Read the live object without trusting a cached copy of it.

        An object that is publicly readable and has a cache lifetime may be
        answered from Cloud Storage's built-in cache for that lifetime, and a
        stale answer would carry a stale generation. A conditional write
        made on the strength of it fails although nothing is wrong.

        Metadata is not cached, so the live generation is asked for first,
        and then that generation by number. A numbered generation never
        changes, so however it is served, its bytes are the right ones. If
        the object is replaced between the two calls the numbered generation
        is gone (the bucket keeps no old versions); that is looked at once
        more and then reported.
        """
        from google.api_core import exceptions as gcs_errors

        try:
            for _attempt in range(2):
                live = self._bucket.blob(name)
                try:
                    live.reload(timeout=self._timeout, retry=self._retry)
                except gcs_errors.NotFound:
                    return None
                if live.generation is None:
                    break
                generation = int(live.generation)
                pinned = self._bucket.blob(name, generation=generation)
                try:
                    data = pinned.download_as_bytes(
                        timeout=self._timeout, retry=self._retry
                    )
                except gcs_errors.NotFound:
                    continue
                return StoredObject(data=data, generation=generation)
        except Exception as exc:
            raise StoreError(
                f"could not read gs://{self._bucket_name}/{key}: {type(exc).__name__}"
            ) from exc
        raise StoreError(
            f"gs://{self._bucket_name}/{key} kept changing while it was being read"
        )

    def write(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str | None = None,
        if_generation_match: int | None = None,
    ) -> int:
        from google.api_core import exceptions as gcs_errors

        blob = self._bucket.blob(str(_check_key(key)))
        if cache_control is not None:
            blob.cache_control = cache_control
        try:
            # One request, one object: Cloud Storage makes the new bytes
            # visible all at once or not at all.
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=if_generation_match,
                timeout=self._timeout,
                # Retrying a write is only safe when it is conditional: if the
                # first attempt did land and its answer was lost, the retry is
                # refused by the precondition instead of writing twice. An
                # unconditional write is attempted once.
                retry=self._retry if if_generation_match is not None else None,
            )
        except gcs_errors.PreconditionFailed as exc:
            raise PreconditionFailed(
                f"gs://{self._bucket_name}/{key} changed since it was read"
            ) from exc
        except Exception as exc:
            raise StoreError(
                f"could not write gs://{self._bucket_name}/{key}: {type(exc).__name__}"
            ) from exc
        return int(blob.generation) if blob.generation is not None else ABSENT
