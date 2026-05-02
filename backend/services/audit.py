"""
services/audit.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gap 4 Fix: Audit log for all write operations.

Every state-changing API call is recorded with:
  - who (actor_email)
  - what restaurant (restaurant_id)
  - what action (endpoint + method)
  - when (timestamp)
  - from where (IP)
  - success/failure

Usage:
  from services.audit import audit_log
  audit_log(actor_email, restaurant_id, action, endpoint, ip, success, detail)

Or use the FastAPI middleware which logs automatically for all write endpoints.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import datetime
import logging
import threading

log = logging.getLogger("audit")

# Write audit events in a background thread so they never block responses
_audit_queue: list[dict] = []
_audit_lock  = threading.Lock()
_audit_thread_started = False


def _audit_worker() -> None:
    """Background thread: drain audit queue to Supabase."""
    import time
    while True:
        time.sleep(5)  # batch every 5 seconds
        with _audit_lock:
            events = _audit_queue.copy()
            _audit_queue.clear()
        if not events:
            continue
        try:
            from services.supabase_db import _sb
            if _sb:
                _sb.table("audit_log").insert(events).execute()
        except Exception as e:
            log.warning("Audit log flush failed: %s", e)


def _start_audit_worker() -> None:
    global _audit_thread_started
    if _audit_thread_started:
        return
    _audit_thread_started = True
    t = threading.Thread(target=_audit_worker, daemon=True, name="audit-log")
    t.start()


def audit_log(
    actor_email:   str | None,
    restaurant_id: str | None,
    action:        str,
    endpoint:      str = "",
    ip_address:    str = "",
    success:       bool = True,
    detail:        str = "",
) -> None:
    """
    Record an audit event. Non-blocking — queued for background write.
    Called by security.py require_auth and key write endpoints.
    """
    event = {
        "ts":            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "actor_email":   actor_email,
        "restaurant_id": restaurant_id,
        "action":        action,
        "endpoint":      endpoint,
        "ip_address":    ip_address,
        "success":       success,
        "detail":        detail[:500] if detail else "",
    }
    with _audit_lock:
        _audit_queue.append(event)
        if len(_audit_queue) > 500:   # safety cap: don't grow unbounded
            _audit_queue.pop(0)
    _start_audit_worker()


# =============================================================================
# FastAPI Middleware — auto-audits all POST / PUT / PATCH / DELETE requests
# =============================================================================

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class AuditMiddleware(BaseHTTPMiddleware):
    """
    Automatically logs every write operation (non-GET) to the audit trail.
    Skips: GET requests, health checks, Telegram webhooks (high-volume).
    """
    _SKIP_PATHS = {"/", "/api/health", "/api/telegram_webhook", "/api/bot_info"}
    _WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in self._WRITE_METHODS or request.url.path in self._SKIP_PATHS:
            return await call_next(request)

        # Extract actor from Bearer token (non-blocking peek)
        actor_email   = None
        restaurant_id = None
        try:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
                from services import auth as _auth
                info = _auth.validate_web_token(token)
                if info:
                    actor_email   = info.get("email")
                    restaurant_id = info.get("restaurant_id")
        except Exception:
            pass

        # IP
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )

        response = await call_next(request)
        success  = response.status_code < 400

        audit_log(
            actor_email   = actor_email,
            restaurant_id = restaurant_id,
            action        = f"{request.method} {request.url.path}",
            endpoint      = str(request.url.path),
            ip_address    = ip,
            success       = success,
            detail        = f"status={response.status_code}",
        )
        return response
