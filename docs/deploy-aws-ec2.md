# Deploying on AWS EC2

AWS-specific setup. For installing, upgrading and rolling back, see
[DEPLOYMENT.md](DEPLOYMENT.md).

One instance runs everything: the API, worker, scheduler, Postgres, Redis and
the per-tool containers. Create the bucket and credentials first, then start the
service.

## 1. Instance

| | |
| --- | --- |
| Architecture | **x86_64.** Do not use Graviton or other ARM instances: the tool images are `linux/amd64`. |
| Size | At least 4 vCPU and 16 GiB RAM (for example `m6a.xlarge`). Avoid burstable `t*` types; pipelines keep the CPU busy for minutes. |
| OS | A current Ubuntu LTS release, or any Linux distribution Docker supports |
| Disk | gp3, 100 GB or more, **encrypted** (encryption cannot be added in place later) |
| Region | The same region as the bucket |

Tool images take about 9 GB, and a large run can need tens of GB of scratch.
Local run data is deleted after `RETENTION_HOURS`; S3 holds the durable copy.
See [Sizing](../readme.md#sizing) to tune from real run data.

## 2. S3 bucket

```text
<S3_BUCKET>/<S3_PREFIX>/uploads/…          the caller writes, this service reads
<S3_BUCKET>/<S3_PREFIX>/runs/<run_id>/…    this service writes, the caller reads
```

`S3_PREFIX` must equal the caller's folder exactly, otherwise uploads are looked
for in the wrong place. `GET /health` shows the effective prefixes.

```bash
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

Skip `create-bucket` if you are using an existing bucket.

## 3. Credentials

Use the minimal policy below, and keep it that narrow. Do not widen it to `s3:*`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::BUCKET/PREFIX/uploads/*" },
    { "Effect": "Allow", "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::BUCKET/PREFIX/runs/*" }
  ]
}
```

Then pick one:

**Access keys in `.env`.** Create an IAM user with only this policy (no console
access), and set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Set both or
neither. Rotate them when someone leaves.

**Instance role (no keys).** Attach the policy to a role, attach the role to the
instance, and leave both variables empty. Because the service runs in
containers, also raise the metadata hop limit, or credentials resolve on the
host but fail inside containers:

```bash
aws ec2 modify-instance-metadata-options --instance-id i-… \
  --http-tokens required --http-put-response-hop-limit 2
```

Check it:

```bash
curl -s localhost:8000/health | jq .storage     # "reachable": true
```

If every submission returns `422 … does not exist` for keys you can see in the
console, check the bucket name in the policy, that `S3_PREFIX` matches the
caller's folder, and that the policy covers the full prefix.

## 4. Security group

| Direction | Port | Source |
| --- | --- | --- |
| Inbound | 8000 | The caller's security group or IP only. Never `0.0.0.0/0`. |
| Inbound | 22 | Your IP only, or omit it and use SSM Session Manager |
| Outbound | 443 | S3, Docker Hub, and the callback host |

Set `API_BIND=0.0.0.0` in `.env` if the caller is on another host. Postgres and
Redis publish no host ports.

## 5. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo apt-get install -y jq curl
sudo usermod -aG docker $USER && newgrp docker
sudo systemctl enable --now docker

sudo mkdir -p /srv/dharini/runs /srv/dharini/references
sudo chown -R $USER /srv/dharini
```

Services restart automatically with Docker, so no systemd unit is needed.

## 6. Deploy

Follow the [quick start](DEPLOYMENT.md#quick-start). In `.env` set the S3
variables from steps 2 and 3, and `DOCKER_GID`
(`getent group docker | cut -d: -f3`).

## 7. Verify

```bash
curl -s localhost:8000/health | jq
```

Expect `status: "ok"`, `storage.reachable: true` and a non-null
`schema_revision`. From the caller's host, `curl http://<instance>:8000/v1/pipelines`
should list the pipelines; if it times out there but works locally, it is the
security group.

## S3 versioning and lifecycle

Neither is enabled. Result keys contain a random `run_id`, so one run cannot
overwrite another. Enable versioning before pointing a second deployment at the
same bucket and prefix, or before writing keys derived from client input.

Nothing expires automatically, so add an S3 lifecycle rule for `uploads/` and
`runs/`. The service has no `s3:DeleteObject` permission on purpose.
