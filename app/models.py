"""Durable run records.

These exist so a completed run survives a Redis flush, a worker restart, and a
consumer that was offline when its webhook fired. Celery's result backend is a
cache, not a system of record.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StageStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    pipeline_id: Mapped[str] = mapped_column(String(64), index=True)
    pipeline_version: Mapped[str] = mapped_column(String(32))

    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False, length=16),
        default=RunStatus.QUEUED,
        index=True,
    )

    params: Mapped[dict] = mapped_column(JSON, default=dict)

    # Original client-supplied filename. The file on disk is always normalised
    # to the pipeline's expected name, so hostile filenames never reach a tool.
    input_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pipeline-declared summary numbers (variant counts, coverage, QC stats).
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)

    # True once artifacts have been swept off disk by retention.
    artifacts_purged: Mapped[bool] = mapped_column(default=False)

    stages: Mapped[list[RunStage]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunStage.ordinal"
    )
    artifacts: Mapped[list[RunArtifact]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def duration_s(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)


class RunStage(Base):
    """One executed stage. Also the sizing telemetry: peak_memory_mb and
    duration_s across N real runs are what tell you whether the box needs to
    grow, instead of guessing."""

    __tablename__ = "run_stages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)

    name: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)

    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, native_enum=False, length=16), default=StageStatus.PENDING
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    peak_memory_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="stages")

    __table_args__ = (Index("ix_run_stages_run_ordinal", "run_id", "ordinal"),)


class RunArtifact(Base):
    """A file a pipeline explicitly declared worth keeping. Anything not
    registered here lives in work/ and is deleted when the run ends."""

    __tablename__ = "run_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)

    kind: Mapped[str] = mapped_column(String(32))       # vcf | bam | consensus | qc | report
    label: Mapped[str] = mapped_column(String(128))
    filename: Mapped[str] = mapped_column(String(256))  # basename within output/
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")

    run: Mapped[Run] = relationship(back_populates="artifacts")


class WebhookDelivery(Base):
    """Delivery state, tracked separately from the run itself.

    A run that computed correctly but whose notification could not be delivered
    is a SUCCEEDED run with a FAILED delivery -- never a failed run. Conflating
    the two is how you end up re-running eight minutes of compute because a
    consumer was redeploying.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)

    url: Mapped[str] = mapped_column(Text)
    event: Mapped[str] = mapped_column(String(32), default="run.completed")

    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, native_enum=False, length=16),
        default=DeliveryStatus.PENDING,
        index=True,
    )

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6)

    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="deliveries")
