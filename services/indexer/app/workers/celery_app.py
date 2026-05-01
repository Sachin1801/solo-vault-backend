from celery import Celery

from app.config import settings

celery_app = Celery(
    "indexer",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.pipeline_task"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
)
