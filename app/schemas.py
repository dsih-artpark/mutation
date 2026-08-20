"""API response shapes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RunRequest(BaseModel):
    """A submission. The file already exists in object storage.

    Streaming a gigabyte through this process a second time bought nothing but
    a timeout risk and a second copy on local disk, so the client uploads to
    storage first and sends the key.
    """

    model_config = ConfigDict(extra="forbid")

    pipeline_id: str = Field(description="Pipeline id, e.g. 'ont-lsdv'")

    external_id: str = Field(
        max_length=128,
        description=(
            "Unique per run, chosen by the caller BEFORE submitting. Carries a "
            "unique constraint: resubmitting one returns the original run "
            "instead of starting a second. It is also what lets a caller "
            "correlate the eventual webhook when the 202 was lost."
        ),
    )

    input_key: str = Field(description="Object key of the primary input")

    owner_id: str | None = Field(
        default=None, max_length=128,
        description=(
            "Which of the caller's users this run belongs to. A browser token "
            "may read only its own runs; a service key sees every run."
        ),
    )

    input_filename: str | None = Field(
        default=None, max_length=512,
        description=(
            "The user's original filename, kept as display metadata only. The "
            "file on disk is normalised to the pipeline's own name, so a "
            "hostile filename never reaches a tool command line."
        ),
    )

    extra_input_keys: dict[str, str] = Field(
        default_factory=dict,
        description="Object key per declared extra input, e.g. {'primer_bed': '...'}",
    )

    params: dict = Field(
        default_factory=dict,
        description="Validated against the pipeline's params_schema before queueing",
    )

    callback_url: str | None = Field(
        default=None, description="Webhook called when the run reaches a terminal state"
    )


class StageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    peak_memory_mb: float | None = None
    message: str | None = None


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    label: str
    filename: str
    size_bytes: int
    content_type: str
    url: str


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event: str
    status: str
    attempts: int
    max_attempts: int
    last_status_code: int | None = None
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    delivered_at: datetime | None = None


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    pipeline_id: str
    pipeline_version: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    input_filename: str | None = None


class RunDetail(RunSummary):
    params: dict = {}
    metrics: dict = {}
    error_stage: str | None = None
    error_message: str | None = None
    artifacts_purged: bool = False

    progress: str
    stages: list[StageOut] = []
    artifacts: list[ArtifactOut] = []
    deliveries: list[DeliveryOut] = []


class RunAccepted(BaseModel):
    run_id: str
    external_id: str
    pipeline_id: str
    pipeline_version: str
    status: str
    stages: list[str]
    queue_position: int | None = None
    estimated_start_s: float | None = None


class StageProgressOut(BaseModel):
    name: str
    status: str
    duration_s: float | None = None


class ProgressOut(BaseModel):
    """Small and fixed-size -- built for a 3-5 second poll, unlike RunDetail
    which drags params, artifacts and deliveries with it."""

    run_id: str
    external_id: str
    status: str
    stage: str | None = None
    stage_index: int | None = None
    stage_total: int
    stage_started_at: datetime | None = None
    stage_elapsed_s: float | None = None
    stage_typical_s: float | None = None
    heartbeat_age_s: float | None = None
    eta_s: int | None = None
    eta_at: datetime | None = None
    queue_position: int | None = None
    estimated_start_s: int | None = None
    stages: list[StageProgressOut] = []


class QueueRunningOut(BaseModel):
    run_id: str
    pipeline_id: str
    stage: str | None = None
    eta_s: int | None = None


class QueueOut(BaseModel):
    worker_concurrency: int
    queued: int
    estimated_wait_s: int | None = None
    # Omitted entirely (not just null) for a Bearer token -- another user's
    # run ids are sensitive, queue depth and wait are not (spec §14).
    running: list[QueueRunningOut] | None = None


class PipelineOut(BaseModel):
    id: str
    version: str
    display_name: str
    description: str
    accepted_extensions: list[str]
    stages: list[str]
    params_schema: dict
    extra_inputs: dict = {}
