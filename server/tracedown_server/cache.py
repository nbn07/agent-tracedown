"""Redis: a shared read cache, a shared live feed and ingest rate limits.

Set ``TDN_REDIS_URL=redis://host:6379/0`` and every server replica (and ``tracedown-worker``)
shares three things:

* **Read cache.** Run lists, run details, findings, agent networks and analytics are cached.
  Entries are keyed by a generation number per project and per trace; a write (new spans,
  findings or a profile) bumps the generation, so stale entries are never served even when
  another replica or the Kafka worker did the write. TTLs bound everything else.
* **Live feed.** ``RedisEventBus`` sends every event through Redis pub/sub, so a browser
  connected to any replica sees spans and runs ingested by any other.
* **Rate limits.** ``TDN_INGEST_RATE_LIMIT`` (requests per minute per API key or user) is
  counted in Redis across replicas, or in memory without Redis.

Without ``TDN_REDIS_URL`` nothing is cached and the live feed stays in-process, as before.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from .pipeline import EventBus

log = logging.getLogger("tracedown.cache")

PREFIX = os.environ.get("TDN_REDIS_PREFIX", "tdn")
CHANNEL_SUFFIX = "events"
DEFAULT_TTL = int(os.environ.get("TDN_CACHE_TTL", "300"))


class RedisCache:
    """Generation-keyed read-through cache."""

    def __init__(self, url: str, prefix: str = PREFIX) -> None:
        import redis
        self.url = url
        self.prefix = prefix
        self.r = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2, health_check_interval=30)
        self.r.ping()
        self.hits = self.misses = 0

    # ------------------------------------------------------------------ generations
    def _gen_keys(self, project: Optional[str], trace_id: Optional[str]) -> List[str]:
        keys = [f"{self.prefix}:gen:all", f"{self.prefix}:gen:p:{project or '*'}"]
        if trace_id:
            keys.append(f"{self.prefix}:gen:t:{trace_id}")
        return keys

    def bump(self, project: Optional[str] = None, trace_id: Optional[str] = None) -> None:
        """Invalidate everything cached for a project (and a trace)."""
        try:
            pipe = self.r.pipeline()
            for k in self._gen_keys(project, trace_id):
                pipe.incr(k)
            if project:  # cross-project views are keyed by project '*'
                pipe.incr(f"{self.prefix}:gen:p:*")
            pipe.execute()
        except Exception as exc:  # noqa: BLE001 - a cache must never break writes
            log.warning("cache invalidation failed: %s", exc)

    # ------------------------------------------------------------------ read-through
    def get_or_set(self, name: str, fn: Callable[[], Any], project: Optional[str] = None,
                   trace_id: Optional[str] = None, args: Iterable[Any] = (), ttl: int = DEFAULT_TTL) -> Any:
        try:
            gens = self.r.mget(self._gen_keys(project, trace_id))
            raw_key = json.dumps([name, project, trace_id, list(args), [g.decode() if g else "0" for g in gens]],
                                 default=str)
            key = f"{self.prefix}:c:{name}:{hashlib.sha1(raw_key.encode()).hexdigest()}"
            hit = self.r.get(key)
        except Exception as exc:  # noqa: BLE001 - Redis down: serve from the database
            log.warning("cache read failed: %s", exc)
            return fn()
        if hit is not None:
            self.hits += 1
            return json.loads(hit)
        self.misses += 1
        value = fn()
        try:
            self.r.set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache write failed: %s", exc)
        return value

    def event_bus(self) -> "RedisEventBus":
        return RedisEventBus(self.url, channel=f"{self.prefix}:{CHANNEL_SUFFIX}")


class CachedStore:
    """Wraps any store (SQLite, Postgres, ClickHouse hybrid) with the Redis cache.

    Reads in ``READS`` are cached; writes go straight through and then invalidate. Every other
    attribute is the wrapped store's.
    """

    def __init__(self, inner, cache: RedisCache) -> None:
        self._inner = inner
        self._cache = cache

    def __getattr__(self, name: str) -> Any:  # only called for attributes the wrapper lacks
        return getattr(self._inner, name)

    # ------------------------------------------------------------------ cached reads
    def trace_project(self, trace_id: str) -> Optional[str]:
        # Ownership never changes once set, so a positive answer can live long.
        return self._cache.get_or_set("trace_project", lambda: self._inner.trace_project(trace_id),
                                      trace_id=trace_id, ttl=3600) or self._inner.trace_project(trace_id)

    def get_spans(self, trace_id: str) -> List[Dict[str, Any]]:
        return self._cache.get_or_set("spans", lambda: self._inner.get_spans(trace_id),
                                      project=self._project(trace_id), trace_id=trace_id)

    def get_run(self, trace_id: str) -> Optional[Dict[str, Any]]:
        return self._cache.get_or_set("run", lambda: self._inner.get_run(trace_id),
                                      project=self._project(trace_id), trace_id=trace_id)

    def get_findings(self, trace_id: str) -> Optional[Dict[str, Any]]:
        return self._cache.get_or_set("findings", lambda: self._inner.get_findings(trace_id),
                                      project=self._project(trace_id), trace_id=trace_id)

    def list_runs(self, project: Optional[str] = None, agent: Optional[str] = None, outcome: Optional[str] = None,
                  limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        return self._cache.get_or_set("runs", lambda: self._inner.list_runs(project, agent, outcome, limit, offset),
                                      project=project, args=(agent, outcome, limit, offset), ttl=60)

    def stats(self, project: Optional[str] = None) -> Dict[str, Any]:
        return self._cache.get_or_set("stats", lambda: self._inner.stats(project), project=project, ttl=60)

    def _project(self, trace_id: str) -> Optional[str]:
        return self.trace_project(trace_id)

    # ------------------------------------------------------------------ writes: through, then invalidate
    def put_spans(self, project: str, spans: Iterable[Dict[str, Any]]) -> int:
        spans = list(spans)
        n = self._inner.put_spans(project, spans)
        for tid in {s["trace_id"] for s in spans}:
            self._cache.bump(project, tid)
        return n

    def put_findings(self, trace_id: str, report: Dict[str, Any], created_at: int) -> None:
        self._inner.put_findings(trace_id, report, created_at)
        self._cache.bump(self._inner.trace_project(trace_id), trace_id)

    def set_profile(self, trace_id: str, profile: Dict[str, Any]) -> None:
        self._inner.set_profile(trace_id, profile)
        self._cache.bump(self._inner.trace_project(trace_id), trace_id)


def wrap_store(store, cache: Optional[RedisCache]):
    if cache is None or isinstance(store, CachedStore):
        return store
    return CachedStore(store, cache)


def unwrap(store):
    return store._inner if isinstance(store, CachedStore) else store


# ----------------------------------------------------------------------------- live feed

class RedisEventBus(EventBus):
    """``EventBus`` whose events travel through Redis pub/sub, so all replicas see all events.

    ``publish`` sends to Redis; one listener thread per process delivers each message to the
    local subscribers. If Redis is unreachable, events are delivered locally only.
    """

    def __init__(self, url: str, channel: str = f"{PREFIX}:{CHANNEL_SUFFIX}", max_queue: int = 1000) -> None:
        import redis
        super().__init__(max_queue=max_queue)
        self.channel = channel
        self._r = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        self._ps = redis.Redis.from_url(url).pubsub(ignore_subscribe_messages=True)
        self._ps.subscribe(channel)
        self._ready = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._listen, name="tdn-redis-bus", daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def _listen(self) -> None:
        self._ready.set()
        backoff = 1.0
        while not self._stop:
            try:
                msg = self._ps.get_message(timeout=1.0)
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - redis-py resubscribes on the next read
                if self._stop:
                    return
                log.warning("live feed: redis read failed (retrying in %.0fs): %s", backoff, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if msg and msg.get("type") == "message":
                try:
                    EventBus.publish(self, json.loads(msg["data"]))
                except ValueError:
                    continue

    def publish(self, event: Dict[str, Any]) -> None:
        try:
            self._r.publish(self.channel, json.dumps(event, default=str))
        except Exception as exc:  # noqa: BLE001 - Redis down: at least this replica's viewers get it
            log.warning("live feed: redis publish failed, delivering locally: %s", exc)
            EventBus.publish(self, event)

    def close(self) -> None:
        self._stop = True
        try:
            self._ps.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------------- rate limits

class RateLimiter:
    """Fixed one-minute windows per principal: Redis-backed (shared) or in memory."""

    def __init__(self, per_minute: int, cache: Optional[RedisCache] = None) -> None:
        self.per_minute = per_minute
        self.cache = cache
        self._local: Dict[str, int] = {}
        self._lock = threading.Lock()

    def allow(self, who: str) -> bool:
        if self.per_minute <= 0:
            return True
        window = int(time.time() // 60)
        key = f"{PREFIX}:rl:{who}:{window}"
        if self.cache is not None:
            try:
                pipe = self.cache.r.pipeline()
                pipe.incr(key)
                pipe.expire(key, 120)
                n = pipe.execute()[0]
                return n <= self.per_minute
            except Exception as exc:  # noqa: BLE001 - fail open: telemetry must not be dropped on a cache outage
                log.warning("rate limit check failed: %s", exc)
                return True
        with self._lock:
            for k in [k for k in self._local if not k.endswith(f":{window}")]:
                del self._local[k]
            self._local[key] = self._local.get(key, 0) + 1
            return self._local[key] <= self.per_minute


def cache_from_env() -> Optional[RedisCache]:
    url = os.environ.get("TDN_REDIS_URL")
    if not url:
        return None
    try:
        return RedisCache(url)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"TDN_REDIS_URL is set but Redis is unreachable ({exc})") from exc
