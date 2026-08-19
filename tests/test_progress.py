"""Progress reporting: what can be said truthfully about a running job.

Uses a synthetic pipeline (like test_api.py's test-echo) so ETA and stage math
are exercised without Docker: "qc", "align", "prepare", "call", "variant",
"annotate", "report" declared -> effective stages are
fetch, qc, align, prepare, call, variant, annotate, report, publish (9 total),
with "variant" at index 5. A second pipeline id carries seeded history so the
no-history and with-history cases never share a cache entry.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import jwt
import pytest

from app.db import session_scope
from app.models import Run, RunStage, RunStatus, StageStatus, utcnow
from app.pipelines import registry
from app.pipelines.base import Pipeline

SECRET = "test-jwt-secret"
ISS = "dharini-test"
AUD = "pipeline-test"

PIPELINE_ID = "test-progress"
HIST_PIPELINE_ID = "test-progress-hist"


class _ProgressPipeline(Pipeline):
    id = PIPELINE_ID
    version = "0.0.1"
    display_name = "Test Progress"
    description = "Synthetic pipeline for progress/ETA tests."
    accepted_extensions = (".fasta",)
    input_filename = "assembly.fasta"
    stages = ["qc", "align", "prepare", "call", "variant", "annotate", "report"]
    params_schema = {"type": "object", "additionalProperties": False, "properties": {}}

    def run(self, ctx):  # pragma: no cover - never actually executed
        raise NotImplementedError


class _ProgressHistPipeline(_ProgressPipeline):
    id = HIST_PIPELINE_ID


for _pipeline in (_ProgressPipeline(), _ProgressHistPipeline()):
    if _pipeline.id not in registry:
        registry.register(_pipeline)


# ============================================================== fixtures

@pytest.fixture()
def session():
    with session_scope() as db_session:
        yield db_session


@pytest.fixture()
def freeze_time(monkeypatch):
    """Advance the clock `app.progress` sees, without touching the database --
    what distinguishes a live sampler from one that stopped writing."""
    import app.progress as progress_module

    state = {"offset": 0.0}

    def _fake_utcnow():
        return utcnow() + timedelta(seconds=state["offset"])

    monkeypatch.setattr(progress_module, "utcnow", _fake_utcnow)

    def _advance(seconds: float) -> None:
        state["offset"] += seconds

    return _advance


@pytest.fixture()
def running_run():
    """Mid-`variant`, on a pipeline id with no SUCCEEDED history anywhere in
    the suite -- the ETA must come back null (S19)."""
    with session_scope() as db_session:
        run = Run(
            pipeline_id=PIPELINE_ID, pipeline_version="0.0.1",
            external_id=str(uuid.uuid4()), status=RunStatus.RUNNING, params={},
            started_at=utcnow(),
        )
        db_session.add(run)
        db_session.flush()
        run_id = run.id
        db_session.add(RunStage(
            run_id=run_id, name="variant", ordinal=5,
            status=StageStatus.RUNNING, started_at=utcnow(),
            last_heartbeat_at=utcnow(),
        ))
    return run_id


@pytest.fixture()
def running_run_with_history():
    """Same shape, but on a pipeline id seeded with real history for every
    effective stage, so an ETA can actually be computed."""
    effective = ["fetch", *_ProgressHistPipeline.stages, "publish"]

    with session_scope() as db_session:
        for i in range(5):
            hist_run = Run(
                pipeline_id=HIST_PIPELINE_ID, pipeline_version="0.0.1",
                external_id=str(uuid.uuid4()), status=RunStatus.SUCCEEDED, params={},
            )
            db_session.add(hist_run)
            db_session.flush()
            for ordinal, name in enumerate(effective):
                db_session.add(RunStage(
                    run_id=hist_run.id, name=name, ordinal=ordinal,
                    status=StageStatus.SUCCEEDED, duration_s=60.0 + i,
                    started_at=utcnow(), finished_at=utcnow(),
                ))

        run = Run(
            pipeline_id=HIST_PIPELINE_ID, pipeline_version="0.0.1",
            external_id=str(uuid.uuid4()), status=RunStatus.RUNNING, params={},
            started_at=utcnow(),
        )
        db_session.add(run)
        db_session.flush()
        run_id = run.id
        db_session.add(RunStage(
            run_id=run_id, name="variant", ordinal=5,
            status=StageStatus.RUNNING, started_at=utcnow(),
            last_heartbeat_at=utcnow(),
        ))
    return run_id


@pytest.fixture()
def queued_run():
    """Two other runs queued strictly before this one. Cleaned up afterward
    so a later test's queue count is not inflated by rows nothing ever
    processes (direct inserts, not real submissions)."""
    created: list[str] = []

    def _make(offset_s: float) -> str:
        with session_scope() as db_session:
            run = Run(
                pipeline_id=PIPELINE_ID, pipeline_version="0.0.1",
                external_id=str(uuid.uuid4()), status=RunStatus.QUEUED, params={},
                created_at=utcnow() - timedelta(seconds=offset_s),
            )
            db_session.add(run)
            db_session.flush()
            created.append(run.id)
            return run.id

    _make(3)
    _make(2)
    target = _make(1)

    yield target

    with session_scope() as db_session:
        db_session.query(Run).filter(Run.id.in_(created)).delete(synchronize_session=False)


@pytest.fixture()
def client_with_runs():
    """One run each for two different owners, so ownership scoping on the
    progress endpoint can be exercised over real HTTP."""
    from fastapi.testclient import TestClient

    from app.main import app

    def _make(owner_id: str) -> str:
        with session_scope() as db_session:
            run = Run(
                pipeline_id=PIPELINE_ID, pipeline_version="0.0.1",
                external_id=str(uuid.uuid4()), status=RunStatus.RUNNING, params={},
                owner_id=owner_id,
            )
            db_session.add(run)
            db_session.flush()
            return run.id

    mine = _make("u17")
    theirs = _make("u99")
    return TestClient(app), mine, theirs


# ================================================================ helpers

def _history(session, pipeline_id, stage_name, durations):
    """Seed SUCCEEDED run_stages rows for the ETA median query. Each duration
    gets its own run, mirroring how real history accumulates one run at a
    time."""
    for value in durations:
        run = Run(
            pipeline_id=pipeline_id, pipeline_version="0.0.1",
            external_id=str(uuid.uuid4()), status=RunStatus.SUCCEEDED, params={},
        )
        session.add(run)
        session.flush()
        session.add(RunStage(
            run_id=run.id, name=stage_name, ordinal=0,
            status=StageStatus.SUCCEEDED, duration_s=float(value),
            started_at=utcnow(), finished_at=utcnow(),
        ))
    session.flush()


def _progress(run_id: str) -> dict:
    """What the endpoint actually returns -- routed through ProgressOut so a
    datetime field is a JSON-safe string here exactly as it would be over
    HTTP."""
    from app.progress import build_progress
    from app.schemas import ProgressOut

    with session_scope() as db_session:
        run = db_session.get(Run, run_id)
        pipeline = registry.get(run.pipeline_id)
        payload = build_progress(db_session, run, pipeline)
    return ProgressOut(**payload).model_dump(mode="json")


def _bearer(sub: str) -> dict[str, str]:
    token = jwt.encode(
        {"sub": sub, "iss": ISS, "aud": AUD,
         "iat": int(time.time()), "exp": int(time.time()) + 300},
        SECRET, algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


# ================================================================== tests

def test_typical_duration_is_the_median_not_the_mean(session):
    """One pathological forty-minute alignment must not move everyone's ETA."""
    from app.progress import typical_durations

    _history(session, "p", "align", [100, 110, 120, 130, 3600])
    assert typical_durations(session, "p")["align"] == 120


def test_eta_is_null_when_there_is_no_history(running_run):
    """A first run of a new pipeline reports elapsed time and nothing else.
    Inventing a number would be worse than admitting ignorance."""
    payload = _progress(running_run)
    assert payload["stage_typical_s"] is None
    assert payload["eta_s"] is None


def test_eta_counts_the_remaining_stages_too(running_run_with_history):
    payload = _progress(running_run_with_history)
    assert payload["eta_s"] > 0
    assert payload["eta_at"] is not None


def test_progress_names_the_current_stage_and_its_position(running_run):
    payload = _progress(running_run)
    assert payload["stage"] == "variant"
    assert payload["stage_index"] == 5
    assert payload["stage_total"] == 9


def test_heartbeat_age_rises_when_the_sampler_stops(running_run, freeze_time):
    payload = _progress(running_run)
    assert payload["heartbeat_age_s"] < 2
    freeze_time(seconds=120)
    assert _progress(running_run)["heartbeat_age_s"] > 100


def test_a_queued_run_reports_its_position_not_a_stage(queued_run):
    payload = _progress(queued_run)
    assert payload["status"] == "QUEUED"
    assert payload["stage"] is None
    assert payload["queue_position"] == 2


def test_the_progress_payload_stays_small(running_run):
    """It is polled every few seconds; GET /v1/runs/{id} is the wrong shape."""
    import json
    assert len(json.dumps(_progress(running_run))) < 2048


def test_queue_depth_and_wait(client, queued_run):
    body = client.get("/v1/queue", headers={"X-API-Key": "test-key"}).json()
    assert body["queued"] >= 1
    assert body["worker_concurrency"] == 1
    assert "running" in body


def test_a_user_token_sees_depth_but_not_other_peoples_runs(client, queued_run):
    body = client.get("/v1/queue", headers=_bearer("u17")).json()
    assert "queued" in body
    assert "running" not in body


def test_a_user_may_not_read_someone_elses_progress(client_with_runs):
    client, _mine, theirs = client_with_runs
    assert client.get(f"/v1/runs/{theirs}/progress",
                      headers=_bearer("u17")).status_code == 404
