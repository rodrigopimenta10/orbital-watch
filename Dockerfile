# syntax=docker/dockerfile:1
#
# The refresh job: one run of `python -m orbital_watch.job`, then exit.
# Cloud Scheduler starts it as a Cloud Run job every ten minutes.
#
# Two stages. The first has uv and builds a virtual environment from the
# lockfile; the second is the same Python base with only that environment
# copied in. Nothing that resolves or downloads packages is present in the
# image that runs.

# Base images are named by tag *and* digest. The tag says what it is; the
# digest is what is actually pulled, so a re-pushed tag cannot change what
# this builds from. Both are multi-architecture index digests.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build

# uv is copied from its own published image rather than installed with a
# script piped into a shell. Same version as CI uses.
COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in a layer of their own: they change far less often
# than the source, so most builds reuse it. --locked refuses to proceed if
# uv.lock and pyproject.toml disagree, instead of quietly resolving anew.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Then the project itself, installed as an ordinary package (not editable),
# so the next stage needs the environment and nothing else.
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM ${PYTHON_IMAGE}

# A fixed, unprivileged user. The job reads two buckets over HTTPS and
# writes nowhere on disk in production; it has no use for root.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=build /app/.venv /app/.venv

# The commit this image was built from. The job writes it into the document
# it publishes, which is how the deploy workflow knows the new image -- and
# not the previous one -- produced what it is looking at.
ARG REVISION=unknown

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ORBITAL_WATCH_REVISION=${REVISION} \
    ORBITAL_WATCH_LOCAL=/tmp/orbital-watch

# ORBITAL_WATCH_LOCAL only matters when the bucket variables are not set,
# i.e. when the image is run by hand or by CI's smoke test: it gives the
# local store somewhere writable. On Cloud Run the buckets are used and
# nothing is written here.

USER 10001:10001

ENTRYPOINT ["python", "-m", "orbital_watch.job"]
