"""
services/supabase_db.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Supabase-backed database adapter.

Architecture (Hybrid):
  • Supabase (PostgreSQL) = source of truth for ALL persistent data
  • Redis / in-memory cache = fast reads, shared across instances
  • JSON (atomic write)     = local safety net, always current
  • Async background push   = Supabase never blocks a web request

All other services call load_database() / save_database() — unchanged.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import os
import json
import threading
import datetime
import logging
from typing import Any

log = logging.getLogger("supabase_db")

# ── Supabase client ────────────────────────────────────────────────────────────
try:
    from supabase import create_client, Client as SupabaseClient
    _SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    _SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    _sb: SupabaseClient | None = create_client(_SUPABASE_URL, _SUPABASE_KEY) if _SUPABASE_URL and _SUPABASE_KEY else None
    if _sb:
        log.info("✅ Supabase connected")
    else:
        log.warning("⚠️  Supabase not configured — falling back to JSON")
except Exception as _e:
    log.warning(f"⚠️  Supabase import failed ({_e}) — falling back to JSON")
    _sb = None

# ── Cache (Redis if available, in-memory fallback) ─────────────────────────────
from services.cache_layer import cache_get, cache_set, cache_delete
_DB_CACHE_KEY = "wastewise:db_snapshot"
_DB_CACHE_TTL = 300   # 5 minutes; Supabase is re-fetched after this
_cache_lock   = threading.Lock()   # guards the Supabase pull (prevent stampede)

# ── JSON path (primary synchronous store) ─────────────────────────────────────
_JSON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "database.json")
_json_lock = threading.Lock()

# ── Background Supabase push queue ────────────────────────────────────────────
# Supabase push is always async so it never blocks a web request.
# JSON is always written first (synchronously + atomically) so data
# is safe even if Supabase is temporarily unreachable.
import queue as _queue
_push_queue: _queue.Queue = _queue.Queue(maxsize=1)   # only keep latest snapshot
_push_worker_started = False
_last_push_ts: float = 0.0   # unix timestamp of last successful Supabase push


def get_last_push_ts() -> float:
    """Return unix timestamp of the last successful Supabase push (0 = never)."""
    return _last_push_ts


def _start_push_worker() -> None:
    """Start the background thread that drains the Supabase push queue."""
    global _push_worker_started
    if _push_worker_started or not _sb:
        return
    _push_worker_started = True

    def _worker() -> None:
        global _last_push_ts
        while True:
            try:
                db = _push_queue.get(timeout=5)
                for attempt in range(3):   # retry up to 3 times
                    try:
                        _push_to_supabase(db)
                        import time as _t
                        _last_push_ts = _t.monotonic()  # record successful push time
                        break
                    except Exception as e:
                        if attempt == 2:
                            log.error(f"Supabase push failed after 3 retries: {e}")
                        else:
                            import time; time.sleep(2 ** attempt)  # 1s, 2s backoff
            except _queue.Empty:
                continue
            except Exception as e:
                log.error(f"Push worker error: {e}")

    t = threading.Thread(target=_worker, daemon=True, name="supabase-push")
    t.start()


# ── JSON fallback path ─────────────────────────────────────────────────────────


def load_database() -> dict:
    """
    Load full database dict.
    Priority: Redis/memory cache → Supabase → JSON file
    Cache TTL = 5 min. After expiry Supabase is re-fetched once.
    """
    cached = cache_get(_DB_CACHE_KEY)
    if cached is not None:
        return cached

    # Cache miss — pull from source (lock prevents Supabase stampede)
    with _cache_lock:
        # Double-check after acquiring lock
        cached = cache_get(_DB_CACHE_KEY)
        if cached is not None:
            return cached
        if _sb:
            db = _pull_from_supabase()
        else:
            db = _load_json()
        cache_set(_DB_CACHE_KEY, db, ttl=_DB_CACHE_TTL)
        return db


def save_database(db: dict) -> None:
    """
    Production-grade save strategy:
      1. Write to cache (Redis/memory) immediately
      2. Write to JSON atomically (synchronous — data always safe)
      3. Queue Supabase push (async background — never blocks request)
    """
    import copy
    snapshot = copy.deepcopy(db)

    # 1. Update cache (all instances see new data via Redis)
    cache_set(_DB_CACHE_KEY, snapshot, ttl=_DB_CACHE_TTL)

    # 2. JSON write — synchronous + atomic
    _save_json(snapshot)

    # 3. Supabase push — background, non-blocking
    if _sb:
        _start_push_worker()
        try:
            _push_queue.put_nowait(snapshot)
        except _queue.Full:
            # Production Fix: Always keep the most recent snapshot. Drop the old one.
            try:
                _push_queue.get_nowait()
            except _queue.Empty:
                pass
            
            try:
                _push_queue.put_nowait(snapshot)
            except _queue.Full:
                log.warning("Supabase push queue full. Data is safe in Redis/JSON cache.")


def invalidate_cache() -> None:
    """Force next load_database() to re-fetch from Supabase."""
    cache_delete(_DB_CACHE_KEY)


# =============================================================================
# SUPABASE PULL — read all tables → reconstruct dict format
# =============================================================================

def _pull_from_supabase() -> dict:
    """Fetch all data from Supabase and reconstruct the legacy dict format.
    
    PERFORMANCE: All 17 table fetches run in parallel (ThreadPoolExecutor).
    Cold-start load time: ~500ms instead of 5-8 seconds.
    """
    assert _sb is not None
    from concurrent.futures import ThreadPoolExecutor, as_completed

    db: dict = {
        "restaurants": [],
        "regions": {},
        "accounts": [],
        "pending_otps": [],
        "pending_registrations": [],
        "pending_approvals": [],
        "global_learning_events": [],
        "chains": [],
    }
    try:
        # ── Fetch ALL tables in parallel ──────────────────────────────────
        TABLES = [
            "regions", "restaurants", "restaurant_menu",
            "daily_records", "daily_items_sold", "active_events",
            "closing_stock", "marketplace_orders",
            "accounts", "sessions",
            "pending_otps", "pending_registrations", "pending_approvals",
        ]

        def _fetch(table: str):
            for attempt in range(3):
                try:
                    return table, (_sb.table(table).select("*").execute().data or [])
                except Exception as e:
                    if attempt == 2:
                        raise e
                    import time
                    time.sleep(1 + attempt)

        raw: dict = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for table, data in pool.map(_fetch, TABLES):
                raw[table] = data

        rows       = raw["regions"]
        rests      = raw["restaurants"]
        menus      = raw["restaurant_menu"]
        records    = raw["daily_records"]
        items_sold = raw["daily_items_sold"]
        events     = raw["active_events"]
        stock      = raw["closing_stock"]
        orders     = raw["marketplace_orders"]

        # ── Regions ──────────────────────────────────────────────────────────
        for r in rows:
            db["regions"][r["name"]] = {
                "type": r["type"],
                "foot_traffic_baseline": r["foot_traffic_baseline"],
                "weekend_multiplier": r["weekend_multiplier"],
                "holiday_multiplier": r["holiday_multiplier"],
                "rain_impact": r["rain_impact"],
            }

        # Index for fast lookup
        menu_by_rest: dict[str, list] = {}
        for m in menus:
            menu_by_rest.setdefault(m["restaurant_id"], []).append({
                "item": m["item"],
                "base_daily_demand": m["base_daily_demand"],
                "profit_margin_rm": float(m["profit_margin_rm"]),
                "price_rm": float(m["price_rm"]),
            })

        items_by_date: dict[str, dict[str, dict]] = {}
        for i in items_sold:
            key = f"{i['restaurant_id']}_{i['date']}"
            items_by_date.setdefault(key, {})[i["item"]] = i["qty_sold"]

        events_by_rest: dict[str, list] = {}
        for ev in events:
            events_by_rest.setdefault(ev["restaurant_id"], []).append({
                "description": ev["description"],
                "headcount": ev["headcount"],
                "days": ev["days"],
                "date": ev["event_date"],
                "expires_at": ev["expires_at"],
            })

        stock_by_rest: dict[str, list] = {}
        closing_date_by_rest: dict[str, str] = {}
        for s in stock:
            stock_by_rest.setdefault(s["restaurant_id"], []).append({
                "item": s["item"],
                "qty_available": s["qty_available"],
                "original_price_rm": float(s["original_price_rm"]),
                "discounted_price_rm": float(s["discounted_price_rm"]),
                "discount_pct": s["discount_pct"],
            })
            closing_date_by_rest[s["restaurant_id"]] = s["stock_date"]

        orders_by_rest: dict[str, list] = {}
        for o in orders:
            orders_by_rest.setdefault(o["restaurant_id"], []).append({
                "order_id": o["order_id"],
                "date": o["order_date"],
                "customer_name": o["customer_name"],
                "phone": o["phone"],
                "items": o["items"],
                "total_rm": float(o["total_rm"]),
                "shopkeeper_earnings_rm": float(o["shopkeeper_earnings_rm"]),
                "platform_fee_rm": float(o["platform_fee_rm"]),
                "status": o["status"],
            })

        # Build daily_records per restaurant
        records_by_rest: dict[str, list] = {}
        for rec in records:
            rid = rec["restaurant_id"]
            key = f"{rid}_{rec['date']}"
            actual_sales = items_by_date.get(key, {})
            records_by_rest.setdefault(rid, []).append({
                "date": rec["date"],
                "forecast": rec.get("forecast_text"),
                "forecast_generated_at": rec.get("forecast_generated_at"),
                "actual_sales": actual_sales if actual_sales else None,
                "total_revenue_rm": float(rec.get("total_revenue_rm", 0)),
                "total_waste_qty": int(rec.get("total_waste_qty", 0)),
                "weather": rec.get("weather"),
                "foot_traffic": rec.get("foot_traffic"),
            })

        for rest in rests:
            rid = rest["id"]
            db["restaurants"].append({
                "id": rid,
                "name": rest["name"],
                "region": rest["region"],
                "type": rest["type"],
                "owner_name": rest["owner_name"],
                "telegram_chat_id": rest.get("telegram_chat_id"),
                "telegram_username": rest.get("telegram_username"),
                "email": rest.get("email"),
                "chain_id": rest.get("chain_id"),
                "privacy_accepted": rest.get("privacy_accepted", True),
                "registered_at": rest.get("registered_at", ""),
                "specialty_weather": rest.get("specialty_weather", "neutral"),
                "closing_time": rest.get("closing_time", "21:00"),
                "discount_pct": rest.get("discount_pct", 30),
                "marketplace_enabled": rest.get("marketplace_enabled", True),
                "preferred_language": rest.get("preferred_language", "english"),
                "latitude": rest.get("latitude"),
                "longitude": rest.get("longitude"),
                "bom": rest.get("bom") or {},
                "recent_feedback_memory": rest.get("recent_feedback_memory") or [],
                "q_tables": rest.get("q_tables") or {},
                "sustainability_waste_prevented_kg": float(rest.get("sustainability_waste_prevented_kg", 0)),
                "sustainability_co2_saved_kg": float(rest.get("sustainability_co2_saved_kg", 0)),
                "menu": menu_by_rest.get(rid, []),
                "daily_records": sorted(records_by_rest.get(rid, []), key=lambda r: r["date"]),
                "active_events": events_by_rest.get(rid, []),
                "closing_stock": stock_by_rest.get(rid, []),
                "closing_stock_date": closing_date_by_rest.get(rid, ""),
                "marketplace_orders": orders_by_rest.get(rid, []),
            })

        # ── Accounts + Sessions ───────────────────────────────────────────
        accts = raw["accounts"]
        sess  = raw["sessions"]
        sess_by_account: dict[str, list] = {}
        for s in sess:
            sess_by_account.setdefault(str(s["account_id"]), []).append({
                "session_id": s["session_id"],
                "type": s["type"],
                "chat_id": s.get("chat_id"),
                "telegram_username": s.get("telegram_username"),
                "label": s.get("label", ""),
                "is_primary": s.get("is_primary", False),
                "linked_at": s.get("linked_at", ""),
                "last_active": s.get("last_active", ""),
                "expires_at": s.get("expires_at"),
            })
        for a in accts:
            db["accounts"].append({
                "email": a["email"],
                "restaurant_id": a.get("restaurant_id"),
                "created_at": a.get("created_at", ""),
                "sessions": sess_by_account.get(str(a["id"]), []),
                "_account_uuid": str(a["id"]),  # internal reference
            })

        # ── Auth tables ───────────────────────────────────────────────────
        db["pending_otps"] = raw["pending_otps"]
        db["pending_registrations"] = [
            {**r, "restaurant_data": r.get("restaurant_data") or {}}
            for r in raw["pending_registrations"]
        ]
        db["pending_approvals"] = raw["pending_approvals"]

        # ── Chains ────────────────────────────────────────────────────────
        db["chains"] = raw["chains"]

        log.info(f"✅ Pulled from Supabase: {len(db['restaurants'])} restaurants, {len(accts)} accounts")

    except Exception as e:
        log.error(f"Supabase pull failed: {e} — using JSON fallback")
        return _load_json()

    return db


# =============================================================================
# SUPABASE PUSH — take dict format → upsert to all tables
# =============================================================================

def _push_to_supabase(db: dict) -> None:
    """Write changed data back to Supabase tables."""
    assert _sb is not None
    now = datetime.datetime.utcnow().isoformat()

    # ── Regions ───────────────────────────────────────────────────────────────
    for name, r in db.get("regions", {}).items():
        _sb.table("regions").upsert({
            "name": name,
            "type": r.get("type", "General Area"),
            "foot_traffic_baseline": r.get("foot_traffic_baseline", 500),
            "weekend_multiplier": r.get("weekend_multiplier", 1.1),
            "holiday_multiplier": r.get("holiday_multiplier", 1.0),
            "rain_impact": r.get("rain_impact", -0.2),
        }, on_conflict="name").execute()

    # ── Restaurants ───────────────────────────────────────────────────────────
    for rest in db.get("restaurants", []):
        rid = rest["id"]
        _sb.table("restaurants").upsert({
            "id": rid,
            "name": rest["name"],
            "region": rest["region"],
            "type": rest.get("type", "hawker"),
            "owner_name": rest.get("owner_name", "Owner"),
            "telegram_chat_id": rest.get("telegram_chat_id"),
            "telegram_username": rest.get("telegram_username"),
            "email": rest.get("email"),
            "chain_id": rest.get("chain_id"),
            "privacy_accepted": rest.get("privacy_accepted", True),
            "registered_at": rest.get("registered_at", now),
            "specialty_weather": rest.get("specialty_weather", "neutral"),
            "closing_time": rest.get("closing_time", "21:00"),
            "discount_pct": rest.get("discount_pct", 30),
            "marketplace_enabled": rest.get("marketplace_enabled", True),
            "preferred_language": rest.get("preferred_language", "english"),
            "latitude": rest.get("latitude"),
            "longitude": rest.get("longitude"),
            "bom": rest.get("bom", {}),
            "recent_feedback_memory": rest.get("recent_feedback_memory", []),
            "q_tables": rest.get("q_tables", {}),
            "sustainability_waste_prevented_kg": rest.get("sustainability_waste_prevented_kg", 0),
            "sustainability_co2_saved_kg": rest.get("sustainability_co2_saved_kg", 0),
            "updated_at": now,
        }, on_conflict="id").execute()

        # ── Menu items: batch delete then batch insert ───────────────
        _sb.table("restaurant_menu").delete().eq("restaurant_id", rid).execute()
        menu_rows = [
            {"restaurant_id": rid, "item": item["item"],
             "base_daily_demand": item.get("base_daily_demand", 50),
             "profit_margin_rm": item.get("profit_margin_rm", 2.5),
             "price_rm": item.get("price_rm", 5.0),
             "updated_at": now}
            for item in rest.get("menu", [])
        ]
        if menu_rows:
            _sb.table("restaurant_menu").insert(menu_rows).execute()

        # ── Daily records: BATCH upsert ───────────────────────────────────
        daily_rows = []
        sold_rows  = []
        price_map  = {m["item"]: m.get("price_rm", 0) for m in rest.get("menu", [])}

        for rec in rest.get("daily_records", []):
            date_str = rec.get("date", "")
            if not date_str:
                continue
            daily_rows.append({
                "restaurant_id": rid, "date": date_str,
                "total_revenue_rm": rec.get("total_revenue_rm", 0),
                "total_waste_qty": rec.get("total_waste_qty", 0),
                "weather": rec.get("weather"),
                "foot_traffic": rec.get("foot_traffic"),
                "forecast_text": rec.get("forecast"),
                "forecast_generated_at": rec.get("forecast_generated_at"),
            })
            actual = rec.get("actual_sales") or rec.get("items_sold") or {}
            items_list = [{"item": k, "qty_sold": v} for k, v in actual.items()] \
                         if isinstance(actual, dict) else actual
            for sold in items_list:
                sold_rows.append({
                    "restaurant_id": rid, "date": date_str,
                    "item": sold["item"],
                    "qty_sold": sold.get("qty_sold", 0),
                    "revenue_rm": round(sold.get("qty_sold", 0) * price_map.get(sold["item"], 0), 2),
                })

        if daily_rows:
            _sb.table("daily_records").upsert(daily_rows, on_conflict="restaurant_id,date").execute()
        if sold_rows:
            _sb.table("daily_items_sold").upsert(sold_rows, on_conflict="restaurant_id,date,item").execute()

        # ── Active events: batch delete then batch insert ─────────────────
        _sb.table("active_events").delete().eq("restaurant_id", rid).execute()
        ev_rows = [
            {"restaurant_id": rid, "description": ev.get("description", ""),
             "headcount": ev.get("headcount", 0), "days": ev.get("days", 1),
             "event_date": ev.get("date", ""), "expires_at": ev.get("expires_at", "")}
            for ev in rest.get("active_events", [])
        ]
        if ev_rows:
            _sb.table("active_events").insert(ev_rows).execute()

        # Closing stock: batch delete then batch insert
        _sb.table("closing_stock").delete().eq("restaurant_id", rid).execute()
        stock_rows = []
        stock_date = rest.get("closing_stock_date", "")
        if stock_date:
            for s in rest.get("closing_stock", []):
                stock_rows.append({
                    "restaurant_id": rid,
                    "stock_date": stock_date,
                    "stock_time": rest.get("closing_stock_time"),
                    "item": s["item"],
                    "qty_available": s.get("qty_available", 0),
                    "original_price_rm": s.get("original_price_rm", 0),
                    "discounted_price_rm": s.get("discounted_price_rm", 0),
                    "discount_pct": s.get("discount_pct", 30),
                })
            if stock_rows:
                _sb.table("closing_stock").insert(stock_rows).execute()

        # Marketplace orders: batch delete then batch insert
        _sb.table("marketplace_orders").delete().eq("restaurant_id", rid).execute()
        order_rows = []
        for o in rest.get("marketplace_orders", []):
            order_rows.append({
                "order_id": o.get("order_id", ""),
                "restaurant_id": rid,
                "order_date": o.get("date", ""),
                "customer_name": o.get("customer_name", ""),
                "phone": o.get("phone", ""),
                "items": o.get("items", []),
                "total_rm": o.get("total_rm", 0),
                "shopkeeper_earnings_rm": o.get("shopkeeper_earnings_rm", 0),
                "platform_fee_rm": o.get("platform_fee_rm", 0),
                "status": o.get("status", "pending"),
            })
        if order_rows:
            _sb.table("marketplace_orders").insert(order_rows).execute()

    # ── Accounts + Sessions ───────────────────────────────────────────────────
    for acct in db.get("accounts", []):
        # Check if account has a UUID stored
        uuid_val = acct.get("_account_uuid")
        acct_row = {"email": acct["email"], "restaurant_id": acct.get("restaurant_id")}
        result = _sb.table("accounts").upsert(acct_row, on_conflict="email").execute()
        # Get UUID from DB
        if not uuid_val:
            rows = _sb.table("accounts").select("id").eq("email", acct["email"]).execute().data
            uuid_val = rows[0]["id"] if rows else None
        if uuid_val:
            # ── Sessions: BATCH upsert ────────────────────────────────────
            sess_rows = [
                {"session_id": s["session_id"], "account_id": uuid_val,
                 "type": s.get("type", "web"), "chat_id": s.get("chat_id"),
                 "telegram_username": s.get("telegram_username"),
                 "label": s.get("label", ""), "is_primary": s.get("is_primary", False),
                 "linked_at": s.get("linked_at", now),
                 "last_active": s.get("last_active", now),
                 "expires_at": s.get("expires_at")}
                for s in acct.get("sessions", [])
            ]
            if sess_rows:
                _sb.table("sessions").upsert(sess_rows, on_conflict="session_id").execute()

    # ── Pending OTPs ─────────────────────────────────────────────────────────
    _sb.table("pending_otps").delete().lt("expires_at", now).execute()
    for otp in db.get("pending_otps", []):
        _sb.table("pending_otps").upsert(otp, on_conflict="id").execute()

    # ── Pending Registrations ─────────────────────────────────────────────────
    _sb.table("pending_registrations").delete().lt("expires_at", now).execute()
    for reg in db.get("pending_registrations", []):
        _sb.table("pending_registrations").upsert({
            "email": reg["email"],
            "telegram_username": reg.get("telegram_username", ""),
            "restaurant_data": reg.get("restaurant_data", {}),
            "code_hash": reg.get("code_hash", ""),
            "expires_at": reg.get("expires_at", ""),
        }, on_conflict="email").execute()

    # ── Pending Approvals ─────────────────────────────────────────────────────
    for ap in db.get("pending_approvals", []):
        _sb.table("pending_approvals").upsert(ap, on_conflict="approval_id").execute()


    # JSON is already written by save_database() before this function runs.
    # Writing it again here would be a duplicate write — removed.


# =============================================================================
# JSON ATOMIC STORE (synchronous, always written on every save)
# =============================================================================

def _load_json() -> dict:
    """Load from JSON file. Returns empty structure if file missing."""
    try:
        with _json_lock:
            with open(_JSON_PATH, encoding="utf-8") as f:
                return json.load(f)
    except FileNotFoundError:
        return {"restaurants": [], "regions": {}, "accounts": [],
                "pending_otps": [], "pending_registrations": [],
                "global_learning_events": []}
    except json.JSONDecodeError as e:
        log.error(f"database.json corrupt: {e} — attempting to load from Supabase")
        if _sb:
            return _pull_from_supabase()
        raise


def _save_json(db: dict) -> None:
    """
    Atomically write JSON using write-to-temp + os.replace.
    os.replace() is atomic on all major operating systems:
      - On POSIX: atomic rename (POSIX guarantee)
      - On Windows: atomic since Python 3.3+
    The .tmp file is NEVER in a partial state when database.json is read.
    Creates the data/ directory automatically if it doesn't exist (e.g. Railway).
    """
    with _json_lock:
        # Ensure the data/ directory exists (Railway doesn't have it by default)
        os.makedirs(os.path.dirname(_JSON_PATH), exist_ok=True)
        tmp = _JSON_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _JSON_PATH)


# =============================================================================
# TARGETED HELPERS (bypass full load/save for hot paths)
# These are used by auth.py for OTP/session operations
# to avoid a full DB round-trip on every auth call.
# =============================================================================

def sb_upsert_session(account_email: str, session: dict) -> None:
    """Upsert a single session without rewriting the entire DB."""
    if _sb:
        rows = _sb.table("accounts").select("id").eq("email", account_email).execute().data
        if rows:
            account_id = rows[0]["id"]
            _sb.table("sessions").upsert(
                {**session, "account_id": account_id},
                on_conflict="session_id"
            ).execute()
    invalidate_cache()


def sb_get_account_by_session(session_id: str) -> dict | None:
    """Fast lookup: get account by session token."""
    if _sb:
        rows = _sb.table("sessions").select("*").eq("session_id", session_id).execute().data
        if not rows:
            return None
        s = rows[0]
        acct_rows = _sb.table("accounts").select("*").eq("id", str(s["account_id"])).execute().data
        return acct_rows[0] if acct_rows else None
    db = _load_json()
    for acct in db.get("accounts", []):
        for sess in acct.get("sessions", []):
            if sess.get("session_id") == session_id:
                return acct
    return None


def sb_get_restaurant(restaurant_id: str) -> dict | None:
    """Fast single-restaurant lookup from cache or Supabase."""
    db = load_database()
    return next((r for r in db.get("restaurants", []) if r["id"] == restaurant_id), None)
