# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately, either:

- through GitHub: the **Security** tab of this repository, then **Report a vulnerability**; or
- by email to dharini-support@artpark.in.

Include what you found, how to reproduce it, and the version (image tag) you tested.
We will acknowledge your report and keep you updated until it is resolved.

## Supported versions

Security fixes are made on the latest release. Please reproduce on the newest
`dsihartpark/dharini-mutation-pipeline` image before reporting.

## Scope

This policy covers the code in this repository. The bioinformatics tools it runs
(see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)) are separate projects; report
problems in them upstream.

When deploying, read the security checklist in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md):
the worker has access to the Docker socket, so run it on a dedicated host.
