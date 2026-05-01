import json

from redis import Redis

from app.config import settings


def emit_progress(entry_id: str, step: str, pct: int, message: str = "") -> None:
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    payload = {
        "entry_id": entry_id,
        "step": step,
        "progress_pct": pct,
        "message": message,
    }
    redis_client.publish(f"progress:{entry_id}", json.dumps(payload))
