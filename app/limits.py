"""Per-client request rate limiting.

Sliding window: the timestamps of recent requests are held in a sorted set and
anything older than the window is discarded on each call. Unlike a fixed
window it cannot be tricked by straddling a boundary.

Redis runs the script below single-threaded, so trimming, counting and adding
happen as one indivisible step. Doing those as separate round trips is what
lets concurrent requests all observe "under the limit" and all get through.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Optional

import redis

from app.config import settings

_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local used = redis.call('ZCARD', key)

if used < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, window)
  return {1, limit - used - 1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return {0, 0, window - (now - tonumber(oldest[2]))}
"""


@dataclass
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


_client: Optional[redis.Redis] = None
_script = None


def _script_handle():
    global _client, _script
    if _script is None:
        _client = redis.Redis.from_url(settings.redis_url)
        _script = _client.register_script(_SLIDING_WINDOW)
    return _script


def use_client(client: redis.Redis) -> None:
    """Swap the Redis connection, so tests can run against a fake."""
    global _client, _script
    _client = client
    _script = client.register_script(_SLIDING_WINDOW)


def check(client_id: str, limit_per_minute: int) -> RateDecision:
    window_ms = 60_000
    now_ms = int(time.time() * 1000)

    allowed, remaining, retry_ms = _script_handle()(
        keys=[f"ratelimit:{client_id}"],
        args=[now_ms, window_ms, limit_per_minute, uuid.uuid4().hex],
    )
    return RateDecision(
        allowed=bool(allowed),
        limit=limit_per_minute,
        remaining=int(remaining),
        retry_after_seconds=max(1, -(-int(retry_ms) // 1000)) if not allowed else 0,
    )
