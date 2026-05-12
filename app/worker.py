from celery import Celery
from app.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND

celery = Celery(
    "worker",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=["app.tasks"]
)

# Optional: Configuration for periodic tasks
celery.conf.beat_schedule = {
    'cleanup-old-runs-every-hour': {
        'task': 'app.tasks.cleanup_old_runs',
        'schedule': 3600.0,  # Run every hour
        'args': (12,)        # Delete runs older than 12 hours
    },
}

celery.conf.timezone = 'UTC'
