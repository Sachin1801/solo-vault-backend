import threading
import time

from app.config import settings
from app.notify.progress import get_redis
from app.s3.client import download_file


class LocalTokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated_at = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: int = 1) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.updated_at
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.updated_at = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                missing = tokens - self.tokens
                wait_for = max(0.01, missing / self.rate)
            time.sleep(wait_for)


class RedisGlobalTokenBucket:
    SCRIPT = """
    local key = KEYS[1]
    local rate = tonumber(ARGV[1])
    local capacity = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local requested = tonumber(ARGV[4])

    local data = redis.call('HMGET', key, 'tokens', 'ts')
    local tokens = tonumber(data[1])
    local ts = tonumber(data[2])

    if tokens == nil then tokens = capacity end
    if ts == nil then ts = now end

    local elapsed = math.max(0, now - ts)
    tokens = math.min(capacity, tokens + elapsed * rate)

    if tokens >= requested then
      tokens = tokens - requested
      redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
      redis.call('EXPIRE', key, 120)
      return 1
    else
      redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
      redis.call('EXPIRE', key, 120)
      return 0
    end
    """

    def __init__(self, redis_client, rate: float, capacity: float):
        self.redis = redis_client
        self.rate = rate
        self.capacity = capacity
        self.key = "ratelimit:s3:global"
        self._script = self.redis.register_script(self.SCRIPT)

    def acquire(self, tokens: int = 1) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            granted = self._script(
                keys=[self.key],
                args=[self.rate, self.capacity, time.time(), tokens],
            )
            if int(granted) == 1:
                return
            time.sleep(0.05)
        raise TimeoutError("Timed out acquiring global S3 rate limit token")


class RateLimitedS3:
    def __init__(self):
        self._local = LocalTokenBucket(rate=settings.s3_rate_limit_rps, capacity=10)
        self._global = RedisGlobalTokenBucket(
            get_redis(),
            rate=settings.s3_rate_limit_rps,
            capacity=50,
        )

    def download(self, bucket: str, key: str, local_path: str) -> None:
        self._local.acquire()
        self._global.acquire()
        download_file(bucket, key, local_path)


rate_limited_s3 = RateLimitedS3()
