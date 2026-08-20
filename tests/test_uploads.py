"""Artifact publication, and the rule that a computed run is never discarded.

The asymmetry these tests pin down: a fetch failure fails the run, because
nothing has been computed and a resubmission costs one transfer. An upload
failure does not, because twenty minutes of Clair3 is already spent.
"""

from __future__ import annotations

import uuid

import pytest

from app.db import session_scope
from app.models import Run, RunArtifact, RunStatus, utcnow
from app.runtime.context import RunPaths
from app.storage import StorageError, get_store


@pytest.fixture(autouse=True)
def no_eager_retry(monkeypatch):
    """Stop Celery's eager mode from running the whole retry chain inline.

    tests/conftest.py sets task_always_eager, so `apply_async` executes
    immediately -- a scheduled retry recurses until the budget is spent and one
    attempt looks like six. Recording the call instead is what lets these tests
    assert on a single attempt and on the backoff that was scheduled.
    """
    import app.tasks as tasks

    scheduled: list[tuple[str, dict]] = []
    for name in ("upload_run_artifacts", "deliver_webhook"):
        task = getattr(tasks, name)
        monkeypatch.setattr(
            task, "apply_async",
            lambda args=None, task_name=name, **kw: scheduled.append((task_name, kw)),
        )
    return scheduled


@pytest.fixture()
def failing_store(monkeypatch):
    """A store whose uploads always fail, leaving downloads working."""
    store = get_store()

    def _boom(path, key, content_type):
        raise StorageError(f"simulated outage writing {key}")

    monkeypatch.setattr(store, "upload", _boom)
    return store


@pytest.fixture()
def counting_store(monkeypatch):
    """Counts successful uploads so a retry's work can be measured."""
    store = get_store()
    original = store.upload
    calls: list[str] = []

    def _counted(path, key, content_type):
        calls.append(key)
        return original(path, key, content_type)

    monkeypatch.setattr(store, "upload", _counted)
    return calls


def _make_run(status=RunStatus.SUCCEEDED, artifacts=("annotated.vcf",),
              callback_url="https://dharini.example.org/hook", age_hours=24, **kwargs) -> str:
    """A terminal run with real files on disk, ready to publish.

    Aged by default: cleanup_expired_runs compares created_at against a cutoff,
    so a run created this instant is never old enough to be swept and a
    retention test would pass for the wrong reason.
    """
    from datetime import timedelta

    with session_scope() as session:
        run = Run(
            created_at=utcnow() - timedelta(hours=age_hours),
            pipeline_id="test-echo",
            pipeline_version="0.0.1",
            external_id=str(uuid.uuid4()),
            status=status,
            params={},
            callback_url=callback_url,
            finished_at=utcnow(),
            **kwargs,
        )
        session.add(run)
        session.flush()
        run_id = run.id

        paths = RunPaths.for_run(_runs_root(), run_id).create()
        for name in artifacts:
            (paths.output / name).write_text(f"contents of {name}\n")
            session.add(RunArtifact(
                run_id=run_id, kind="vcf", label=name, filename=name,
                size_bytes=20, content_type="text/plain",
            ))
    return run_id


def _runs_root():
    from app.config import RUNS_ROOT
    return RUNS_ROOT


def _read(run_id: str):
    with session_scope() as session:
        run = session.get(Run, run_id)
        return {
            "status": run.status,
            "uploaded": run.artifacts_uploaded,
            "attempts": run.upload_attempts,
            "error": run.upload_error,
            "next_attempt": run.next_upload_attempt_at,
            "bucket": run.output_bucket,
            "artifacts": [(a.filename, a.s3_key, a.uploaded_at) for a in run.artifacts],
            "deliveries": len(run.deliveries),
        }


# ------------------------------------------------------------------ success

def test_artifacts_land_under_the_run_prefix():
    from app.tasks import upload_run_artifacts

    run_id = _make_run()
    upload_run_artifacts(run_id)

    state = _read(run_id)
    assert state["uploaded"] is True
    for filename, key, uploaded_at in state["artifacts"]:
        assert key == f"runs/{run_id}/{filename}"
        assert uploaded_at is not None
        assert get_store().head(key) is not None


def test_a_run_json_manifest_is_written_beside_them():
    """Makes the output prefix self-describing, and is the recovery path if a
    delivery is abandoned and the webhook is lost for good."""
    import json

    from app.tasks import upload_run_artifacts

    run_id = _make_run()
    upload_run_artifacts(run_id)

    manifest = RunPaths.for_run(_runs_root(), run_id).root / "run.json"
    assert manifest.exists()
    payload = json.loads(manifest.read_text())
    assert payload["run"]["id"] == run_id
    assert payload["artifacts"][0]["key"] == f"runs/{run_id}/annotated.vcf"
    # A manifest is not a delivery attempt; recording one would put a phantom
    # row in the run's delivery history.
    assert "delivery_id" not in payload
    # The manifest is written only after every artifact landed. Reading the
    # flag off the not-yet-committed run row made every manifest in the bucket
    # claim false -- exactly backwards for the one document someone reads when
    # the webhook was lost for good.
    assert payload["artifacts_uploaded"] is True
    assert payload["artifacts_error"] is None
    assert get_store().head(f"runs/{run_id}/run.json") is not None


def test_publish_is_recorded_as_a_stage():
    """effective_stages counts publish, so something has to record it. Without
    this a run that finished perfectly reports 9/10 forever and a UI reads it
    as stuck."""
    from app.models import RunStage, StageStatus
    from app.tasks import upload_run_artifacts

    run_id = _make_run()
    upload_run_artifacts(run_id)

    with session_scope() as session:
        stages = session.query(RunStage).filter(
            RunStage.run_id == run_id, RunStage.name == "publish").all()
        assert len(stages) == 1
        assert stages[0].status == StageStatus.SUCCEEDED
        assert stages[0].duration_s is not None


def test_a_retrying_upload_does_not_pile_up_publish_rows(failing_store):
    """Upserted, not inserted -- six attempts must not leave six rows."""
    from app.config import S3_UPLOAD_MAX_ATTEMPTS
    from app.models import RunStage, StageStatus
    from app.tasks import upload_run_artifacts

    run_id = _make_run(upload_attempts=S3_UPLOAD_MAX_ATTEMPTS - 1)
    upload_run_artifacts(run_id)
    upload_run_artifacts(run_id)

    with session_scope() as session:
        stages = session.query(RunStage).filter(
            RunStage.run_id == run_id, RunStage.name == "publish").all()
        assert len(stages) == 1
        assert stages[0].status == StageStatus.FAILED


def test_the_webhook_fires_once_the_upload_succeeds():
    from app.tasks import upload_run_artifacts

    run_id = _make_run()
    upload_run_artifacts(run_id)
    assert _read(run_id)["deliveries"] == 1


# ------------------------------------------------------------------ failure

def test_upload_failure_leaves_the_run_succeeded(failing_store):
    """Twenty minutes of Clair3 is not discarded because storage blipped."""
    from app.tasks import upload_run_artifacts

    run_id = _make_run()
    upload_run_artifacts(run_id)

    state = _read(run_id)
    assert state["status"] == RunStatus.SUCCEEDED
    assert state["uploaded"] is False
    assert state["attempts"] == 1
    assert state["next_attempt"] is not None
    assert "simulated outage" in state["error"]


def test_the_first_retry_is_scheduled_at_the_first_backoff_step(failing_store, no_eager_retry):
    from app.config import S3_UPLOAD_BACKOFF_S
    from app.tasks import upload_run_artifacts

    upload_run_artifacts(_make_run())

    retries = [kw for name, kw in no_eager_retry if name == "upload_run_artifacts"]
    assert retries and retries[0]["countdown"] == S3_UPLOAD_BACKOFF_S[0]


def test_the_webhook_is_withheld_until_the_upload_succeeds(failing_store):
    """The payload carries object keys. Sending it early hands the consumer
    keys that 404, which is worse than a late webhook."""
    from app.tasks import upload_run_artifacts

    run_id = _make_run()
    upload_run_artifacts(run_id)
    assert _read(run_id)["deliveries"] == 0


def test_exhausting_the_retries_still_notifies(failing_store):
    """The run computed and the results are stranded. The consumer has to learn
    that, rather than waiting forever for a delivery that will never come."""
    from app.config import S3_UPLOAD_MAX_ATTEMPTS
    from app.tasks import upload_run_artifacts

    run_id = _make_run(upload_attempts=S3_UPLOAD_MAX_ATTEMPTS - 1)
    upload_run_artifacts(run_id)

    state = _read(run_id)
    assert state["status"] == RunStatus.SUCCEEDED
    assert state["uploaded"] is False
    assert state["next_attempt"] is None, "no further retry is scheduled"
    assert state["deliveries"] == 1


def test_a_retry_skips_artifacts_that_already_landed(counting_store):
    from app.tasks import upload_run_artifacts

    run_id = _make_run(artifacts=("a.vcf", "b.vcf", "c.vcf"))
    with session_scope() as session:
        run = session.get(Run, run_id)
        for artifact in run.artifacts:
            if artifact.filename != "c.vcf":
                artifact.s3_key = f"runs/{run_id}/{artifact.filename}"
                artifact.uploaded_at = utcnow()

    counting_store.clear()
    upload_run_artifacts(run_id)

    # c.vcf plus run.json. a.vcf and b.vcf were already stamped, and a 3 GB BAM
    # must not be re-sent because a 40 kB VCF failed.
    assert counting_store == [f"runs/{run_id}/c.vcf", f"runs/{run_id}/run.json"]


def test_a_failed_run_with_no_artifacts_still_fires_its_webhook():
    """Making the webhook conditional on having artifacts is how a failed run
    silently notifies nobody."""
    from app.tasks import upload_run_artifacts

    run_id = _make_run(status=RunStatus.FAILED, artifacts=())
    upload_run_artifacts(run_id)

    state = _read(run_id)
    assert state["uploaded"] is True
    assert state["deliveries"] == 1


# ---------------------------------------------------------------- retention

def test_retention_never_purges_a_run_whose_artifacts_are_not_in_storage():
    """Until the upload lands, these local files are the only copy."""
    from app.tasks import cleanup_expired_runs

    run_id = _make_run(callback_url=None, input_key="uploads/x/a.fasta")
    cleanup_expired_runs(hours=0)
    assert _read(run_id)["uploaded"] is False

    with session_scope() as session:
        assert session.get(Run, run_id).artifacts_purged is False


def test_retention_still_purges_a_run_that_predates_object_storage():
    """A run with no input_key was never going to upload. Exempting it would
    leave every historical run's files on the disk permanently -- its contract
    was the ordinary retention window, and it still is."""
    from app.tasks import cleanup_expired_runs

    run_id = _make_run(callback_url=None, input_key=None)
    assert _read(run_id)["uploaded"] is False

    cleanup_expired_runs(hours=0)
    with session_scope() as session:
        assert session.get(Run, run_id).artifacts_purged is True


def test_retention_purges_a_run_once_its_artifacts_are_uploaded():
    from app.tasks import cleanup_expired_runs, upload_run_artifacts

    run_id = _make_run(callback_url=None)
    upload_run_artifacts(run_id)
    cleanup_expired_runs(hours=0)

    with session_scope() as session:
        assert session.get(Run, run_id).artifacts_purged is True


# ------------------------------------------------------------------- sweeper

def test_the_sweeper_rearms_an_overdue_upload(failing_store):
    from app.tasks import sweep_pending_uploads, upload_run_artifacts

    run_id = _make_run()
    upload_run_artifacts(run_id)

    # Pretend the scheduled retry message was lost with the broker.
    with session_scope() as session:
        session.get(Run, run_id).next_upload_attempt_at = utcnow()

    assert sweep_pending_uploads()["requeued"] >= 1


# ------------------------------------------------------------------ endpoint

SERVICE = {"X-API-Key": "test-key"}


def _user_token(sub: str = "u17") -> dict:
    import time

    import jwt

    token = jwt.encode(
        {"sub": sub, "iss": "dharini-test", "aud": "pipeline-test",
         "exp": int(time.time()) + 300},
        "test-jwt-secret", algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_reupload_refuses_a_valid_user_token(client):
    """403, not 401: the credential is genuine, the power is not. Republishing
    is Dharini's server acting, not a browser reading."""
    run_id = _make_run()
    response = client.post(f"/v1/runs/{run_id}/reupload", headers=_user_token())
    assert response.status_code == 403


def test_reupload_refuses_no_credential(client):
    run_id = _make_run()
    assert client.post(f"/v1/runs/{run_id}/reupload").status_code == 401


def test_reupload_resets_the_attempt_budget(client, failing_store):
    """An operator asking for this has fixed the cause, and should get the full
    schedule again rather than one attempt against a spent budget."""
    from app.config import S3_UPLOAD_MAX_ATTEMPTS
    from app.tasks import upload_run_artifacts

    run_id = _make_run(upload_attempts=S3_UPLOAD_MAX_ATTEMPTS - 1)
    upload_run_artifacts(run_id)
    assert _read(run_id)["next_attempt"] is None, "precondition: budget spent"

    response = client.post(f"/v1/runs/{run_id}/reupload", headers=SERVICE)
    assert response.status_code == 202
    assert _read(run_id)["attempts"] == 0


def test_reupload_is_a_no_op_once_artifacts_are_uploaded(client):
    from app.tasks import upload_run_artifacts

    run_id = _make_run()
    upload_run_artifacts(run_id)

    response = client.post(f"/v1/runs/{run_id}/reupload", headers=SERVICE)
    assert response.json()["status"] == "already_uploaded"
