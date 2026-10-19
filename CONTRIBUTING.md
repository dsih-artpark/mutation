# Contributing

Thanks for your interest. Bug reports, fixes, new pipelines and documentation
improvements are welcome.

## Getting started

Docker is the only requirement; everything runs in containers.

```bash
make test-fast     # unit tests, no Docker-in-Docker
make test          # the full suite, including container-backed tests
make help          # all targets
```

See the [readme](readme.md#local-development) for local development and
[Adding a pipeline](readme.md#adding-a-pipeline) for new pipelines.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make a focused change, with tests where behaviour changes.
3. Make sure `make test-fast` passes. CI runs the full suite.
4. Open a pull request describing what changed and why.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/),
for example `fix(images): stop the lock step filling the runner's disk`.

A maintainer reviews every pull request. The first run of CI on a pull request
from a new contributor needs a maintainer's approval.

## Sign your commits (DCO)

Every commit must carry a `Signed-off-by` line, which certifies that you wrote
the change, or have the right to submit it, under this project's
[Apache-2.0 licence](LICENSE) (the [Developer Certificate of Origin](https://developercertificate.org/)).
Add it with:

```bash
git commit -s
```

A pull request check fails if any commit is missing it. To fix existing commits:
`git rebase --signoff main` and force-push your branch.

## Tool images

Changing a tool image (`shared/` or `app/pipelines/*/docker/`) needs its digest
pinned in `images.lock.json`, and only maintainers can push images to Docker Hub.
Open the pull request as usual; a maintainer will build the image and add the lock.

## Reporting problems

Use the issue templates. For security problems, see [SECURITY.md](SECURITY.md)
and do not open a public issue.

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
