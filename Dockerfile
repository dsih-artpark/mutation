# The application image runs no bioinformatics tools -- every stage executes in
# its own container through the host Docker daemon. So this is a plain Python
# image plus the Docker CLI, which is why it builds in seconds and why the
# JAVA_HOME / PERL5LIB / conda-prefix handling the old image needed is gone.
#
# Split into stages because the developer targets (pytest, tools/images.py) must
# also run in a container -- Docker is the only thing this project asks anyone to
# install -- and the image that ships must not carry what those targets need.
# `dev` adds requirements-dev.txt; `runtime` is what docker-compose builds.
#
# Production is linux/amd64 and an image built on an arm64 laptop must not
# quietly ship as arm64, so the platform is pinned rather than inherited from
# whoever runs the build. The Makefile overrides BASE_PLATFORM to the host's own
# architecture for the dev stage only: under Docker Desktop's amd64 emulation on
# an arm64 host, CPython's vfork/exec failure path loses the child's errno, so a
# missing executable comes back as exit status 127 instead of FileNotFoundError
# and test_missing_docker_binary_gives_an_actionable_error fails on a machine
# where nothing is wrong.
ARG BASE_PLATFORM=linux/amd64
FROM --platform=${BASE_PLATFORM} python:3.13-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI only -- no daemon. The worker talks to the host's daemon over the
# mounted socket to start per-tool containers (see docker-compose.yml). The dev
# stage inherits it for the same reason: tools/images.py and the docker-marked
# tests drive that same socket.
#
# The apt source's `arch=` is read from dpkg rather than written as a literal
# amd64: the dev stage builds for the developer's own architecture, and a
# hardcoded arch makes `apt-get update` fail there with a bare exit code 100.
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

# Dependencies before source, so a code change does not invalidate this layer.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1


# -- dev: the Makefile's test and tooling target. Never deployed. ------------
FROM base AS dev

# buildx here and not in base: `tools/images.py build` runs in this stage, and
# without the plugin the docker CLI silently falls back to the legacy builder,
# which is deprecated and ignores BuildKit-only Dockerfile syntax. The runtime
# image only ever runs `docker run`, so it does not pay for this.
RUN apt-get update \
 && apt-get install -y --no-install-recommends docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/*

# `-r requirements.txt` on the first line of this file resolves against the copy
# base already installed, so nothing is re-downloaded.
COPY requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir -r requirements-dev.txt

# No `COPY . /app` on purpose. Every dev invocation bind-mounts the working tree
# at its own host path (see the `dev` service in docker-compose.yml), so a baked
# copy would always be shadowed by the mount while still forcing an image
# rebuild on every source edit.


# -- runtime: the default build target, and what every compose service uses --
#
# Last stage in the file, so a bare `docker build .` cannot accidentally produce
# the image carrying pytest.
FROM base AS runtime

COPY . /app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
