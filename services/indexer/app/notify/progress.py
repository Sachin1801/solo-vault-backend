import json

import redis

from app.config import settings

PROGRESS_PCT = {
    "validate": 10,
    "download": 25,
    "parse": 50,
    "chunk": 60,
    "embed": 80,
    "store": 90,
    "done": 100,
    "failed": 0,
}

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url)
    return _redis


def emit_progress(entry_id: str, step: str, pct: int | None = None, message: str = "") -> None:
    progress = PROGRESS_PCT.get(step, 0) if pct is None else pct
    payload = json.dumps(
        {
            "type": "index_progress",
            "entry_id": entry_id,
            "step": step,
            "progress_pct": progress,
            "status": "failed" if step == "failed" else ("indexed" if progress == 100 else "running"),
            "message": message,
        }
    )
    get_redis().publish(f"progress:{entry_id}", payload)
