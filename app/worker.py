from celery import Celery

from app.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND, WORKER_CONCURRENCY

celery = Celery(
    "worker",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=["app.tasks"],
)

celery.conf.update(
    timezone="UTC",
    task_track_started=True,
    worker_concurrency=WORKER_CONCURRENCY,
    # Genomics stages are long and single-threaded per run; prefetching extra
    # messages just leaves them queued behind an eight-minute job on a worker
    # that cannot start them.
    worker_prefetch_multiplier=1,
    # Redis needs a visibility timeout longer than the slowest task, otherwise
    # it redelivers a message that is still being worked on and the run
    # executes twice.
    broker_transport_options={"visibility_timeout": 6 * 3600},
    result_expires=7 * 24 * 3600,
)

celery.conf.beat_schedule = {
    "cleanup-expired-runs": {
        "task": "app.tasks.cleanup_expired_runs",
        "schedule": 3600.0,
    },
    # Re-arms webhook retries whose scheduled Celery message was lost, e.g.
    # after a broker restart. Cheap: a single indexed query that usually
    # returns nothing.
    "sweep-pending-deliveries": {
        "task": "app.tasks.sweep_pending_deliveries",
        "schedule": 300.0,
    },
}
