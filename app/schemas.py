"""API response shapes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
    pipeline_id: str
    pipeline_version: str
    status: str
    stages: list[str]
    queue_position: int | None = None


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
