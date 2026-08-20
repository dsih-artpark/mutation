"""What can be said truthfully about a run in flight.

Deliberately no sub-stage percentage. Clair3, Medaka and minimap2 emit nothing
machine-readable, so "62% through variant calling" would be a number we made
up. Stage, elapsed, liveness and a historically-derived typical duration are
what the data actually supports (spec S18).

Pure query logic, no routing -- app/api/runs.py and app/api/queue.py are the
only callers, so this module can be exercised with nothing but a session and
some rows in run_stages.
"""

from __future__ import annotations

import time
from datetime import timedelta, timezone
from statistics import median

from .config import ETA_CACHE_TTL_S, ETA_HISTORY_RUNS, WORKER_CONCURRENCY
from .models import Run, RunStage, RunStatus, StageStatus, utcnow
from .pipelines import PipelineNotFound, registry

# {pipeline_id: (expires_at_monotonic, {stage_name: median_seconds})}. Module
# level and process-wide: medians move slowly and the query is a group-by
# across two tables, so recomputing it on every poll would be wasted work.
_cache: dict[str, tuple[float, dict[str, float]]] = {}


def typical_durations(session, pipeline_id: str) -> dict[str, float]:
    """Median `duration_s` per stage name, over the most recent
    `ETA_HISTORY_RUNS` runs of this pipeline (S19).

    Median, not mean: one pathological forty-minute alignment must not move
    the estimate for everyone the way an outlier moves an average.
    """
    now = time.monotonic()
    cached = _cache.get(pipeline_id)
    if cached and cached[0] > now:
        return cached[1]

    recent_run_ids = (
        session.query(Run.id)
        .filter(Run.pipeline_id == pipeline_id)
        .order_by(Run.created_at.desc())
        .limit(ETA_HISTORY_RUNS)
        .subquery()
    )
    rows = (
        session.query(RunStage.name, RunStage.duration_s)
        .filter(
            RunStage.run_id.in_(session.query(recent_run_ids.c.id)),
            RunStage.status == StageStatus.SUCCEEDED,
            RunStage.duration_s.isnot(None),
        )
        .all()
    )

    by_stage: dict[str, list[float]] = {}
    for name, duration in rows:
        by_stage.setdefault(name, []).append(duration)

    result = {name: median(values) for name, values in by_stage.items()}
    _cache[pipeline_id] = (now + ETA_CACHE_TTL_S, result)
    return result


def _current_stage(run: Run) -> RunStage | None:
    return next((s for s in run.stages if s.status == StageStatus.RUNNING), None)


def _elapsed_s(since) -> float:
    """Seconds between `since` and now.

    SQLite (the test suite) drops tzinfo on a DateTime(timezone=True) column
    round-trip; Postgres (production) does not. Normalising here is what
    keeps this arithmetic correct on both without conditionals at every call
    site.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return (utcnow() - since).total_seconds()


def _eta_seconds(
    session, pipeline, stage: RunStage
) -> tuple[float | None, float | None]:
    """(stage_typical_s, eta_s) for the stage currently running.

    Both are `None` the moment any needed typical is missing -- the current
    stage's own history, or that of any stage still ahead. A first run of a
    new pipeline reports elapsed time and nothing else (S19); inventing a
    number would be worse than admitting ignorance.
    """
    typicals = typical_durations(session, pipeline.id)
    current_typical = typicals.get(stage.name)
    if current_typical is None:
        return None, None

    effective = list(pipeline.effective_stages)
    try:
        remaining = effective[effective.index(stage.name) + 1 :]
    except ValueError:
        remaining = []

    later_typicals = [typicals.get(name) for name in remaining]
    if any(t is None for t in later_typicals):
        return current_typical, None

    elapsed = _elapsed_s(stage.started_at) if stage.started_at else 0.0
    eta = max(0.0, current_typical - elapsed) + sum(later_typicals)
    return current_typical, eta


def _eta_for_run(session, run: Run) -> float | None:
    """`eta_s` for one running run, or `None` when its pipeline is unknown,
    nothing is running, or history is missing. Shared by the progress
    endpoint and the queue's `estimated_wait_s`."""
    try:
        pipeline = registry.get(run.pipeline_id)
    except PipelineNotFound:
        return None
    stage = _current_stage(run)
    if stage is None:
        return None
    _, eta = _eta_seconds(session, pipeline, stage)
    return eta


def _pipeline_total_typical(session, pipeline_id: str) -> float | None:
    """Sum of every effective stage's typical duration -- the full cost of
    running this pipeline once, used to size a queued run's wait."""
    try:
        pipeline = registry.get(pipeline_id)
    except PipelineNotFound:
        return None
    typicals = typical_durations(session, pipeline_id)
    values = [typicals.get(name) for name in pipeline.effective_stages]
    if any(v is None for v in values):
        return None
    return sum(values)


def _estimated_wait_s(session, ahead_of: object = None) -> int | None:
    """Remaining ETA of every currently RUNNING run, plus the full typical
    duration of every QUEUED run ahead, divided across the workers.

    `ahead_of`: `None` counts everyone currently running or queued (the wait
    for a run submitted right now, spec S14). A `created_at` value scopes the
    queued half to runs queued strictly before it -- what a QUEUED run itself
    is waiting behind.
    """
    if WORKER_CONCURRENCY <= 0:
        return None

    running_runs = session.query(Run).filter(Run.status == RunStatus.RUNNING).all()
    queued_query = session.query(Run).filter(Run.status == RunStatus.QUEUED)
    if ahead_of is not None:
        queued_query = queued_query.filter(Run.created_at < ahead_of)
    queued_runs = queued_query.all()

    total = 0.0
    for run in running_runs:
        eta = _eta_for_run(session, run)
        if eta is None:
            return None
        total += eta
    for run in queued_runs:
        typical_total = _pipeline_total_typical(session, run.pipeline_id)
        if typical_total is None:
            return None
        total += typical_total

    return int(round(total / WORKER_CONCURRENCY))


def build_progress(session, run: Run, pipeline) -> dict:
    """Exactly the shape spec §13.1 promises a 3-5 second poller. Small and
    fixed-size on purpose -- GET /v1/runs/{id} drags params, artifacts and
    deliveries with it and is the wrong shape to hit that often.
    """
    effective = list(pipeline.effective_stages)
    stage = _current_stage(run) if run.status == RunStatus.RUNNING else None

    stage_index = stage_started_at = stage_elapsed_s = None
    stage_typical_s = heartbeat_age_s = eta_s = None

    if stage is not None:
        stage_index = effective.index(stage.name)
        stage_started_at = stage.started_at
        if stage.started_at:
            stage_elapsed_s = round(_elapsed_s(stage.started_at), 1)
        if stage.last_heartbeat_at:
            heartbeat_age_s = round(_elapsed_s(stage.last_heartbeat_at), 1)
        stage_typical_s, eta_s = _eta_seconds(session, pipeline, stage)

    eta_at = utcnow() + timedelta(seconds=eta_s) if eta_s is not None else None

    queue_position = estimated_start_s = None
    if run.status == RunStatus.QUEUED:
        queue_position = (
            session.query(Run)
            .filter(Run.status == RunStatus.QUEUED, Run.created_at < run.created_at)
            .count()
        )
        estimated_start_s = _estimated_wait_s(session, ahead_of=run.created_at)

    by_name = {s.name: s for s in run.stages}
    stages_out = [
        {
            "name": name,
            "status": (by_name[name].status.value if name in by_name else StageStatus.PENDING.value),
            "duration_s": (
                round(by_name[name].duration_s, 1)
                if name in by_name and by_name[name].duration_s is not None
                else None
            ),
        }
        for name in effective
    ]

    return {
        "run_id": run.id,
        "external_id": run.external_id,
        "status": run.status.value,
        "stage": stage.name if stage else None,
        "stage_index": stage_index,
        "stage_total": len(effective),
        "stage_started_at": stage_started_at,
        "stage_elapsed_s": stage_elapsed_s,
        "stage_typical_s": round(stage_typical_s, 1) if stage_typical_s is not None else None,
        "heartbeat_age_s": heartbeat_age_s,
        "eta_s": int(round(eta_s)) if eta_s is not None else None,
        "eta_at": eta_at,
        "queue_position": queue_position,
        "estimated_start_s": estimated_start_s,
        "stages": stages_out,
    }


def queue_snapshot(session, include_running: bool) -> dict:
    """`{worker_concurrency, queued, estimated_wait_s}`, plus `running` only
    for a service key -- another user's run ids are sensitive, queue depth
    and wait are not (§14)."""
    running_runs = session.query(Run).filter(Run.status == RunStatus.RUNNING).all()
    queued_count = session.query(Run).filter(Run.status == RunStatus.QUEUED).count()

    snapshot: dict = {
        "worker_concurrency": WORKER_CONCURRENCY,
        "queued": queued_count,
        "estimated_wait_s": _estimated_wait_s(session),
    }

    if include_running:
        running_out = []
        for run in running_runs:
            stage = _current_stage(run)
            eta = _eta_for_run(session, run)
            running_out.append({
                "run_id": run.id,
                "pipeline_id": run.pipeline_id,
                "stage": stage.name if stage else None,
                "eta_s": int(round(eta)) if eta is not None else None,
            })
        snapshot["running"] = running_out

    return snapshot
