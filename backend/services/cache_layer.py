"""
services/cache_layer.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gap 1 Fix: Redis cache with automatic in-memory fallback.

Strategy:
  - If REDIS_URL is set → use Redis (supports multi-instance deployment)
  - If not set → fall back to in-memory dict (single-instance, current behaviour)
  - API is identical either way — callers never know the difference

Usage:
  from services.cache_layer import cache_get, cache_set, cache_delete, cache_flush
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import os
import json
import threading
import logging
from typing import Any, Optional

log = logging.getLogger("cache_layer")

# ── Redis init (Upstash TLS-safe) ────────────────────────────────────────────
_redis_client = None
_REDIS_URL    = os.environ.get("REDIS_URL", "")

if _REDIS_URL:
    try:
        import redis as _redis_lib

        # Upstash uses rediss:// (TLS). The ssl_cert_reqs=None skips cert
        # verification — required for Upstash free tier shared certificates.
        _kwargs = dict(
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,   # auto-reconnect if idle
        )
        if _REDIS_URL.startswith("rediss://"):
            _kwargs["ssl_cert_reqs"] = None  # Upstash TLS

        _redis_client = _redis_lib.from_url(_REDIS_URL, **_kwargs)
        _redis_client.ping()
        log.info("✅ Redis cache connected (Upstash): %s", _REDIS_URL.split("@")[-1])
    except Exception as e:
        log.warning("⚠️  Redis unavailable (%s) — using in-memory fallback", e)
        _redis_client = None
else:
    log.info("ℹ️  REDIS_URL not set — using in-memory cache")


# ── In-memory fallback ────────────────────────────────────────────────────────
_mem_store: dict[str, Any] = {}
_mem_lock  = threading.Lock()

# Default TTL (seconds). None = no expiry.
DEFAULT_TTL = 300   # 5 minutes for DB snapshots


# =============================================================================
# PUBLIC API
# =============================================================================

def cache_get(key: str) -> Optional[Any]:
    """Get a value from cache. Returns None if missing or expired."""
    if _redis_client:
        try:
            raw = _redis_client.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as e:
            log.warning("Redis GET failed (%s) — using memory fallback", e)
            # fall through to memory
    with _mem_lock:
        return _mem_store.get(key)


def cache_set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    """Set a value in cache with optional TTL (seconds)."""
    if _redis_client:
        try:
            raw = json.dumps(value, ensure_ascii=False, default=str)
            if ttl:
                _redis_client.setex(key, ttl, raw)
            else:
                _redis_client.set(key, raw)
            return
        except Exception as e:
            log.warning("Redis SET failed (%s) — using memory fallback", e)
    with _mem_lock:
        _mem_store[key] = value


def cache_delete(key: str) -> None:
    """Delete a specific key from cache."""
    if _redis_client:
        try:
            _redis_client.delete(key)
            return
        except Exception as e:
            log.warning("Redis DEL failed (%s)", e)
    with _mem_lock:
        _mem_store.pop(key, None)


def cache_flush(pattern: str = "*") -> None:
    """Delete all keys matching pattern. Use '*' to flush everything."""
    if _redis_client:
        try:
            keys = _redis_client.keys(pattern)
            if keys:
                _redis_client.delete(*keys)
            return
        except Exception as e:
            log.warning("Redis FLUSH failed (%s)", e)
    with _mem_lock:
        if pattern == "*":
            _mem_store.clear()
        else:
            import fnmatch
            to_delete = [k for k in _mem_store if fnmatch.fnmatch(k, pattern)]
            for k in to_delete:
                del _mem_store[k]


def cache_using_redis() -> bool:
    """Returns True if Redis is active, False if using in-memory fallback."""
    return _redis_client is not None


def cache_health() -> dict:
    """Health check — called from /api/health endpoint."""
    if _redis_client:
        try:
            _redis_client.ping()
            return {"cache": "redis", "status": "ok"}
        except Exception as e:
            return {"cache": "redis", "status": "error", "error": str(e)}
    return {"cache": "memory", "status": "ok", "keys": len(_mem_store)}
