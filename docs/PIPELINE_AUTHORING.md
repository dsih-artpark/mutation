# Writing a Pipeline

How to add a new analysis pipeline to the Dharini pipeline service.

The goal of this contract is that **adding a pipeline is a backend-only
change**. You add **one directory** under `app/pipelines/` and edit no existing
file. You do not touch the API, the database, the worker, the queue, or the
Dharini frontend — `app/pipelines/__init__.py` discovers your package at
startup, it registers itself, and Dharini renders its parameter form from the
JSON Schema you declare.

---

## 1. The contract

Every pipeline declares three things and implements one method.

```python
class Pipeline:
    id: str                 # "ont-lsdv" — stable, used in the API
    version: str            # "1.0.0"   — recorded on every run
    display_name: str
    description: str

    accepted_extensions: tuple[str, ...]
    input_filename: str     # uploads are normalised to this name

    params_schema: dict     # JSON Schema
    stages: list[str]       # declared up front, drives progress reporting

    def run(self, ctx: RunContext) -> RunResult: ...
```

That's it. `app/pipelines/base.py` is the authoritative version.

### Why each declaration exists

| Declaration | What the platform does with it |
|---|---|
| `params_schema` | Validates submissions in milliseconds (a bad parameter is a `422`, not a job that dies eight minutes in). Dharini renders its form from it. Defaults are applied automatically. |
| `stages` | Progress reads `4/8 · consensus` from the moment the run is queued, instead of a binary PENDING/SUCCESS. |
| `accepted_extensions` | Rejects a FASTA sent to a FASTQ pipeline before anything is written to disk. |
| `input_filename` | The upload is stored under this name. Client filenames contain spaces, unicode and shell metacharacters, and must never reach a tool command line. |
| `version` | Lets a result from six months ago be traced to the code that produced it. Bump it whenever behaviour changes. |

---

## 2. The four rules

Inside `run()` you never write:

| ❌ Never | ✅ Instead |
|---|---|
| `subprocess.run(...)` | `ctx.run(spec, argv)` |
| `docker run ...` | a `StageSpec(image=...)` |
| a relative path like `"output/x.bam"` | `ctx.work_dir / "x.bam"` |
| `sys.exit(1)` | raise, or let `ctx.run` raise for you |

These aren't style preferences. Each one is a bug the platform has already had:

- **`subprocess` with `shell=True`** interpolating a filename is remote code
  execution, because the filename comes from an upload.
- **`docker run` built by hand** breaks the moment the caller is itself inside a
  container, because the Docker daemon resolves bind mounts against the *host*.
- **Relative paths** mean two concurrent runs write to the same file and corrupt
  each other.
- **`sys.exit(1)`** inside a Celery worker kills the worker process, not the
  task.

`RunContext` has no method that lets you do any of them.

---

## 3. What `ctx` gives you

```python
ctx.params                       # validated, defaults applied
ctx.param("variant.tool", "bcftools")   # nested lookup with fallback

ctx.input_path                   # the uploaded file (normalised name)
ctx.extra_input("primer_bed")    # optional secondary upload, or None
ctx.input_dir                    # uploads
ctx.work_dir                     # scratch — DELETED when the run ends
ctx.output_dir                   # published artifacts — retained
ctx.refs_dir                     # shared references, read-only

with ctx.stage("align"): ...     # timing + peak memory, recorded to Postgres
ctx.skip("primer", "no primer BED supplied")

ctx.run(spec, argv, env=..., stdout_path=...)   # execute a tool
ctx.require(path)                # assert a tool actually produced its output

ctx.artifact(path, kind=, label=)   # publish a file
ctx.metric("variant_count", 42)     # a number worth reporting
ctx.set_variants(rows)              # parsed rows for the webhook payload
ctx.log("...")
```

### `ctx.require()` matters more than it looks

Bioinformatics tools fail *softly*. They exit 0 having written nothing, or
having written an empty file. Checking the exit code alone is not a success
test. Assert on the artifact:

```python
ctx.run(specs.ALIGN, [...])
ctx.require(bam)          # ← without this, a soft failure propagates silently
```

---

## 4. Declaring resources

A `StageSpec` says what to run and what it may consume.

```python
from app.runtime.images import images_for
from app.runtime.spec import StageSpec

img = images_for(__package__)        # binds to this package's manifest.yaml

ALIGN = StageSpec(
    image=img("aligner"),            # a logical key, never an image string
    cpus=4,
    memory_gb=6,
    timeout_s=7200,
)
```

`image` is **required**. There is no local execution path: a stage that ran on
the worker host got neither the CPU cap nor the memory cap, so one mis-declared
stage could claim the whole box, and `StageSpec(image=None)` no longer exists.

`image` is also **never a literal string**. `img("aligner")` looks the key up in
your `manifest.yaml` (§5 below) and resolves it through the lock file, so
repinning an image is a manifest edit and touches no Python. A hardcoded
`"my-registry/aligner:1.2.0"` still *works*, which is exactly why it is
dangerous: it bypasses the manifest, the digest lock and `REQUIRE_PINNED_IMAGES`
silently, and the contract test cannot catch it — that test iterates the refs
that went through `img()`, and a hardcoded string was never there to be seen.

Both `cpus` and `memory_gb` become real `docker run` limits. They're clamped to
`MAX_STAGE_CPUS` / `MAX_STAGE_MEMORY_GB`, so a typo can't claim the whole box.

Reuse one base spec and adjust per call with `.with_()`:

```python
ctx.run(ALIGN.with_(memory_gb=12), argv)
```

**Estimate first, then measure.** Every stage's real peak memory and wall time
are recorded in the `run_stages` table. After ~20 real runs, replace your
estimates with measured values:

```sql
SELECT name,
       round(avg(duration_s))      AS avg_seconds,
       round(max(peak_memory_mb))  AS peak_mb
FROM run_stages
WHERE status = 'SUCCEEDED'
GROUP BY name
ORDER BY peak_mb DESC;
```

That query is also how you decide whether the server needs to grow.

---

## 5. Declaring images: `manifest.yaml`

**Every pipeline package needs a `manifest.yaml`.** It is not optional and not
a later step: `images_for(__package__)` reads it to find your scope, and raises
`ImageResolutionError` at import time if it is absent — which means the package
fails to import, and `app/pipelines/__init__.py` refuses to start rather than
letting a pipeline silently vanish from the registry.

A pipeline is one directory, and the manifest is what makes that true. The
images live next to the code that runs them, so nothing outside the directory
has to know your pipeline exists:

```text
app/pipelines/my_pipeline/
├── __init__.py        # from .pipeline import MyPipeline
├── manifest.yaml      # ← what this pipeline needs from the outside world
├── specs.py           # StageSpecs, images resolved via img()
├── pipeline.py        # the Pipeline subclass
└── docker/
    └── tool/Dockerfile
```

```yaml
schema_version: 1
scope: my-pipeline          # MUST equal Pipeline.id — the contract test checks

images:
  # source: build — you keep the Dockerfile, CI builds and pushes it.
  tool:
    source: build
    dockerfile: docker/tool/Dockerfile    # relative to this manifest
    context: docker/tool
    repository: artpark/dharini-my-tool
    version: "1.0.0"                      # never "latest"; parsing rejects it
    platform: linux/amd64

  # source: registry — an upstream image you only pull.
  aligner:
    source: registry
    repository: quay.io/biocontainers/minimap2
    version: "2.28--he4a0461_0"

assets:                     # optional: reference data fetched at deploy time
  reference:
    url: "https://…"
    filename: my_reference.fasta
    sha256: ""              # "" means not yet pinned; fill it in
```

### Keys, scopes and the `shared:` prefix

`img("tool")` resolves inside your own scope. To use something declared in
`shared/manifest.yaml` — the snpEff and Snippy images, the LSDV reference —
prefix it:

```python
SNPEFF = StageSpec(image=img("shared:snpeff-lsdv"), cpus=2, memory_gb=4)
```

There is no implicit fallback between the two. A bare key that is not in your
manifest is an error, not a silent lookup somewhere else, so where an image
comes from is always visible at the call site.

**Put an image in `shared/` when two pipelines must agree on it**, not merely
when two pipelines happen to use it. One snpEff image means the same variant is
annotated the same way whichever pipeline produced it; that is the reason, and
it is stronger than saving a download.

### How pinning actually happens

You declare `repository` and `version`; you never write a digest. Then:

1. `make images-build` builds every `source: build` image from your Dockerfile.
2. `make images-push` publishes it.
3. `make images-lock` resolves **every** image — built and upstream alike — to a
   digest and writes `images.lock.json`.
4. `app/runtime/images.py` prefers the locked digest over the manifest tag, so
   the same declaration yields a pinned ref in production and a convenient tag
   locally.
5. `REQUIRE_PINNED_IMAGES=true` makes an unpinned ref a startup failure, and
   `.github/workflows/images.yml` regenerates the lock on every manifest or
   Dockerfile change.

Your part is steps 0 and 1: declare the image and keep its Dockerfile in this
repository. An image nobody can rebuild is a liability regardless of how well
it is pinned.

---

## 6. Running tools

### The normal case: an argv list

```python
ctx.run(specs.SAMTOOLS, ["samtools", "index", str(bam)])
```

A list, never a string. No shell is involved, so a filename containing `;` or
`$(...)` is just a filename.

### Capturing stdout to a file

Use `stdout_path` rather than a shell redirect:

```python
ctx.run(specs.SNPEFF, ["snpEff", "LSDV", str(vcf)], stdout_path=annotated)
```

### When you genuinely need a pipe

Piping is idiomatic and unavoidable in this domain — materialising an
intermediate SAM would be absurd. So it's allowed, under one rule:

> **The script text must be a compile-time constant. Every value arrives
> through a quoted environment variable.**

```python
from app.runtime.runner import sh

ctx.run(
    specs.ALIGN,
    sh('minimap2 -ax map-ont -t "$T" "$REF" "$FQ" | samtools sort -m "$M" -o "$BAM"'),
    env={"T": "4", "REF": str(ref), "FQ": str(fastq),
         "M": "512M", "BAM": str(bam)},
)
```

Quoted expansion of a shell variable is **not re-parsed** for metacharacters, so
this is safe. An f-string here would not be:

```python
# ❌ NEVER. If the filename is `x.fq; rm -rf /`, this executes it.
sh(f'minimap2 {ref} {fastq} | samtools sort -o {bam}')
```

---

## 7. Publishing results

`ctx.work_dir` is **deleted** when the run ends. Only what you publish survives.
That's deliberate: a FASTQ run generates several GB of BAMs and intermediates,
and keeping them all would fill the disk in a week.

```python
ctx.artifact(annotated, kind="vcf", label="Annotated variants")
```

`kind` is one of `vcf`, `bam`, `alignment`, `consensus`, `qc`, `report`. Each
artifact becomes a download URL in the webhook payload and on
`GET /v1/runs/{id}`.

Metrics belong in the run record, not only in a log:

```python
ctx.metric("reads_mapped_percent", 94.2)
ctx.metric("consensus_completeness_percent", 98.1)
```

And parsed rows go in the webhook payload so the consumer gets queryable data:

```python
from app.vcf import parse_annotated_vcf, summarise

rows = parse_annotated_vcf(annotated)
ctx.set_variants(rows)
for key, value in summarise(rows).items():
    ctx.metric(key, value)
```

---

## 8. Writing `params_schema`

This is the pipeline's public API. Dharini builds its form from it, so
`title` and `description` are user-facing text, not comments.

```python
params_schema = {
    "type": "object",
    "additionalProperties": False,     # ← always. Catches typo'd keys.
    "properties": {
        "threads": {
            "type": "integer", "minimum": 1, "maximum": 16, "default": 4,
            "title": "Threads",
            "description": "Clamped per stage to that stage's CPU allocation.",
        },
        "variant": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "tool": {
                    "type": "string",
                    "enum": ["bcftools", "medaka", "clair3"],
                    "default": "clair3",
                    "description": "Clair3 and Medaka run neural-network "
                                   "inference on CPU and are much slower.",
                },
            },
        },
    },
}
```

Rules that matter:

- **`additionalProperties: False` everywhere.** Without it, `{"treads": 8}` is
  silently accepted and ignored.
- **`enum` for tool choices.** Dharini renders a dropdown, and an unknown tool
  is rejected at submission rather than at stage 4.
- **Every optional field gets a `default`.** Defaults are applied before your
  code runs, so `ctx.param("threads")` is always populated.
- **Never put a file path from the client in a plain string field** without
  validating it stays inside an allowed directory. See `prepare_reference()` in
  `app/pipelines/ont_lsdv/common.py`.

---

## 9. Worked example

Four files in one new directory, and nothing outside it changes.

`app/pipelines/my_pipeline/manifest.yaml`:

```yaml
schema_version: 1
scope: my-pipeline

images:
  tool:
    source: registry
    repository: quay.io/biocontainers/my-tool
    version: "1.0.0--hdfd78af_0"
```

`app/pipelines/my_pipeline/specs.py`:

```python
from ...runtime.images import images_for
from ...runtime.spec import StageSpec

img = images_for(__package__)          # reads the manifest.yaml beside this file

TOOL = StageSpec(image=img("tool"), cpus=2, memory_gb=4)
```

`app/pipelines/my_pipeline/pipeline.py`:

```python
from ...runtime.context import RunContext, RunResult
from ..base import Pipeline
from ..registry import registry
from . import specs


class MyPipeline(Pipeline):
    id = "my-pipeline"
    version = "1.0.0"
    display_name = "My Analysis"
    description = "What it does, in one sentence."

    accepted_extensions = (".fastq", ".fastq.gz")
    input_filename = "reads.fastq.gz"

    stages = ["prepare", "analyse"]

    params_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "threshold": {
                "type": "number", "minimum": 0, "maximum": 1, "default": 0.5,
                "title": "Detection threshold",
                "description": "Calls below this score are discarded.",
            },
        },
    }

    def run(self, ctx: RunContext) -> RunResult:
        with ctx.stage("prepare"):
            ctx.require(ctx.input_path, hint="uploaded FASTQ is missing")

        with ctx.stage("analyse"):
            output = ctx.work_dir / "results.tsv"
            ctx.run(specs.TOOL, [
                "my-tool",
                "--input", str(ctx.input_path),
                "--threshold", str(ctx.param("threshold", 0.5)),
                "--output", str(output),
            ])
            ctx.require(output)

        ctx.artifact(output, kind="report", label="Analysis results")
        ctx.metric("rows", sum(1 for _ in output.open()) - 1)
        return ctx.result


registry.register(MyPipeline())
```

`app/pipelines/my_pipeline/__init__.py`:

```python
from .pipeline import MyPipeline

__all__ = ["MyPipeline"]
```

**No existing file changes.** `app/pipelines/__init__.py` walks its own package
directory with `pkgutil` at startup and imports every subpackage, so importing
`app.pipelines.my_pipeline` runs `registry.register(...)` above. An import
failure is re-raised naming your package rather than swallowed — a pipeline that
quietly disappears from the registry is far harder to debug than one that fails
loudly at startup.

**That is the entire integration.** `GET /v1/pipelines` now lists it,
`POST /v1/runs` accepts it, progress and webhooks work, and Dharini can render
its form.

---

## 10. Testing before you deploy

```bash
# 1. Does it register and is the schema valid?
python -c "from app.pipelines import registry; print([p.id for p in registry.all()])"

# 2. Do defaults and validation behave?
python -c "
from app.pipelines import registry
print(registry.validate_params('my-pipeline', {}))
"

# 3. Does the contract kit pass? It covers your pipeline the moment it is
#    registered -- schema shape, stage names, manifest, image declarations.
pytest tests/test_all_pipelines.py -q

# 4. Pre-pull the tool images so the first real run isn't a 10-minute download
make images-pull      # needs images.lock.json; see readme "Known gaps"

# 5. Run it end to end
curl -X POST http://localhost:8000/v1/runs \
  -F pipeline_id=my-pipeline \
  -F file=@test.fastq.gz \
  -F 'params={"threshold": 0.7}'

# 6. Watch it
curl http://localhost:8000/v1/runs/<run_id> | jq '.progress, .stages'
```

If a stage fails, its full log is at
`$RUNS_ROOT/<run_id>/logs/NN_<stage>.log`, and the tail is in
`error_message` on the run record.

---

## 11. Checklist

Before opening a PR:

- [ ] `id` is stable and kebab-case; `version` is set
- [ ] `params_schema` has `additionalProperties: False` at every level
- [ ] Every optional parameter has a `default`
- [ ] `title` and `description` are written for a user, not a developer
- [ ] `stages` matches the `ctx.stage(...)` names exactly
- [ ] Every `ctx.run()` is followed by `ctx.require()` on its output
- [ ] No `subprocess`, no `docker`, no relative paths, no `sys.exit`
- [ ] Any `sh()` script is a literal constant with data passed via `env`
- [ ] Every `StageSpec` names its image with `img("key")`, never a literal
      string — a literal bypasses the manifest and the digest lock silently,
      and no test can see it
- [ ] `manifest.yaml` exists, its `scope` equals `Pipeline.id`, and every image
      declares an exact `version` (never `latest`). Pinning by digest is then
      the lock file's job, not yours: `make images-lock` resolves every
      declared image and `REQUIRE_PINNED_IMAGES=true` enforces it in production
- [ ] Every custom image has a Dockerfile in this repository
- [ ] Resource estimates are declared on every `StageSpec`
- [ ] Files worth keeping are published with `ctx.artifact()`; everything else
      is left in `work_dir` to be deleted
- [ ] `pytest tests/test_all_pipelines.py -q` passes — no new test file needed

---

## 12. Mistakes we've already made

Real defects found in the first ONT delivery. Worth reading once — they're the
reason several rules above exist.

| Mistake | Consequence |
|---|---|
| `docker run -v $(pwd):/pipeline` | `$(pwd)` is the *caller's* path. Inside a container the daemon can't find it, silently mounts an empty directory, and the tool "can't find" data that exists. |
| Hardcoded `output/alignment.bam` | Two concurrent runs overwrite each other. Concurrency was impossible by construction. |
| `sys.exit(1)` on tool failure | Kills the Celery worker rather than failing the task. |
| Report printing `COMPLETED` unconditionally | A report that says success regardless of what happened is worse than no report. |
| VCF-path selection copy-pasted into three modules | They drifted. Consensus silently broke for 3 of 4 variant callers. |
| Plain `.vcf` passed to `bcftools consensus` | It requires bgzip + index. Fails for Medaka, whose `medaka vcf` writes plain text. |
| `samtools sort -@ 8` with no `-m` | Defaults to ~768 MB *per thread* — ~6 GB reserved, OOM-killed in a capped container. |
| Clair3 without `--include_all_ctgs` | Defaults to human contig names, finds no match on a viral reference, exits 0 having called nothing. |
| `bcftools call` without `--ploidy 1` | Emits heterozygous genotypes for a haploid virus. |
| Medaka fed the raw FASTQ while alignment used the filtered one | Enabling NanoFilt silently gave two stages different input. |
| `:latest` image tags | Two runs a month apart quietly use different tool versions. Unreproducible. |

---

## 13. Getting the tool images right

Tool isolation by image is correct and is why this design works: Medaka, Clair3
and ARTIC align_trim have irreconcilable Python, PyTorch/TensorFlow and htslib
requirements, and no single conda environment can hold all three.

But an image you cannot rebuild is a liability:

1. **Keep a Dockerfile in this repository** for every custom image.
2. **Push to a registry the organisation controls**, not a personal account.
3. **Pin by digest**: `registry/img@sha256:abc123...`, not `:latest` or a
   mutable `:v4`.

For a surveillance platform whose outputs may inform animal-health decisions,
being able to say exactly which tool version produced a result is part of the
product, not an optimisation.
