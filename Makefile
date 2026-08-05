# Every target here runs inside a container. Docker is the only thing this
# project asks anyone to install: no host Python, no host pytest, no host pip.
#
# DHARINI_REPO is the host path of this checkout, and docker-compose.yml mounts
# the tree at that same absolute path inside the dev container.
#
# It has to be the same path because the containers this one starts get bind
# mounts, and the Docker daemon resolves a bind-mount source against the HOST
# filesystem, never against the container that asked for it. Parity is what
# makes a single path string -- BASETEMP below, and everything pytest creates
# under it -- valid on both sides at once. Mounted at /app instead, the daemon
# would find no /app on the host, create an empty directory, mount that, and the
# tool would report "file not found" against data that plainly exists. Identical
# trap to RUNS_ROOT, documented at the top of docker-compose.yml.
#
# Build contexts are not affected: the docker CLI reads and streams those itself,
# so `tools/images.py build` would work from any path. The mount is not for it.
export DHARINI_REPO := $(CURDIR)

# The dev container is built for this machine's own architecture; only the
# production image is pinned to linux/amd64. See the BASE_PLATFORM comment in
# the Dockerfile for what emulation breaks. Derived from uname rather than from
# `docker version` so that `make help` costs nothing; the two disagree only when
# DOCKER_HOST points at a remote daemon of a different architecture.
DEV_ARCH := $(patsubst aarch64,arm64,$(patsubst x86_64,amd64,$(shell uname -m)))
export DHARINI_DEV_PLATFORM := linux/$(DEV_ARCH)

COMPOSE := docker compose

# --build: rebuilds the dev image when the Dockerfile or requirements-dev.txt
# change, and is a cache hit costing under a second when they have not. Without
# it an edit to requirements-dev.txt silently never takes effect.
#
# --no-deps: the dev service declares no depends_on, and this makes that
# non-negotiable from the call site. The suite runs against SQLite with Celery
# in eager mode, so a depends_on added to the service later would start Postgres
# and Redis on every `make test-fast` for services nothing connects to.
DEV := $(COMPOSE) run --rm --build --no-deps dev

# Same container, no --build, so stdout carries only what the command printed.
# Used where the output is parsed rather than read.
DEV_QUIET := $(COMPOSE) run --rm --no-deps -T dev

# pytest's tmp_path is bind-mounted into the sibling containers the docker-marked
# tests start, so it has to be a path that exists identically on the host. The
# dev container's own /tmp is not; a directory under the repo mount is. It sits
# under runs/ because that is the one scratch path .gitignore and .dockerignore
# already cover, so a test run leaves neither an untracked directory behind nor
# a new entry in the build context.
BASETEMP := --basetemp=$(CURDIR)/runs/.pytest-tmp

.PHONY: help dev-image images-build images-push images-lock images-pull images-list \
        test test-fast check shell fixtures smoke

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

dev-image:     ## Build the container every target below runs in
	$(COMPOSE) build dev

images-build:  ## Build every image declared with source: build
	$(DEV) python tools/images.py build $(if $(SCOPE),--scope $(SCOPE),)

# The one target that does NOT run in the dev container. Pushing is the only
# operation here that needs registry credentials, and those do not cross the
# container boundary: `docker login` stores them via a credential helper
# (`credsStore: desktop` on Docker Desktop, which reads the OS keychain), so a
# container that mounts only the socket authenticates as nobody and the push
# fails with `insufficient_scope: authorization failed` -- a message that reads
# like a permissions problem on the account rather than a missing credential.
#
# Mounting ~/.docker/config.json would not fix it: with a helper configured the
# file holds no secret to mount. So this uses the host CLI, which is already the
# prerequisite for every other target. In CI, docker/login-action writes real
# credentials into the runner's config and this works unchanged.
# Depends on dev-image, and then runs WITHOUT --build: `docker compose run
# --build` writes buildx progress to stdout, which would be captured as if it
# were part of the reference list. Building first keeps this command's stdout to
# exactly the refs it was asked for.
images-push: dev-image  ## Push every built image to the registry (uses host credentials)
	@$(DEV_QUIET) python tools/images.py refs --build-only $(if $(SCOPE),--scope $(SCOPE),) \
	  | tr -d '\r' \
	  | while read -r ref; do \
	      case "$$ref" in ''|*' '*) continue;; esac; \
	      echo "=== push $$ref ==="; \
	      docker push "$$ref" || exit 1; \
	    done

images-lock:   ## Resolve digests and write images.lock.json
	$(DEV) python tools/images.py lock

images-pull:   ## Pull every image at its locked digest
	$(DEV) python tools/images.py pull

images-list:   ## The whole image catalogue: every scope, every key, who uses it
	$(DEV) python tools/images.py list $(if $(SCOPE),--scope $(SCOPE),)

test:          ## Full suite, including container-backed runner tests
	docker pull bash:5.2
	$(DEV) python -m pytest tests/ -v $(BASETEMP)

test-fast:     ## Skip anything that needs Docker
	$(DEV) python -m pytest tests/ -q -m "not docker"

check:         ## Contract and manifest validation only
	$(DEV) python -m pytest tests/test_all_pipelines.py tests/test_manifest.py tests/test_images.py -q

fixtures:      ## Synthesise deterministic pipeline inputs into fixtures/
	$(DEV) python tools/make_fixtures.py

# The end-to-end check an author runs before opening a PR. Same RunContext, same
# schema validation, same stage containers as the worker -- only Postgres, Redis,
# the queue and HTTP are absent.
#
# RUNS_ROOT and REFS_ROOT are pinned to paths inside the checkout so this works
# on a fresh clone with no .env: the compose defaults point at /srv/dharini,
# which exists on the server and on no developer's machine.
smoke:         ## End-to-end run on generated fixtures (PIPELINE=, INPUT=, ARGS=)
	$(DEV) env RUNS_ROOT=$(CURDIR)/runs REFS_ROOT=$(CURDIR)/data \
	  python tools/run_local.py $(or $(PIPELINE),ont-lsdv) \
	    $(or $(INPUT),fixtures/reads.fastq.gz) $(ARGS)

shell:         ## Interactive shell in the dev container
	$(DEV) bash
