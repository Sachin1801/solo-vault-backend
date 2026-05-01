import json

import redis

from app.config import settings

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def is_file_indexed(fhash: str) -> bool:
    return bool(_get_redis().exists(f"cache:file:{fhash}"))


def mark_file_indexed(fhash: str, entry_id: str, ttl_seconds: int = 86400 * 7) -> None:
    _get_redis().set(f"cache:file:{fhash}", entry_id, ex=ttl_seconds)


def get_cached_embedding(chash: str) -> list[float] | None:
    value = _get_redis().get(f"cache:chunk:{chash}")
    if value is None:
        return None
    return json.loads(value)


def cache_embedding(chash: str, embedding: list[float], ttl_seconds: int = 86400 * 30) -> None:
    _get_redis().set(f"cache:chunk:{chash}", json.dumps(embedding), ex=ttl_seconds)
