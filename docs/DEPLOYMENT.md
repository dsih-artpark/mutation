# Deploying the Dharini Pipeline Service

The service ships as one application image, `dsihartpark/dharini-mutation-pipeline`,
plus tool images that the worker starts for each pipeline stage. You deploy by
version; nothing is built or cloned on the server. The application image
contains `images.lock.json`, so one version pins the code and the exact tool
images it uses.

On AWS, also read [deploy-aws-ec2.md](deploy-aws-ec2.md).

## Requirements

- Linux **x86_64**. ARM hosts only work under emulation, and several tool images have no ARM build.
- Docker Engine with Compose v2, plus `curl` and `jq`.
- About 9 GB for tool images, and RAM and CPU as in [Sizing](../readme.md#sizing).
- An S3 bucket, or an S3-compatible store such as MinIO.
- A host dedicated to this service. The worker mounts the Docker socket to start tool containers, which is root-equivalent access to the host.

## Quick start

```bash
sudo mkdir -p /srv/dharini/runs /srv/dharini/references
sudo chown -R $USER /srv/dharini
mkdir -p /srv/dharini/service && cd /srv/dharini/service

IMAGE=dsihartpark/dharini-mutation-pipeline
VERSION=1.0.0     # see https://hub.docker.com/r/dsihartpark/dharini-mutation-pipeline/tags

# compose.yaml, deploy.sh and .env.example ship inside the image, so they always
# match the version being deployed.
for f in compose.yaml deploy.sh; do docker run --rm $IMAGE:$VERSION cat /app/deploy/$f > $f; done
docker run --rm $IMAGE:$VERSION cat /app/.env.example > .env
chmod +x deploy.sh

$EDITOR .env
./deploy.sh $VERSION
```

`deploy.sh` exits non-zero unless `GET /health` reports `status: ok`, reachable
storage and a schema revision.

Use `latest` to follow the newest release, or pin a release tag (`1.0.0`) or an
immutable commit tag (`sha-1a2b3c4`) for repeatable deploys. `edge` (the tip of
`main`) is refused unless you pass `--allow-mutable`.

## Configuration

Edit `.env`. `.env.example` lists every required variable; anything else has a
default in `app/config.py`.

| Variable | Notes |
| --- | --- |
| `S3_BUCKET`, `S3_REGION`, `S3_PREFIX` | Where inputs are read and results written. `S3_PREFIX` must equal the producer's folder. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Both or neither. Empty uses the AWS default credential chain. `S3_ENDPOINT_URL` for MinIO. |
| `API_KEYS` | `label:secret` pairs. Required unless `ALLOW_UNAUTHENTICATED=true` (development only). |
| `WEBHOOK_SECRET`, `WEBHOOK_ALLOWED_HOSTS` | HMAC secret shared with the receiver, and the hosts the worker may call back. |
| `POSTGRES_PASSWORD` | Avoid `$` in it. |
| `RUNS_ROOT`, `REFS_ROOT` | Absolute host paths, mounted at the same path in the containers. |
| `DOCKER_GID` | `getent group docker \| cut -d: -f3` |
| `REQUIRE_PINNED_IMAGES` | `true` in production. |
| `API_BIND` | Interface for port 8000. Default `127.0.0.1`; use `0.0.0.0` to accept connections from other hosts, then restrict the port in your firewall. |
| `COMPOSE_PROJECT_NAME` | Default `dharini`. It prefixes the Docker volume names, so changing it starts on an empty database. |
| `DHARINI_IMAGE` | Only if you publish your own image. |

`deploy.sh` writes `APP_VERSION` into `.env`; do not edit it.

## Upgrading

```bash
./deploy.sh 1.1.0      # or: ./deploy.sh latest
```

It validates your setup, pulls the application and tool images, takes a database
dump to `backups/` (last five kept), restarts the stack and waits for `/health`.
On failure it restores the previous version. Options: `--no-backup`,
`--skip-tool-images`, `--no-pull`, `--new-database`, `--allow-mutable`.
`./deploy.sh status` shows the running version and recent deploys.

Migrations run at API startup. A run in progress when the worker restarts is
lost and must be resubmitted; `external_id` makes that safe.

## Rolling back

```bash
./deploy.sh 1.0.0
```

A failed deploy restores the previous image automatically, including when it
was `latest`. Rolling back restores code, not the database schema. If the newer version ran a
migration, check `schema_revision` on `/health` against what the older code
expects, or restore the dump from `backups/`.

## Moving an existing installation

If the stack was previously started with `docker compose up --build` from a git
checkout, its volumes are named after that directory:

```bash
docker volume ls | grep _postgres_data     # app_postgres_data  ->  project "app"
```

Copy the old `.env` into the new service directory and add:

```bash
COMPOSE_PROJECT_NAME=app     # the prefix found above
API_BIND=0.0.0.0             # the old compose published port 8000 on all interfaces
```

then run `./deploy.sh <version>`. It refuses to start if the project name would
create a new empty database.

## Releases (maintainers)

| Event | Result |
| --- | --- |
| Merge to `main` | After the tests pass, `:sha-<commit>` and `:edge` are pushed |
| Tag `vX.Y.Z` | `:X.Y.Z`, `:X.Y` and `:latest` are pushed |
| Dockerfile or manifest change | A maintainer runs the `images` workflow on the PR branch; it pushes the tool images and commits the refreshed `images.lock.json` |

Repository secrets: `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`. Pull requests
never see them. To deploy from CI, have your pipeline run `deploy.sh <version>`
on the host (for example over SSH or AWS SSM) from a private repository, so
that no server credentials live in this one.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `APP_VERSION is not set` | Use `./deploy.sh <version>` rather than `docker compose` directly. |
| Refuses to start: `API_KEYS is empty` | Set `API_KEYS`, or `ALLOW_UNAUTHENTICATED=true` for development. |
| `422 … does not exist` on every submission | `S3_PREFIX` differs from the producer's folder. |
| Tool reports "file not found" for existing data | `RUNS_ROOT` or `REFS_ROOT` differs between host and container. See [Path parity](../readme.md#-path-parity). |
| `permission denied` on `docker.sock` | `DOCKER_GID` is not the host's docker group. |
| Cannot reach the API from another host | `API_BIND` is still loopback. |
| Deploy rolled back | Read the output, then `docker compose -f compose.yaml logs api worker`. |

## Database migrations

`init_db()` runs `alembic upgrade head` automatically at API startup, so a
**fresh** database needs no manual step. A database this service already
created before Alembic was adopted (with SQLAlchemy's `create_all()`) needs
one step, once, before the first deploy of this version:

```bash
docker compose -f compose.yaml run --rm --no-deps api alembic stamp head   # records "head" WITHOUT running any DDL
```

Skip it and Alembic tries to `CREATE TABLE` over tables that already exist,
and fails loudly rather than silently — but it is still a deploy-day surprise
worth avoiding by doing this first. From then on:

```bash
docker compose -f compose.yaml run --rm --no-deps api alembic upgrade head   # bring a database to head by hand
make migration m="…"                                                        # (developers) generate a new revision after changing app/models.py
```

`GET /health` reports `schema_revision`, precisely so "the migration didn't
run" and "the code is newer than the schema" don't look identical from
outside.

## `/health` and `/v1/system`

Two endpoints, split by who can reach them.

**`GET /health` — unauthenticated, and carries nothing that helps a caller
aim.** This is the route that ends up exposed: load-balancer target groups,
uptime monitors and container probes all hit it, and it is the first thing
anyone opens to the world.

```json
{
  "status": "ok", "pipelines": 2, "worker_concurrency": 1,
  "schema_revision": "0002",
  "storage": {"backend": "s3", "reachable": true},
  "disk": {"free_gb": 81.4, "total_gb": 102.9, "used_percent": 20.9}
}
```

Always `200` while the process is up. Storage being unreachable is reported in
the body rather than failing the check: taking every instance out of rotation
because S3 blipped turns a degradation into an outage. This is what tells "the
process is dead" apart from "the process is fine but cannot reach S3" apart
from "the schema is behind the code" — three failure modes that otherwise look
identical from outside, at 2am, on a run that has been computing for forty
minutes.

On failure `storage.error` is the exception **type** and nothing more
(`NoCredentialsError`, `EndpointConnectionError`, `ClientError`). Those already
separate the failure modes worth acting on, while the message would hand an
unauthenticated caller the bucket name, the key and sometimes the account id.

**`GET /v1/system` — behind the credential, and carries the detail.**

```bash
curl -s localhost:8000/v1/system | jq
```

```jsonc
{
  "schema_revision": "0002",
  "storage": {
    "backend": "s3", "bucket": "your-bucket-name",
    "input_prefix":  "app-data/genomic-sequencing/uploads/",
    "output_prefix": "app-data/genomic-sequencing/runs/",
    "reachable": true, "error": null      // full message, not just the type
  },
  "pipelines": [
    { "id": "ont-lsdv", "version": "1.1.0",
      "stages": ["fetch", "prepare", …, "publish"],
      "images": ["hkubal/clair3@sha256:57cf5d20…",
                 "quay.io/biocontainers/medaka@sha256:…",
                 "dsihartpark/dharini-ont-core@sha256:314f8675…"] }
  ],
  "limits": { "worker_concurrency": 1, "max_input_bytes": 5368709120,
              "local_retention_hours": 6, "require_pinned_images": true }
}
```

The prefixes are here because a prefix that disagrees with Dharini's
`S3_FOLDER` fails as *"object does not exist"* — indistinguishable from a
typo'd key, since the service cannot list the bucket. Printing what it is
actually looking under is what separates the two.

`images` is the **resolved digest** for every image each pipeline will run, so
"which build of Medaka produced this result" has an answer months later. That
is the question a variant call is worth nothing without.

> **There is deliberately no "is this image pulled" field.** Answering it means
> talking to the Docker daemon, and the API container does not mount the socket
> — only the worker does, because that socket is root-equivalent access to the
> host. Mounting it here to satisfy a status page would trade a real security
> boundary for a convenience. `make images-list` on the host answers it.
