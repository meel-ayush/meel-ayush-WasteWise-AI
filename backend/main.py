import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import threading
import uuid as _uuid_mod
import datetime as _dt_mod
from fastapi import FastAPI, HTTPException, File, UploadFile, Form, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from dotenv import load_dotenv
load_dotenv()

from services import auth
from services.bom_ai import ask_bom_conversational
from services.security import (
    require_auth, require_restaurant_access,
    AuthenticatedUser, SecurityHeadersMiddleware,
    validate_email, validate_telegram_username, validate_item_name,
    sanitise_text, validate_otp_code, validate_closing_time,
    check_otp_rate_limit, record_failed_otp, clear_otp_attempts, get_client_ip,
)
from services.audit import AuditMiddleware

from services.nlp import (
    generate_morning_forecast, process_ai_data_ingestion, register_owner_event,
    get_accuracy_data, load_database, save_database, _get_restaurant,
    get_active_events, _do_generate_forecast, process_image_upload, get_shopping_list,
)
from services.cache import invalidate_forecast, invalidate_intelligence, invalidate_db
from services.inventory import (
    compute_remaining_inventory, compute_profit_split,
    get_today_profit_summary, get_weekly_profit_data,
    build_closing_time_telegram_message,
    get_all_marketplace_restaurants, get_dynamic_discount, get_marketplace_menu,
    get_marketplace_listings, ai_optimize_discounts,
)
import re as _re

from services.file_processor import process_upload, extract_image_mime

# ── Advanced AI Feature Modules ────────────────────────────────────────────────
from services.causal_ai import analyse_underperformance, format_causal_report_telegram
from services.menu_engineering import (
    classify_menu_items, generate_menu_recommendations, get_weekly_menu_report_telegram
)
from services.chain_management import (
    create_chain, add_branch_to_chain, get_chain_summary,
    push_menu_template_to_chain, format_chain_telegram_summary
)
from services.federated_learning import run_federated_round
from services.computer_vision_inventory import scan_inventory_from_image

app = FastAPI(
    title="WasteWise AI",
    version="9.0.0",
    docs_url=None,            # Disable Swagger UI in production
    redoc_url=None,           # Disable ReDoc in production
    openapi_url=None,         # Disable OpenAPI schema endpoint in production
)

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Security headers on every response
app.add_middleware(SecurityHeadersMiddleware)

# Gap 4: Audit all write operations automatically
app.add_middleware(AuditMiddleware)

# CORS — strict origin enforcement
# ALLOWED_ORIGINS MUST be explicitly set.
# Failing to set it raises an error at startup.
_origins_raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
if not _origins_raw:
    raise RuntimeError(
        "[STARTUP] ALLOWED_ORIGINS environment variable is not set. "
        "Set it to your frontend URL (e.g. https://yourapp.vercel.app or http://localhost:3000) before starting."
    )
ALLOWED_ORIGINS = [o.strip() for o in _origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)


@app.on_event("startup")
def on_startup():
    """Run startup tasks: backfills, bucket setup, scheduler start."""
    _backfill_price_rm()
    _backfill_new_restaurant_fields()
    try:
        from services.storage_service import _ensure_buckets
        _ensure_buckets()
    except Exception as e:
        print(f"[Startup] Storage bucket setup skipped: {e}")
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if token:
        from services.scheduler import start_scheduler
        start_scheduler(token)






def _backfill_price_rm():
    """
    Phase 0 critical fix: add price_rm to any menu item that is missing it.
    Estimates from profit_margin_rm using formula: price = margin / 0.60.
    """
    try:
        db = load_database()
        changed = False
        for restaurant in db.get("restaurants", []):
            for item in restaurant.get("menu", []):
                if "price_rm" not in item:
                    pm = item.get("profit_margin_rm", 0)
                    item["price_rm"] = round(pm / 0.60, 2) if pm > 0 else 5.00
                    changed = True
                if "halal_certified" not in item:
                    item["halal_certified"] = True
                    changed = True
                if "allergens" not in item:
                    item["allergens"] = []
                    changed = True
                if "description" not in item:
                    item["description"] = ""
                    changed = True
        if changed:
            save_database(db)
            print("[Startup] Backfilled missing menu fields (price_rm, halal, allergens).")
    except Exception as e:
        print(f"[Startup] price_rm backfill failed: {e}")


def _backfill_new_restaurant_fields():
    """Add new restaurant-level fields introduced in v9.0 to existing restaurants."""
    try:
        db = load_database()
        changed = False
        for restaurant in db.get("restaurants", []):
            defaults = {
                "preferred_language": "english",
                "sustainability_totals": {"waste_prevented_kg": 0.0, "co2_saved_kg": 0.0},
                "ingredient_purchases": [],
                "gamification": {
                    "current_streak": 0, "longest_streak": 0,
                    "last_log_date": None, "total_logs": 0, "accuracy_milestones": []
                },
                "chain_id": None,
                
                "q_tables": {},
                "bayesian_beliefs": {},
            }
            for field, default in defaults.items():
                if field not in restaurant:
                    restaurant[field] = default
                    changed = True
        if changed:
            save_database(db)
            print("[Startup] Backfilled new restaurant fields (v9.0).")
    except Exception as e:
        print(f"[Startup] Restaurant field backfill failed: {e}")


class UploadPayload(BaseModel):
    restaurant_id: str
    action:        str
    menu_mode:     str = "none"

class EventPayload(BaseModel):
    description: str = Field(..., min_length=3, max_length=200)
    headcount:   int = Field(..., ge=1, le=100_000)
    days:        int = Field(1, ge=1, le=30)

class MenuItemPayload(BaseModel):
    item:              str        = Field(..., min_length=1, max_length=100)
    base_daily_demand: int        = Field(50, ge=1, le=10_000)
    profit_margin_rm:  float      = Field(2.50, ge=0.10, le=500.0)
    price_rm:          float      = Field(5.00, ge=0.10, le=1000.0)  # REQUIRED in v9
    halal_certified:   bool       = Field(True)
    allergens:         List[str]  = Field(default_factory=list)
    description:       str        = Field("", max_length=300)

class MarketplaceAuthPayload(BaseModel):
    email:    str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=8, max_length=200)
    name:     str = Field("", max_length=100)
    phone:    str = Field("", max_length=20)

class ClosingTimePayload(BaseModel):
    closing_time:        str  = Field(..., pattern=r'^\d{2}:\d{2}$')   # HH:MM
    discount_pct:        int  = Field(30, ge=5, le=70)
    marketplace_enabled: bool = Field(True)

class CustomerOrderPayload(BaseModel):
    restaurant_id: str  = Field(...)
    customer_name: str  = Field(..., min_length=1, max_length=100)
    phone:         str  = Field(..., min_length=5, max_length=20)
    items:         List[dict]  = Field(...)   # [{item, qty}]
    pickup_notes:  str  = Field("", max_length=200)

class MarketplaceItemUpdate(BaseModel):
    listed:       Optional[bool]  = None
    price_rm:     Optional[float] = Field(None, ge=0.10, le=9999)
    discount_pct: Optional[int]   = Field(None, ge=0, le=70)  # None = use global slider



def _validate_rest_id(restaurant_id: str) -> None:
    """Validate restaurant_id format to prevent path traversal and injection."""
    if not restaurant_id or not isinstance(restaurant_id, str):
        raise HTTPException(status_code=400, detail="restaurant_id is required.")
    if len(restaurant_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid restaurant_id.")
    import re as _re_v
    if not _re_v.match(r'^[a-zA-Z0-9_\-]+$', restaurant_id):
        raise HTTPException(status_code=400, detail="Invalid restaurant_id format.")


@app.get("/")
def root():
    bot_username = os.environ.get("BOT_USERNAME", "WasteWise_bot")
    return {"status": "ok", "service": "WasteWise AI", "version": "9.0.0",
            "bot_username": bot_username}


@app.get("/api/health")
def health_check():
    """
    Production health endpoint.
    Returns status of all subsystems: DB, cache, task queue, Supabase, push worker.
    """
    from services.cache_layer import cache_health
    from services.task_queue import queue_health
    from services.supabase_db import get_last_push_ts
    import time as _time_mod

    supabase_ok = False
    try:
        from services.supabase_db import _sb
        if _sb:
            _sb.table("restaurants").select("id").limit(1).execute()
            supabase_ok = True
    except Exception:
        pass

    db_ok = False
    try:
        db = load_database()
        db_ok = isinstance(db, dict)
    except Exception:
        pass

    last_push = get_last_push_ts()
    push_age  = round(_time_mod.monotonic() - last_push) if last_push else None

    return {
        "status":      "ok" if db_ok else "degraded",
        "version":     "9.0.0",
        "supabase":    "ok" if supabase_ok else "unavailable (JSON fallback active)",
        "cache":       cache_health(),
        "task_queue":  queue_health(),
        "database":    "ok" if db_ok else "error",
        "push_worker": {
            "last_push_seconds_ago": push_age,
            "status": "ok" if push_age is not None else "no_push_yet",
        },
    }


@app.get("/api/bot_info")
async def get_bot_info():
    """
    Return the Telegram bot username.
    Priority: 1) BOT_USERNAME in .env  2) live Telegram API call  3) safe default
    To change the bot name: edit BOT_USERNAME in backend/.env — no restart needed
    after the next request, because os.environ is read fresh each call.
    """
    # Check .env first — owner can set this without needing a Telegram API call
    env_username = os.environ.get("BOT_USERNAME", "").strip()
    if env_username:
        return {"bot_username": env_username, "source": "env"}

    # Fall back to asking Telegram directly
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    if tg_token:
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient() as c:
                r = await c.get(
                    f"https://api.telegram.org/bot{tg_token}/getMe", timeout=8
                )
                if r.status_code == 200:
                    username = r.json().get("result", {}).get("username", "")
                    if username:
                        return {"bot_username": username, "source": "telegram"}
        except Exception:
            pass

    return {"bot_username": "WasteWise_bot", "source": "default"}


@app.get("/api/restaurants")
def get_restaurants():
    db = load_database()
    return [{"id": r["id"], "name": r["name"], "region": r["region"]} for r in db.get("restaurants", [])]


@app.get("/api/dashboard/{restaurant_id}")
def get_dashboard(
    restaurant_id: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    require_restaurant_access(restaurant_id, user)
    db = load_database()                              # Single load
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    forecast_text = generate_morning_forecast(restaurant_id)  # Returns instantly (AI runs in background)
    return {
        "restaurant": {
            "id": restaurant["id"], "name": restaurant["name"],
            "region": restaurant["region"], "menu": restaurant.get("menu", []),
            "active_events": restaurant.get("active_events", []),
        },
        "region_info":         db.get("regions", {}).get(restaurant["region"], {}),
        "ai_forecast_message": forecast_text,
        "accuracy_data":       get_accuracy_data(restaurant_id),
    }


@app.post("/api/upload")
def upload_text(
    payload: UploadPayload,
    user: AuthenticatedUser = Depends(require_auth),
):
    require_restaurant_access(payload.restaurant_id, user)
    if not payload.action or not payload.action.strip():
        raise HTTPException(status_code=400, detail="No data provided.")
    if payload.menu_mode not in ("none", "append", "overwrite"):
        raise HTTPException(status_code=400, detail="menu_mode must be none/append/overwrite.")
    return {"status": "success", "message": process_ai_data_ingestion(payload.restaurant_id, payload.action, payload.menu_mode)}


@app.post("/api/upload_file")
async def upload_file(
    restaurant_id: str       = Form(...),
    menu_mode:     str       = Form("none"),
    file:          UploadFile = File(...),
    user:          AuthenticatedUser = Depends(require_auth),
):
    require_restaurant_access(restaurant_id, user)
    if menu_mode not in ("none", "append", "overwrite"):
        raise HTTPException(status_code=400, detail="Invalid menu_mode.")

    content = await file.read()
    if len(content) > 5_000_000:  # 5MB limit
        raise HTTPException(status_code=400, detail="File too large. Max 5MB.")
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    filename   = file.filename or "upload"
    text, fmt  = process_upload(filename, content)

    if fmt == "image":
        mime   = extract_image_mime(filename)
        result = process_image_upload(restaurant_id, content, mime)
        return {"status": "success", "message": f"📸 {result}"}

    if not text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from file.")

    result = process_ai_data_ingestion(restaurant_id, text, menu_mode)
    return {"status": "success", "message": f"📁 Processed '{filename}' ({fmt.upper()})\n{result}"}


@app.delete("/api/menu/{restaurant_id}/{item_name}")
def delete_menu_item(
    restaurant_id: str,
    item_name: str,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(require_auth),
):
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    original = len(restaurant.get("menu", []))
    restaurant["menu"] = [m for m in restaurant.get("menu", []) if m["item"].lower() != item_name.lower()]
    if len(restaurant["menu"]) == original:
        raise HTTPException(status_code=404, detail=f"Item not found on menu.")
    invalidate_forecast(restaurant_id); invalidate_intelligence(restaurant_id); invalidate_db()
    save_database(db)
    background_tasks.add_task(_do_generate_forecast, restaurant_id)
    return {"status": "success", "message": f"'{item_name}' deleted. Forecast regenerating."}


@app.post("/api/menu/{restaurant_id}")
def add_menu_item(
    restaurant_id: str,
    payload: MenuItemPayload,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(require_auth),
):
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    existing = {m["item"].lower() for m in restaurant.get("menu", [])}
    if payload.item.lower() in existing:
        raise HTTPException(status_code=400, detail=f"'{payload.item}' already exists.")
    restaurant.setdefault("menu", []).append({
        "item":              validate_item_name(payload.item),
        "base_daily_demand": payload.base_daily_demand,
        "profit_margin_rm":  payload.profit_margin_rm,
        "price_rm":          payload.price_rm,
        "halal_certified":   payload.halal_certified,
        "allergens":         payload.allergens,
        "description":       sanitise_text(payload.description, 300),
    })
    invalidate_forecast(restaurant_id); invalidate_intelligence(restaurant_id); invalidate_db()
    save_database(db)
    background_tasks.add_task(_do_generate_forecast, restaurant_id)
    return {"status": "success", "message": f"'{payload.item}' added. Forecast regenerating."}


@app.post("/api/event/{restaurant_id}")
def add_event(
    restaurant_id: str,
    payload: EventPayload,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(require_auth),
):
    require_restaurant_access(restaurant_id, user)
    message = register_owner_event(restaurant_id, sanitise_text(payload.description, 200), payload.headcount, payload.days)
    invalidate_forecast(restaurant_id)
    background_tasks.add_task(_do_generate_forecast, restaurant_id)
    return {"status": "success", "message": message}


@app.get("/api/accuracy/{restaurant_id}")
def get_accuracy(
    restaurant_id: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    return {"accuracy_data": get_accuracy_data(restaurant_id)}


@app.get("/api/shopping_list/{restaurant_id}")
def get_shopping_list_endpoint(
    restaurant_id: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Today's ingredient shopping list based on forecast quantities."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    items = get_shopping_list(restaurant_id)
    return {"shopping_list": items, "date": _dt_mod.date.today().isoformat()}


@app.post("/api/bom/{restaurant_id}/{item_name}")
def set_item_bom(
    restaurant_id: str,
    item_name: str,
    bom: dict,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Owner defines ingredient ratios for a menu item."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    restaurant.setdefault("bom", {})[item_name] = bom
    invalidate_forecast(restaurant_id)
    save_database(db)
    background_tasks.add_task(_do_generate_forecast, restaurant_id)
    return {"status": "success", "item": item_name, "bom": bom}


@app.get("/api/bom/{restaurant_id}")
def get_bom(
    restaurant_id: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Return all owner-defined BOM ratios for a restaurant."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    return {"bom": restaurant.get("bom", {})}


@app.get("/api/accuracy_notes/{restaurant_id}")
def get_accuracy_notes(
    restaurant_id: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Return plain-English actionable notes for low-accuracy items."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    from services.data_miner import compute_mape_per_item, actionable_accuracy_notes
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    mape_data = compute_mape_per_item(restaurant)
    notes     = actionable_accuracy_notes(mape_data, restaurant)
    return {"notes": notes}


@app.post("/api/auth/register")
async def api_register(
    email:             Optional[str] = None,
    name:              Optional[str] = None,
    owner_name:        Optional[str] = None,
    region:            Optional[str] = None,
    restaurant_type:   Optional[str] = None,
    telegram_username: Optional[str] = None,
    closing_time:      str = "21:00",
):
    import uuid as _uuid, datetime as _dt
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required.")
    if not name or not region:
        raise HTTPException(status_code=400, detail="Restaurant name and region required.")
    if not telegram_username:
        raise HTTPException(status_code=400, detail="Telegram username required.")
    tg_username = telegram_username.lstrip("@").lower()
    if auth.email_registered(email):
        raise HTTPException(status_code=409, detail="This email is already registered. Please sign in instead.")
    if auth.pending_email_registered(email):
        raise HTTPException(status_code=409, detail="A registration for this email is already in progress. Check your Telegram to complete it.")
    db = load_database()
    chat_id = None
    for r in db.get("restaurants", []):
        if r.get("telegram_username","").lower() == tg_username and r.get("telegram_chat_id"):
            chat_id = r["telegram_chat_id"]
            break
    if not chat_id:
        for acc in db.get("accounts", []):
            for s in acc.get("sessions", []):
                if s.get("telegram_username","").lower() == tg_username and s.get("chat_id"):
                    chat_id = s["chat_id"]
                    break
    if chat_id and auth.telegram_registered(chat_id):
        raise HTTPException(status_code=409, detail="This Telegram account is already linked to a restaurant.")
    import re as _re_ct
    ct = closing_time if _re_ct.match(r'^\d{2}:\d{2}$', closing_time or "") else "21:00"
    rest_id  = "rest_" + str(_uuid.uuid4())[:8]
    new_rest = {
        "id": rest_id, "name": name.strip(), "region": region.strip(),
        "type": (restaurant_type or "hawker").strip(),
        "owner_name": (owner_name or "Owner").strip(),
        "telegram_chat_id": chat_id,
        "telegram_username": tg_username,
        "privacy_accepted": True,
        "registered_at": _dt.datetime.now().isoformat(),
        "specialty_weather": "neutral", "bom": {}, "menu": [],
        "recent_feedback_memory": [], "active_events": [], "daily_records": [],
        "closing_time": ct, "discount_pct": 30, "marketplace_enabled": True,
    }
    if chat_id:
        db.setdefault("restaurants", []).append(new_rest)
        if region not in db.get("regions", {}):
            db.setdefault("regions", {})[region] = {"type":"General Area","foot_traffic_baseline":500,
                "weekend_multiplier":1.1,"holiday_multiplier":1.0,"rain_impact":-0.2}
        save_database(db)
        auth.create_account(email, rest_id, chat_id, tg_username)
        token = auth.add_web_session(email, "Web dashboard")
        return {"status": "registered", "restaurant_id": rest_id, "email": email, "token": token}
    else:
        auth.create_pending_registration(email, tg_username,
            {"rest_id": rest_id, "new_rest": new_rest, "region": region})
        return {"status": "pending_telegram",
                "message": "Please open Telegram and send any message to the WasteWise AI bot to complete registration.",
                "email": email}


@app.post("/api/auth/request_otp")
@limiter.limit("5/minute")
async def api_request_otp(request: Request, email: Optional[str] = None):
    client_ip = get_client_ip(request)
    check_otp_rate_limit(client_ip)   # brute-force guard

    email = validate_email(email or "")
    account = auth.get_account_by_email(email)
    if not account:
        raise HTTPException(status_code=404, detail="Email not registered. Please create an account.")
    primary = next((s for s in account.get("sessions",[]) if s.get("is_primary") and s.get("chat_id")), None)
    if not primary:
        raise HTTPException(status_code=400, detail="No Telegram account linked. Please verify your account on Telegram.")
    otp      = auth.create_otp(email, "web_login")
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    if tg_token:
        import httpx as _httpx
        msg = ("\U0001f510 Your WasteWise AI login code: *" + otp + "*\n\n"
               "Valid for " + str(auth.OTP_TTL_SECONDS) + " seconds. Do not share this.")
        try:
            async with _httpx.AsyncClient() as _c:
                await _c.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": primary["chat_id"], "text": msg, "parse_mode": "Markdown"},
                    timeout=10,
                )
        except Exception:
            pass   # Don't expose internal errors
    # NEVER return the OTP or chat_id to the browser
    return {"status": "otp_sent", "expires_in": auth.OTP_TTL_SECONDS}


@app.post("/api/auth/verify_otp")
@limiter.limit("10/minute")
def api_verify_otp(request: Request, email: Optional[str] = None, otp: Optional[str] = None):
    client_ip = get_client_ip(request)
    check_otp_rate_limit(client_ip)

    if not email or not otp:
        raise HTTPException(status_code=400, detail="email and otp required.")
    email = validate_email(email)
    otp   = validate_otp_code(otp)

    if not auth.verify_otp(email, otp, "web_login"):
        record_failed_otp(client_ip)
        raise HTTPException(status_code=401, detail="Invalid or expired OTP.")

    clear_otp_attempts(client_ip)
    account = auth.get_account_by_email(email)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")
    token = auth.add_web_session(email, "Web dashboard")
    return {"status": "success", "token": token, "restaurant_id": account["restaurant_id"], "email": email}


@app.post("/api/auth/verify_telegram_otp")
def api_verify_telegram_otp(chat_id: int = None, otp: str = None):
    """Verify OTP sent to Telegram (used during web registration to confirm Telegram username)."""
    if not chat_id or not otp:
        raise HTTPException(status_code=400, detail="chat_id and otp required.")
    if not auth.verify_telegram_otp(chat_id, otp, "web_register_verify"):
        raise HTTPException(status_code=401, detail="Invalid or expired OTP.")
    return {"status": "verified", "chat_id": chat_id}


@app.get("/api/auth/me")
def api_me(request: Request):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required.")
    info = auth.validate_web_token(token)
    if not info:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    account = auth.get_account_by_email(info["email"])
    sessions = auth.get_sessions_for_account(info["email"])
    return {
        "email":         info["email"],
        "restaurant_id": info["restaurant_id"],
        "sessions":      [
            {"session_id": s["session_id"][:8], "type": s.get("type"),
             "label": s.get("telegram_username") or s.get("label",""),
             "is_primary": s.get("is_primary"), "expires_at": s.get("expires_at")}
            for s in sessions
        ],
    }


@app.delete("/api/auth/session/{session_prefix}")
def api_remove_session(session_prefix: str, request: Request):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required.")
    info = auth.validate_web_token(token)
    if not info:
        raise HTTPException(status_code=401, detail="Session expired.")
    sessions = auth.get_sessions_for_account(info["email"])
    target   = next((s for s in sessions if s["session_id"].startswith(session_prefix)), None)
    if not target:
        raise HTTPException(status_code=404, detail="Session not found.")
    if target.get("is_primary"):
        raise HTTPException(status_code=400, detail="Cannot remove the primary session.")
    removed = auth.remove_session(info["email"], target["session_id"])
    return {"status": "removed" if removed else "not_found"}


@app.patch("/api/auth/session/{session_prefix}/make_primary")
def api_make_session_primary(session_prefix: str, request: Request):
    """
    Transfer primary status to a different already-linked Telegram session.

    Rules enforced:
    - Caller must be authenticated (valid Bearer token)
    - Caller's current token must belong to the PRIMARY Telegram session
      (only the primary can transfer primacy — prevents hostile takeover)
    - Target session must be of type 'telegram' (not a web browser session)
    - Exactly ONE primary exists at all times — the old primary is demoted
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required.")
    info = auth.validate_web_token(token)
    if not info:
        raise HTTPException(status_code=401, detail="Session expired.")

    db = load_database()
    account = next((a for a in db.get("accounts", []) if a["email"].lower() == info["email"].lower()), None)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")

    sessions = account.get("sessions", [])

    # Verify the caller's own session is the current primary Telegram session
    caller_session = next((s for s in sessions if s["session_id"] == token), None)
    if not caller_session or not caller_session.get("is_primary"):
        raise HTTPException(
            status_code=403,
            detail="Only the primary Telegram account can transfer primary status."
        )

    # Find target session to promote
    target = next((s for s in sessions if s["session_id"].startswith(session_prefix)), None)
    if not target:
        raise HTTPException(status_code=404, detail="Session not found.")
    if target.get("session_id") == caller_session.get("session_id"):
        raise HTTPException(status_code=400, detail="This session is already primary.")
    if target.get("type") != "telegram":
        raise HTTPException(status_code=400, detail="Only Telegram sessions can be made primary.")

    # Transfer: demote all, promote target
    for s in sessions:
        s["is_primary"] = (s["session_id"] == target["session_id"])

    save_database(db)
    return {
        "status": "primary_transferred",
        "new_primary_session": target["session_id"][:8],
        "new_primary_telegram": target.get("telegram_username", ""),
    }


@app.delete("/api/auth/account")
def api_delete_account(request: Request, user: AuthenticatedUser = Depends(require_auth)):
    account  = auth.get_account_by_email(user.email)
    rest_id  = account.get("restaurant_id") if account else None
    auth.delete_account(user.email)
    if rest_id:
        db = load_database()
        db["restaurants"] = [r for r in db.get("restaurants",[]) if r["id"] != rest_id]
        save_database(db)
    return {"status": "deleted"}


# =============================================================================
# DELETE RESTAURANT  (with chain support + keep-data option)
# =============================================================================

class DeleteRestaurantPayload(BaseModel):
    keep_data: bool = Field(
        True,
        description=(
            "True = anonymise and keep sales data to help improve WasteWise AI "
            "(recommended — your data is safe and helps other hawkers). "
            "False = permanently delete everything."
        ),
    )
    delete_entire_chain: bool = Field(
        False,
        description="If restaurant is part of a chain, set True to delete ALL branches.",
    )


@app.delete("/api/restaurant/{restaurant_id}")
def delete_restaurant(
    restaurant_id: str,
    payload: DeleteRestaurantPayload,
    user: AuthenticatedUser = Depends(require_auth),
):
    """
    Delete a restaurant with two options:
      keep_data=True  → anonymise all sales data, remove PII, keep for AI training
      keep_data=False → hard delete everything

    Chain support:
      delete_entire_chain=False → remove only this restaurant from chain
      delete_entire_chain=True  → remove all restaurants in the same chain
    """
    require_restaurant_access(restaurant_id, user)
    
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")

    chain_id = restaurant.get("chain_id")

    # Determine which restaurant IDs to delete
    if chain_id and payload.delete_entire_chain:
        # Delete all restaurants in this chain
        target_ids = [r["id"] for r in db.get("restaurants", []) if r.get("chain_id") == chain_id]
        # Verify user owns the chain (owns at least one branch)
        owned = any(r["id"] == restaurant_id for r in db.get("restaurants", []) if r.get("chain_id") == chain_id)
        if not owned:
            raise HTTPException(status_code=403, detail="Access denied.")
        # Remove chain record
        db["chains"] = [c for c in db.get("chains", []) if c.get("chain_id") != chain_id]
        # Remove from Supabase chains table
        try:
            from services.supabase_db import _sb
            if _sb:
                _sb.table("chains").delete().eq("chain_id", chain_id).execute()
        except Exception:
            pass
    else:
        target_ids = [restaurant_id]
        # If only removing one branch from chain, just unlink it
        if chain_id and not payload.delete_entire_chain:
            for r in db.get("restaurants", []):
                if r["id"] == restaurant_id:
                    r["chain_id"] = None
                    break

    deleted_names = []
    for rid in target_ids:
        rest = _get_restaurant(db, rid)
        if not rest:
            continue
        deleted_names.append(rest.get("name", rid))

        if payload.keep_data:
            # ── ANONYMISE: remove all PII, keep sales history for AI ──────
            rest["name"]               = f"[Anonymised Stall {rid[-4:]}]"
            rest["owner_name"]         = "[Anonymised]"
            rest["telegram_chat_id"]   = None
            rest["telegram_username"]  = None
            rest["email"]              = None
            rest["bom"]                = {}
            rest["recent_feedback_memory"] = []
            rest["closing_stock"]      = []
            rest["marketplace_orders"] = []
            rest["active_events"]      = []
            
            rest["_anonymised"]        = True
            rest["_anonymised_at"]     = _dt_mod.datetime.now(_dt_mod.timezone.utc).isoformat()
            # Remove from Supabase: account + sessions only; keep daily_records
            try:
                from services.supabase_db import _sb
                if _sb:
                    _sb.table("restaurants").update({
                        "name": rest["name"], "owner_name": "[Anonymised]",
                        "telegram_chat_id": None, "telegram_username": None, "email": None,
                        "bom": {}, "recent_feedback_memory": [], 
                    }).eq("id", rid).execute()
            except Exception:
                pass
        else:
            # ── HARD DELETE: remove everything ────────────────────────────
            db["restaurants"] = [r for r in db.get("restaurants", []) if r["id"] != rid]
            try:
                from services.supabase_db import _sb
                if _sb:
                    # Cascade deletes handle child tables (ON DELETE CASCADE in schema)
                    _sb.table("restaurants").delete().eq("id", rid).execute()
            except Exception:
                pass

        # Delete the account regardless of keep_data choice
        account = auth.get_account_by_restaurant(rid)
        if account:
            auth.delete_account(account["email"])
            try:
                from services.supabase_db import _sb
                if _sb:
                    _sb.table("accounts").delete().eq("restaurant_id", rid).execute()
            except Exception:
                pass

    invalidate_db()
    save_database(db)

    if payload.keep_data:
        message = (
            f"✅ '{', '.join(deleted_names)}' removed. Your historical sales data has been "
            f"anonymised and kept to help improve forecasts for other hawkers — "
            f"no personal info is retained. Thank you! 🌿"
        )
    else:
        message = f"✅ '{', '.join(deleted_names)}' and all associated data permanently deleted."

    return {
        "status": "deleted",
        "keep_data": payload.keep_data,
        "chain_deleted": payload.delete_entire_chain and bool(chain_id),
        "restaurants_deleted": len(target_ids),
        "message": message,
    }


@app.get("/api/restaurant/{restaurant_id}/chain_info")
def get_chain_info(
    restaurant_id: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    """
    Returns chain info for a restaurant so the frontend can show
    the correct delete options (single vs entire chain).
    """
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")

    chain_id = restaurant.get("chain_id")
    if not chain_id:
        return {"is_chain": False, "chain_id": None, "branch_count": 1, "branches": []}

    chain = next((c for c in db.get("chains", []) if c.get("chain_id") == chain_id), {})
    branches = [
        {"id": r["id"], "name": r["name"], "region": r.get("region", "")}
        for r in db.get("restaurants", []) if r.get("chain_id") == chain_id
    ]
    return {
        "is_chain": True,
        "chain_id": chain_id,
        "chain_name": chain.get("name", ""),
        "branch_count": len(branches),
        "branches": branches,
    }


@app.post("/api/bom/{restaurant_id}/generate")
def api_generate_bom(
    restaurant_id: str,
    item_name: Optional[str] = None,
    owner_hint: str = "",
    user: AuthenticatedUser = Depends(require_auth),
):
    """Generate AI-powered BOM. If owner_hint is empty or 'don't know', uses full AI generation."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    if not item_name:
        raise HTTPException(status_code=400, detail="item_name required.")
    region    = restaurant.get("region", "Malaysia")
    rest_type = restaurant.get("type", "hawker")
    bom       = ask_bom_conversational(item_name, region, rest_type, owner_hint)
    if not bom:
        raise HTTPException(status_code=500, detail="AI could not generate BOM.")
    restaurant.setdefault("bom", {})[item_name] = bom
    save_database(db)
    return {"status": "saved", "item": item_name, "bom": bom}




@app.get("/api/auth/check_pending")
def check_pending(email: Optional[str] = None):
    """
    Poll endpoint: called by frontend after a pending registration to see if
    the user has completed Telegram verification.
    Security: only issues a token if there is an ACTIVE pending registration for
    this email (created within the last 10 minutes). A plain 'account exists'
    check would let any attacker obtain a session token for any registered email
    without ever going through OTP.
    """
    if not email:
        raise HTTPException(status_code=400, detail="email required.")

    import datetime as _dt_check
    db  = load_database()
    now = _dt_check.datetime.utcnow()

    # Only proceed if there is an unexpired pending registration for this email
    pending = next(
        (
            p for p in db.get("pending_registrations", [])
            if p.get("email", "").lower() == email.lower()
            and _dt_check.datetime.fromisoformat(p["expires_at"]) > now
        ),
        None,
    )
    if not pending:
        # No active pending registration — return generic response to prevent enumeration
        return {"status": "pending"}

    account = auth.get_account_by_email(email)
    if account:
        # Telegram bot completed the registration — safe to issue session
        token = auth.add_web_session(email, "Web dashboard")
        return {"status": "completed", "token": token,
                "restaurant_id": account["restaurant_id"], "email": email}
    return {"status": "pending"}


@app.get("/api/intelligence/{restaurant_id}")
def get_intelligence(restaurant_id: str, user: AuthenticatedUser = Depends(require_auth)):
    """Full ML intelligence: trends, correlations, MAPE, waste metrics, data quality."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    from services.data_miner import (
        compute_item_trends_with_tuning, compute_item_correlations,
        compute_mape_per_item, calculate_waste_metrics,
        compute_data_quality_score, mine_ecosystem_signals,
        compute_learned_multipliers, ensemble_forecast,
    )
    import datetime
    today_wd    = datetime.date.today().strftime("%A")
    item_trends = compute_item_trends_with_tuning(restaurant, today_wd)
    correlations = compute_item_correlations(restaurant)
    mape_data    = compute_mape_per_item(restaurant)
    waste        = calculate_waste_metrics(restaurant, item_trends)
    dq_score     = compute_data_quality_score(restaurant)
    eco_signals  = mine_ecosystem_signals(db.get("restaurants", []), db.get("regions", {}))
    learned      = compute_learned_multipliers(restaurant, db.get("global_learning_events", []))

    trends_out = {}
    for name, t in item_trends.items():
        trends_out[name] = {
            "trend_dir": t.trend_dir, "trend_pct": t.trend_pct,
            "recommended_qty": t.recommended_qty, "confidence": t.confidence,
            "holt_forecast": t.holt_forecast, "ewma": t.ewma,
            "velocity_dir": t.velocity_dir, "has_anomaly": t.has_anomaly,
            "anomaly_note": t.anomaly_note,
        }
    return {
        "item_trends":     trends_out,
        "correlations":    [{"item_a":a,"item_b":b,"r":r,"description":d} for a,b,r,d in correlations],
        "mape_per_item":   mape_data,
        "waste_metrics":   waste,
        "data_quality":    dq_score,
        "ecosystem_signals": [{"pattern":s.item_pattern,"direction":s.direction,
                                "count":s.restaurant_count,"avg_shift_pct":s.avg_shift_pct,
                                "strength":s.signal_strength} for s in eco_signals[:5]],
        "learned_weekday_multipliers": learned.weekday,
    }


# ── Closing Time & Store Settings ─────────────────────────────────────────────

@app.post("/api/restaurant/{restaurant_id}/closing_time")
def set_closing_time(restaurant_id: str, payload: ClosingTimePayload,
                     user: AuthenticatedUser = Depends(require_auth)):
    """Set the restaurant closing time, discount %, and marketplace toggle."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    restaurant["closing_time"]        = payload.closing_time
    restaurant["discount_pct"]        = payload.discount_pct
    restaurant["marketplace_enabled"] = payload.marketplace_enabled
    save_database(db)
    return {
        "status": "saved",
        "closing_time": payload.closing_time,
        "discount_pct": payload.discount_pct,
        "marketplace_enabled": payload.marketplace_enabled,
    }


@app.get("/api/restaurant/{restaurant_id}/closing_time")
def get_closing_time(restaurant_id: str,
                     user: AuthenticatedUser = Depends(require_auth)):
    """Get the restaurant closing time settings."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    return {
        "closing_time":        restaurant.get("closing_time", ""),
        "discount_pct":        restaurant.get("discount_pct", 30),
        "marketplace_enabled": restaurant.get("marketplace_enabled", True),
    }


# ── Inventory ──────────────────────────────────────────────────────────────────

@app.get("/api/restaurant/{restaurant_id}/inventory")
def get_inventory(restaurant_id: str,
                  user: AuthenticatedUser = Depends(require_auth)):
    """Get remaining inventory for today."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    remaining = compute_remaining_inventory(restaurant)
    return {
        "date": _dt_mod.date.today().isoformat(),
        "inventory": remaining,
        "closing_time": restaurant.get("closing_time", ""),
        "discount_pct": restaurant.get("discount_pct", 30),
    }


# ── Profit Dashboard ───────────────────────────────────────────────────────────

@app.get("/api/dashboard/{restaurant_id}/profit")
def get_profit_data(restaurant_id: str, user: AuthenticatedUser = Depends(require_auth)):
    """Get today's profit summary and last 7 days of profit data."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    today_summary = get_today_profit_summary(restaurant)
    weekly_data   = get_weekly_profit_data(restaurant)
    return {
        "today": today_summary,
        "weekly": weekly_data,
    }


# ── Customer Marketplace ───────────────────────────────────────────────────────

@app.get("/api/customer/store/{restaurant_id}")
def get_customer_store(restaurant_id: str):
    """Public store page — no auth needed. Returns discounted closing stock."""
    _validate_rest_id(restaurant_id)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Store not found.")
    if not restaurant.get("marketplace_enabled", True):
        raise HTTPException(status_code=404, detail="Marketplace not enabled for this store.")

    today_str = _dt_mod.date.today().isoformat()
    closing_stock = []
    if restaurant.get("closing_stock_date") == today_str:
        closing_stock = restaurant.get("closing_stock", [])

    # Count existing orders for each item today to show remaining qty
    today_orders = [
        o for o in restaurant.get("marketplace_orders", [])
        if o.get("date") == today_str and o.get("status") != "cancelled"
    ]
    ordered_qtys: dict = {}
    for order in today_orders:
        for oi in order.get("items", []):
            key = oi.get("item", "").lower()
            ordered_qtys[key] = ordered_qtys.get(key, 0) + oi.get("qty", 0)

    # Adjust available qty
    available_stock = []
    for cs in closing_stock:
        already_ordered = ordered_qtys.get(cs["item"].lower(), 0)
        avail = max(0, cs["qty_available"] - already_ordered)
        if avail > 0:
            available_stock.append({**cs, "qty_available": avail})

    return {
        "restaurant_id": restaurant_id,
        "restaurant_name": restaurant["name"],
        "region": restaurant["region"],
        "closing_time": restaurant.get("closing_time", ""),
        "discount_pct": restaurant.get("discount_pct", 30),
        "closing_stock": available_stock,
        "has_stock": len(available_stock) > 0,
        "marketplace_active": restaurant.get("closing_stock_date") == today_str,
        "total_orders_today": len(today_orders),
    }


@app.post("/api/customer/order")
def place_customer_order(payload: CustomerOrderPayload):
    """Customer places an order for closing-time discounted items."""
    _validate_rest_id(payload.restaurant_id)
    db = load_database()
    restaurant = _get_restaurant(db, payload.restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Store not found.")
    if not restaurant.get("marketplace_enabled", True):
        raise HTTPException(status_code=400, detail="Marketplace not enabled.")

    today_str = _dt_mod.date.today().isoformat()
    remaining = compute_remaining_inventory(restaurant)
    remaining_map = {r["item"].lower(): r["remaining"] for r in remaining}

    order_items = []
    total_rm = 0.0
    listings = restaurant.get("marketplace_listings", {})
    for oi in payload.items:
        item_name = oi.get("item", "")
        qty       = int(oi.get("qty", 1))
        if qty < 1:
            raise HTTPException(status_code=400, detail=f"Invalid quantity for {item_name}.")
        # Find menu item
        menu_item = next((m for m in restaurant.get("menu",[]) if m["item"].lower()==item_name.lower()), None)
        if not menu_item:
            raise HTTPException(status_code=400, detail=f"{item_name} is not available.")
        cfg = listings.get(item_name, listings.get(item_name.lower(), {}))
        if not cfg.get("listed", True):
            raise HTTPException(status_code=400, detail=f"{item_name} is not listed on marketplace.")
        avail = remaining_map.get(item_name.lower(), 0)
        if avail < qty:
            raise HTTPException(status_code=400, detail=f"Only {avail} portions of {item_name} available.")
        base_price = cfg.get("price_rm") or menu_item.get("profit_margin_rm", 3.0)
        closing_time = restaurant.get("closing_time", "")
        global_disc  = restaurant.get("discount_pct", 30)
        dyn          = get_dynamic_discount(closing_time, global_disc)
        item_disc = cfg.get("discount_pct")
        disc_pct  = item_disc if item_disc is not None else dyn["discount_pct"]
        unit_price = round(base_price * (1 - disc_pct/100), 2) if disc_pct > 0 else round(base_price, 2)
        line_total = round(unit_price * qty, 2)
        total_rm  += line_total
        order_items.append({
            "item":           item_name,
            "qty":            qty,
            "unit_price_rm":  unit_price,
            "line_total_rm":  line_total,
        })

    total_rm = round(total_rm, 2)
    profit_split = compute_profit_split(total_rm)

    order_id = "ord_" + str(_uuid_mod.uuid4())[:8]
    created_now      = _dt_mod.datetime.now()
    pickup_deadline  = (created_now + _dt_mod.timedelta(minutes=45)).isoformat()

    # Daily sequential order number (resets at midnight)
    today_orders_so_far = [o for o in restaurant.get("marketplace_orders", []) if o.get("date") == today_str]
    order_num = len(today_orders_so_far) + 1

    order = {
        "order_id":               order_id,
        "order_num":              order_num,
        "date":                   today_str,
        "created_at":             created_now.isoformat(),
        "pickup_deadline":        pickup_deadline,
        "reminder_sent":          False,
        "customer_name":          payload.customer_name,
        "phone":                  payload.phone,
        "items":                  order_items,
        "total_rm":               total_rm,
        "shopkeeper_earnings_rm": profit_split["shopkeeper"],
        "platform_fee_rm":        profit_split["platform"],
        "pickup_notes":           payload.pickup_notes,
        "status":                 "pending",
    }

    restaurant.setdefault("marketplace_orders", []).append(order)
    save_database(db)

    # Notify shopkeeper via Telegram
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id  = restaurant.get("telegram_chat_id")
    if tg_token and chat_id:
        import httpx as _httpx
        items_str    = ", ".join(f"{oi['qty']}x {oi['item']}" for oi in order_items)
        deadline_dt  = _dt_mod.datetime.fromisoformat(pickup_deadline)
        deadline_fmt = deadline_dt.strftime("%I:%M %p")
        msg = (
            f"\U0001F6D2 *Order #{order_num} — {restaurant['name']}!*\n\n"
            f"\U0001F464 *Customer:* {payload.customer_name}\n"
            f"\U0001F4F1 *Phone:* {payload.phone}\n\n"
            f"\U0001F37D\uFE0F *Items:* {items_str}\n"
            f"\U0001F4B0 *Total:* RM {total_rm:.2f}\n"
            f"\U0001F4DD *Notes:* {payload.pickup_notes or 'None'}\n\n"
            f"\u23F0 *Pickup by:* {deadline_fmt} (within 45 min)\n\n"
            f"\U0001F194 Order: `{order_id}`\n\n"
            f"\u2705 Reply `done {order_num}` when customer collects\n"
            f"\u274C Reply `miss {order_num}` if not picked up"
        )
        try:
            import asyncio as _aio
            async def _notify():
                async with _httpx.AsyncClient() as c:
                    await c.post(
                        f"https://api.telegram.org/bot{tg_token}/sendMessage",
                        json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                        timeout=8,
                    )
            threading.Thread(
                target=lambda: _aio.run(_notify()), daemon=True
            ).start()
        except Exception as e:
            print(f"[Order] Telegram notify error: {e}")

    return {
        "status": "success",
        "order_id": order_id,
        "order_num": order_num,
        "total_rm": total_rm,
        "items": order_items,
        "message": f"Order #{order_num} placed! Pick up at {restaurant['name']} before closing ({restaurant.get('closing_time','')}).",
    }


@app.get("/api/restaurant/{restaurant_id}/orders")
def get_orders(restaurant_id: str,
               user: AuthenticatedUser = Depends(require_auth)):
    """Get today's marketplace orders for the shopkeeper dashboard."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    today_str = _dt_mod.date.today().isoformat()
    today_orders = [
        o for o in restaurant.get("marketplace_orders", [])
        if o.get("date") == today_str
    ]
    total_revenue = sum(o["total_rm"] for o in today_orders if o.get("status") != "cancelled")
    return {
        "orders": today_orders,
        "total_orders": len(today_orders),
        "total_revenue_rm": round(total_revenue, 2),
        "platform_fee_rm": round(total_revenue * 0.10, 2),
        "shopkeeper_earnings_rm": round(total_revenue * 0.90, 2),
    }


@app.patch("/api/restaurant/{restaurant_id}/orders/{order_ref}")
def update_order_status(
    restaurant_id: str, order_ref: str, status: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Update order status: pending → completed or cancelled. order_ref can be order_id or today's order_num."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    if status not in ("pending", "completed", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status.")
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    today_str = _dt_mod.date.today().isoformat()
    for order in restaurant.get("marketplace_orders", []):
        match_by_id  = order.get("order_id") == order_ref
        match_by_num = (order.get("date") == today_str and str(order.get("order_num", "")) == str(order_ref))
        if match_by_id or match_by_num:
            order["status"] = status
            save_database(db)
            return {"status": "updated", "order_id": order.get("order_id"), "order_num": order.get("order_num"), "new_status": status}
    raise HTTPException(status_code=404, detail="Order not found.")


@app.post("/api/restaurant/{restaurant_id}/trigger_closing")
def trigger_closing_manually(
    restaurant_id: str, background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Manually trigger the closing-time inventory snapshot (for testing)."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")

    remaining = compute_remaining_inventory(restaurant)
    discount_pct = restaurant.get("discount_pct", 30)
    today_str = _dt_mod.date.today().isoformat()

    closing_stock = []
    for item in remaining:
        if item["remaining"] > 0:
            original_price = item["profit_margin_rm"]
            discounted = round(original_price * (1 - discount_pct / 100), 2)
            closing_stock.append({
                "item": item["item"],
                "qty_available": item["remaining"],
                "original_price_rm": original_price,
                "discounted_price_rm": discounted,
                "discount_pct": discount_pct,
            })

    restaurant["closing_stock"] = closing_stock
    restaurant["closing_stock_date"] = today_str
    restaurant["closing_stock_time"] = _dt_mod.datetime.now().strftime("%H:%M")
    save_database(db)

    msg = build_closing_time_telegram_message(restaurant, remaining)
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id  = restaurant.get("telegram_chat_id")
    if tg_token and chat_id:
        import httpx as _httpx
        async def _send():
            async with _httpx.AsyncClient() as c:
                await c.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                    timeout=8,
                )
        import asyncio as _aio
        threading.Thread(target=lambda: _aio.run(_send()), daemon=True).start()

    return {
        "status": "triggered",
        "closing_stock": closing_stock,
        "message_preview": msg[:300] + "...",
    }


# ── Per-item Marketplace Listing Management ────────────────────────────────────

@app.get("/api/restaurant/{restaurant_id}/marketplace_listings")
def get_marketplace_listings_api(
    restaurant_id: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Get all menu items with their per-item marketplace listing settings."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    return {
        "listings":            get_marketplace_listings(restaurant),
        "global_discount_pct": restaurant.get("discount_pct", 30),
        "closing_time":        restaurant.get("closing_time", ""),
        "marketplace_enabled": restaurant.get("marketplace_enabled", True),
    }


@app.patch("/api/restaurant/{restaurant_id}/marketplace_listings/{item_name}")
def update_marketplace_listing(
    restaurant_id: str,
    item_name: str,
    payload: MarketplaceItemUpdate,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Toggle listing, override price, or override discount for a single item."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")

    listings = restaurant.setdefault("marketplace_listings", {})
    cfg = listings.get(item_name, {})

    update_data = payload.dict(exclude_unset=True)
    for key, val in update_data.items():
        cfg[key] = val          # None values explicitly clear the override

    listings[item_name] = cfg
    save_database(db)
    return {"status": "updated", "item": item_name, "settings": cfg}


@app.post("/api/restaurant/{restaurant_id}/marketplace_listings/{item_name}/photo")
@limiter.limit("10/minute")
async def upload_item_photo(
    request: Request,
    restaurant_id: str,
    item_name: str,
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(require_auth),
):
    """Upload a photo for a marketplace item (stored as base64 data URI)."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")

    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are accepted.")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large — max 5 MB.")

    import base64 as _b64
    b64_str = f"data:{content_type};base64,{_b64.b64encode(data).decode()}"

    listings = restaurant.setdefault("marketplace_listings", {})
    cfg = listings.get(item_name, {})
    cfg["photo_b64"] = b64_str
    listings[item_name] = cfg
    save_database(db)
    return {"status": "uploaded", "item": item_name}


@app.post("/api/restaurant/{restaurant_id}/ai_discount_optimize")
def ai_discount_optimize(
    restaurant_id: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    """
    AI analyses remaining inventory and sets smart per-item discounts.
    Also raises global discount if most items are surplus near closing.
    """
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")

    result = ai_optimize_discounts(restaurant)

    listings = restaurant.setdefault("marketplace_listings", {})
    for item_name, cfg in result["changes"].items():
        listings[item_name] = cfg

    if result.get("global_change") is not None:
        restaurant["discount_pct"] = result["global_change"]

    save_database(db)
    return {
        "status":        "optimized",
        "changes_made":  len(result["changes"]),
        "global_change": result.get("global_change"),
        "actions":       result["actions"],
    }


# ── Marketplace (multi-restaurant) ────────────────────────────────────────────

@app.get("/api/marketplace")
def get_marketplace():
    """
    List ALL marketplace-enabled restaurants with dynamic pricing.
    Sorted by urgency (closing soon = best deals first).
    """
    db = load_database()
    restaurants = get_all_marketplace_restaurants(db)
    return {
        "restaurants": restaurants,
        "total": len(restaurants),
        "generated_at": _dt_mod.datetime.now().isoformat(),
    }


@app.get("/api/marketplace/{restaurant_id}")
def get_marketplace_restaurant(restaurant_id: str):
    """
    Get a single restaurant's marketplace data with dynamic pricing.
    Public — no auth required.
    """
    _validate_rest_id(restaurant_id)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    if not restaurant.get("marketplace_enabled", True):
        raise HTTPException(status_code=404, detail="Marketplace not enabled for this store.")

    closing_time = restaurant.get("closing_time", "")
    dyn = get_dynamic_discount(closing_time, restaurant.get("discount_pct", 30))
    menu = get_marketplace_menu(restaurant)

    today_str = _dt_mod.date.today().isoformat()
    today_orders = [
        o for o in restaurant.get("marketplace_orders", [])
        if o.get("date") == today_str and o.get("status") != "cancelled"
    ]
    # Subtract already-ordered qtys from closing stock
    if restaurant.get("closing_stock_date") == today_str:
        ordered_qtys: dict = {}
        for order in today_orders:
            for oi in order.get("items", []):
                key = oi.get("item", "").lower()
                ordered_qtys[key] = ordered_qtys.get(key, 0) + oi.get("qty", 0)
        menu = [
            {**m, "qty_available": max(0, (m["qty_available"] or 0) - ordered_qtys.get(m["item"].lower(), 0))}
            for m in menu
            if (m["qty_available"] is None or m["qty_available"] - ordered_qtys.get(m["item"].lower(), 0) > 0)
        ]

    return {
        "id": restaurant_id,
        "name": restaurant["name"],
        "region": restaurant.get("region", "Malaysia"),
        "type": restaurant.get("type", "hawker"),
        "closing_time": closing_time,
        "discount_pct": dyn["discount_pct"],
        "discount_label": dyn["label"],
        "urgency": dyn["urgency"],
        "minutes_to_close": dyn["minutes_to_close"],
        "menu": menu,
        "is_closing_stock": restaurant.get("closing_stock_date") == today_str,
        "orders_today": len(today_orders),
    }




# =============================================================================
# MARKETPLACE CUSTOMER AUTH (Supabase)
# =============================================================================


@app.post("/api/marketplace/auth/register")
@limiter.limit("10/minute")
async def marketplace_register(request: Request, payload: MarketplaceAuthPayload):
    from services.marketplace_auth import register_customer
    from services.email_service import send_welcome_email
    if not payload.name.strip():
        raise HTTPException(400, "Name is required.")
    try:
        user = register_customer(
            email=payload.email.strip().lower(), password=payload.password,
            name=payload.name.strip(), phone=payload.phone.strip(),
        )
        send_welcome_email(user["email"], user["name"])
        return {"success": True, "message": "Account created! You can now log in."}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/marketplace/auth/login")
@limiter.limit("20/minute")
async def marketplace_login(request: Request, payload: MarketplaceAuthPayload):
    from services.marketplace_auth import login_customer
    try:
        return login_customer(email=payload.email.strip().lower(), password=payload.password)
    except ValueError as e:
        raise HTTPException(401, str(e))


@app.delete("/api/marketplace/auth/delete_account")
async def marketplace_delete_account(request: Request):
    from services.marketplace_auth import validate_customer_token, delete_customer_account
    from services.email_service import send_account_deletion_confirmation
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    customer = validate_customer_token(token)
    if not customer:
        raise HTTPException(401, "Not authenticated.")
    try:
        delete_customer_account(customer["user_id"], token)
        send_account_deletion_confirmation(customer["email"], customer.get("name", ""))
        return {"success": True, "message": "Account permanently deleted."}
    except ValueError as e:
        raise HTTPException(400, str(e))


# =============================================================================
# SUSTAINABILITY
# =============================================================================

@app.get("/api/restaurant/{restaurant_id}/sustainability")
def get_sustainability(restaurant_id: str, user: AuthenticatedUser = Depends(require_auth)):
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    from services.sustainability import get_lifetime_sustainability_totals
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    return {"restaurant_id": restaurant_id, **get_lifetime_sustainability_totals(restaurant)}


# =============================================================================
# GAMIFICATION
# =============================================================================

@app.get("/api/restaurant/{restaurant_id}/gamification")
def get_gamification(restaurant_id: str, user: AuthenticatedUser = Depends(require_auth)):
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    gam = restaurant.get("gamification", {})
    return {
        "current_streak": gam.get("current_streak", 0),
        "longest_streak": gam.get("longest_streak", 0),
        "total_logs": gam.get("total_logs", 0),
        "accuracy_milestones": gam.get("accuracy_milestones", []),
        "last_log_date": gam.get("last_log_date"),
    }


# =============================================================================
# LOCATION INTELLIGENCE
# =============================================================================

@app.get("/api/location/autocomplete")
def location_autocomplete(q: str, lat: float = 3.1390, lon: float = 101.6869):
    if len(q) < 3:
        return {"results": []}
    from services.location_intel import autocomplete_address
    return {"results": autocomplete_address(q, lat, lon)}


@app.get("/api/location/weather")
def get_weather_endpoint(lat: float, lon: float):
    from services.location_intel import get_weather_forecast
    weather = get_weather_forecast(lat, lon)
    if not weather:
        raise HTTPException(503, "Weather service unavailable.")
    return weather


@app.get("/api/location/prayer_times")
def get_prayer_times_endpoint(lat: float, lon: float):
    from services.location_intel import get_prayer_times
    times = get_prayer_times(lat, lon)
    if not times:
        raise HTTPException(503, "Prayer times service unavailable.")
    return times


# =============================================================================
# LANGUAGE PREFERENCE
# =============================================================================

@app.patch("/api/restaurant/{restaurant_id}/language")
def set_language_preference(
    restaurant_id: str, language: str,
    user: AuthenticatedUser = Depends(require_auth),
):
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, user)
    valid = {"english", "malay", "mandarin", "tamil"}
    if language not in valid:
        raise HTTPException(400, f"Language must be one of: {', '.join(valid)}")
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    restaurant["preferred_language"] = language
    save_database(db)
    return {"success": True, "preferred_language": language}


# =============================================================================
# CAUSAL AI — Root Cause Analysis (Feature 3)
# =============================================================================

@app.get("/api/restaurant/{restaurant_id}/causal_analysis")
def causal_analysis(
    restaurant_id: str,
    target_date: Optional[str] = None,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Explain WHY a specific date underperformed. Returns causal breakdown."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, current_user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    if not target_date:
        target_date = (_dt_mod.date.today() - _dt_mod.timedelta(days=1)).isoformat()
    try:
        _dt_mod.date.fromisoformat(target_date)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    return analyse_underperformance(restaurant, target_date)


@app.get("/api/restaurant/{restaurant_id}/causal_analysis/telegram")
def causal_analysis_telegram(
    restaurant_id: str,
    target_date: Optional[str] = None,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Returns causal analysis formatted as a Telegram message."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, current_user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    if not target_date:
        target_date = (_dt_mod.date.today() - _dt_mod.timedelta(days=1)).isoformat()
    report = format_causal_report_telegram(restaurant, target_date)
    return {"report": report, "target_date": target_date}


# =============================================================================
# MENU ENGINEERING — BCG Matrix + AI Recommendations (Feature 5)
# =============================================================================

@app.get("/api/restaurant/{restaurant_id}/menu_engineering")
def menu_engineering(
    restaurant_id: str,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """BCG/Menu Engineering matrix: Stars, Ploughhorses, Puzzles, Dogs + AI recommendations."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, current_user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    if len(restaurant.get("menu", [])) == 0:
        raise HTTPException(400, "No menu items. Add your menu first.")
    classification  = classify_menu_items(restaurant)
    recommendations = generate_menu_recommendations(restaurant)
    return {
        "classification":  classification,
        "recommendations": recommendations,
        "data_days":       len(restaurant.get("daily_records", [])),
        "note": "Recommendations improve with more daily sales data." if len(restaurant.get("daily_records", [])) < 14 else None,
    }


@app.get("/api/restaurant/{restaurant_id}/menu_engineering/weekly_report")
def menu_engineering_weekly_report(
    restaurant_id: str,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Weekly menu engineering report formatted for Telegram."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, current_user)
    db = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    language = restaurant.get("preferred_language", "english")
    report = get_weekly_menu_report_telegram(restaurant, language)
    if not report:
        return {"report": None, "reason": "Insufficient data — need at least 7 days of sales records."}
    return {"report": report}


# =============================================================================
# CHAIN MANAGEMENT — Multi-Branch Support (Feature 8)
# =============================================================================

class CreateChainPayload(BaseModel):
    chain_name: str = Field(..., min_length=2, max_length=80)
    chain_type: str = Field(default="franchise")


class AddBranchPayload(BaseModel):
    restaurant_id: str


class PushMenuPayload(BaseModel):
    menu: List[dict]


@app.post("/api/chains")
def create_new_chain(
    payload: CreateChainPayload,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Create a new restaurant chain owned by the current user."""
    valid_types = {"franchise", "multi_brand", "food_court"}
    if payload.chain_type not in valid_types:
        raise HTTPException(400, f"chain_type must be one of: {', '.join(valid_types)}")
    chain = create_chain(current_user.email, payload.chain_name.strip(), payload.chain_type)
    db = load_database()
    db.setdefault("chains", []).append(chain)
    save_database(db)
    return {"success": True, "chain": chain}


@app.post("/api/chains/{chain_id}/branches")
def add_branch(
    chain_id: str,
    payload: AddBranchPayload,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Add an existing restaurant as a branch of a chain."""
    if not _re.match(r'^chain_[a-f0-9]{8}$', chain_id):
        raise HTTPException(400, "Invalid chain_id format.")
    _validate_rest_id(payload.restaurant_id)
    db = load_database()
    chains = db.get("chains", [])
    chain = next((c for c in chains if c["chain_id"] == chain_id), None)
    if not chain:
        raise HTTPException(404, "Chain not found.")
    if chain.get("owner_email", "").lower() != current_user.email.lower():
        raise HTTPException(403, "You do not own this chain.")
    restaurant = _get_restaurant(db, payload.restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    if restaurant.get("owner_email", "").lower() != current_user.email.lower():
        raise HTTPException(403, "You do not own this restaurant.")
    ok = add_branch_to_chain(chain_id, payload.restaurant_id, db)
    if not ok:
        raise HTTPException(400, "Could not add branch.")
    save_database(db)
    return {"success": True, "chain_id": chain_id, "restaurant_id": payload.restaurant_id}


@app.get("/api/chains/{chain_id}/summary")
def chain_summary(
    chain_id: str,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Get consolidated revenue, waste, and branch stats for a chain."""
    if not _re.match(r'^chain_[a-f0-9]{8}$', chain_id):
        raise HTTPException(400, "Invalid chain_id format.")
    db = load_database()
    chains = db.get("chains", [])
    chain = next((c for c in chains if c["chain_id"] == chain_id), None)
    if not chain:
        raise HTTPException(404, "Chain not found.")
    if chain.get("owner_email", "").lower() != current_user.email.lower():
        raise HTTPException(403, "You do not own this chain.")
    return get_chain_summary(chain_id, db)


@app.post("/api/chains/{chain_id}/push_menu")
def push_menu_to_chain(
    chain_id: str,
    payload: PushMenuPayload,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Push a menu template to all branches in the chain."""
    if not _re.match(r'^chain_[a-f0-9]{8}$', chain_id):
        raise HTTPException(400, "Invalid chain_id format.")
    if not payload.menu or len(payload.menu) > 100:
        raise HTTPException(400, "Menu must have 1-100 items.")
    db = load_database()
    chains = db.get("chains", [])
    chain = next((c for c in chains if c["chain_id"] == chain_id), None)
    if not chain:
        raise HTTPException(404, "Chain not found.")
    if chain.get("owner_email", "").lower() != current_user.email.lower():
        raise HTTPException(403, "You do not own this chain.")
    updated = push_menu_template_to_chain(chain_id, payload.menu, db)
    save_database(db)
    return {"success": True, "branches_updated": updated}


@app.get("/api/chains/{chain_id}/telegram_summary")
def chain_telegram_summary(
    chain_id: str,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Returns chain daily summary formatted for Telegram."""
    if not _re.match(r'^chain_[a-f0-9]{8}$', chain_id):
        raise HTTPException(400, "Invalid chain_id format.")
    db = load_database()
    chains = db.get("chains", [])
    chain = next((c for c in chains if c["chain_id"] == chain_id), None)
    if not chain:
        raise HTTPException(404, "Chain not found.")
    if chain.get("owner_email", "").lower() != current_user.email.lower():
        raise HTTPException(403, "You do not own this chain.")
    return {"report": format_chain_telegram_summary(chain_id, db)}


@app.get("/api/chains")
def list_my_chains(current_user: AuthenticatedUser = Depends(require_auth)):
    """List all chains owned by the current user."""
    db     = load_database()
    chains = [
        c for c in db.get("chains", [])
        if c.get("owner_email", "").lower() == current_user.email.lower()
    ]
    for chain in chains:
        chain["branch_count"] = len(chain.get("branch_ids", []))
    return {"chains": chains, "total": len(chains)}


@app.delete("/api/chains/{chain_id}")
def delete_chain(
    chain_id: str,
    delete_branches: bool = False,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Delete a chain. delete_branches=True also deletes all branch records."""
    if not _re.match(r'^chain_[a-f0-9]{8}$', chain_id):
        raise HTTPException(400, "Invalid chain_id format.")
    db     = load_database()
    chains = db.get("chains", [])
    chain  = next((c for c in chains if c["chain_id"] == chain_id), None)
    if not chain:
        raise HTTPException(404, "Chain not found.")
    if chain.get("owner_email", "").lower() != current_user.email.lower():
        raise HTTPException(403, "You do not own this chain.")
    branch_ids = chain.get("branch_ids", [])
    if delete_branches:
        db["restaurants"] = [r for r in db.get("restaurants", []) if r["id"] not in branch_ids]
        try:
            from services.supabase_db import _sb
            if _sb:
                for rid in branch_ids:
                    _sb.table("restaurants").delete().eq("id", rid).execute()
        except Exception:
            pass
    else:
        for r in db.get("restaurants", []):
            if r.get("chain_id") == chain_id:
                r["chain_id"] = None
    db["chains"] = [c for c in chains if c["chain_id"] != chain_id]
    try:
        from services.supabase_db import _sb
        if _sb:
            _sb.table("chains").delete().eq("chain_id", chain_id).execute()
    except Exception:
        pass
    save_database(db)
    return {
        "success": True, "chain_id": chain_id,
        "branches_affected": len(branch_ids),
        "action": "branches_deleted" if delete_branches else "branches_unlinked",
    }


# =============================================================================
# FEDERATED LEARNING — Privacy-Preserving ML Round (Feature 10)
# =============================================================================

@app.post("/api/admin/federated_round")
def trigger_federated_round(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Admin-only: trigger one round of federated averaging across all restaurants."""
    admin_email = os.environ.get("ADMIN_EMAIL", "").lower()
    if not admin_email or current_user.email.lower() != admin_email:
        raise HTTPException(403, "Admin access required.")
    db = load_database()
    result = run_federated_round(db)
    if result.get("participants", 0) > 0:
        save_database(db)
    return {
        "success":       True,
        "participants":  result["participants"],
        "skipped":       result["skipped"],
        "model_version": db.get("federated_model", {}).get("version", 0),
        "updated_at":    db.get("federated_model", {}).get("updated_at"),
    }


@app.get("/api/federated/model_info")
def federated_model_info(user: AuthenticatedUser = Depends(require_auth)):
    """Authenticated: returns current federated model metadata (not weights)."""
    db    = load_database()
    model = db.get("federated_model", {})
    return {
        "version":      model.get("version", 0),
        "updated_at":   model.get("updated_at"),
        "participants": "Privacy-preserving — participant count not disclosed.",
        "description":  "Shared demand prediction model trained via federated averaging. No restaurant data was shared.",
    }


# =============================================================================
# COMPUTER VISION INVENTORY — Image-based Stock Detection (Feature 4)

# =============================================================================
# COMPUTER VISION INVENTORY -- Image-based Stock Detection
# =============================================================================

@app.post("/api/restaurant/{restaurant_id}/cv_inventory")
async def cv_inventory_scan(
    restaurant_id: str,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_auth),
):
    """Upload an inventory photo - AI detects ingredient quantities automatically."""
    _validate_rest_id(restaurant_id)
    require_restaurant_access(restaurant_id, current_user)
    if file.content_type not in ("image/jpeg", "image/png", "image/webp", "image/jpg"):
        raise HTTPException(400, "Upload a JPEG, PNG, or WebP image.")
    image_bytes = await file.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(413, "Image too large. Max 10MB.")
    if len(image_bytes) < 1000:
        raise HTTPException(400, "Image file appears to be empty or corrupt.")
    db         = load_database()
    restaurant = _get_restaurant(db, restaurant_id)
    if not restaurant:
        raise HTTPException(404, "Restaurant not found.")
    from services.computer_vision_inventory import scan_inventory_from_image
    return scan_inventory_from_image(image_bytes, restaurant)


# =============================================================================
# TELEGRAM WEBHOOK
# =============================================================================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram webhook updates."""
    from services.security import verify_telegram_webhook
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    if not webhook_secret:
        print("[Webhook] Error: WEBHOOK_SECRET is not set. Rejecting message for maximum security.")
        return {"ok": True}
        
    incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not verify_telegram_webhook(incoming, webhook_secret):
        print("[Webhook] Security warning: Unauthorized webhook access attempt.")
        return {"ok": True}  # Silently reject
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}
    try:
        from services.telegram_bot import handle_update
        await handle_update(data, os.environ.get("TELEGRAM_TOKEN", ""))
    except Exception as e:
        print(f"[Webhook] Error: {e}")
    return {"ok": True}


# =============================================================================
# DASHBOARD ACTION APPROVAL
# Bot approves in-process via _pending_dashboard_approvals dict.
# No public HTTP endpoint for approval - that would be a security flaw.
# =============================================================================

_pending_dashboard_approvals: dict = {}


@app.post("/api/auth/dashboard_action/request")
async def request_dashboard_action_approval(
    action: str,
    restaurant_id: Optional[str] = None,
    chain_id: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_auth),
):
    """Request primary Telegram approval for a destructive dashboard action."""
    valid_actions = {"delete_restaurant", "delete_chain", "create_chain", "add_branch", "remove_branch"}
    if action not in valid_actions:
        raise HTTPException(400, "action must be one of: " + ", ".join(sorted(valid_actions)))
    account = auth.get_account_by_email(user.email)
    if not account:
        raise HTTPException(404, "Account not found.")
    primary = next((s for s in account.get("sessions", []) if s.get("is_primary") and s.get("chat_id")), None)
    if not primary:
        raise HTTPException(400, "No primary Telegram linked. Connect a Telegram account first.")
    import secrets as _sec
    approval_token = _sec.token_urlsafe(16)
    expires_at = (_dt_mod.datetime.utcnow() + _dt_mod.timedelta(minutes=10)).isoformat()
    _pending_dashboard_approvals[approval_token] = {
        "action": action, "restaurant_id": restaurant_id, "chain_id": chain_id,
        "email": user.email, "status": "pending",
        "expires_at": expires_at, "primary_chat_id": primary["chat_id"],
    }
    action_labels = {
        "delete_restaurant": "Delete restaurant",
        "delete_chain":      "Delete entire chain",
        "create_chain":      "Create new chain",
        "add_branch":        "Add restaurant to chain",
        "remove_branch":     "Remove branch from chain",
    }
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    if tg_token:
        import httpx as _httpx2
        msg = (
            "\U0001F510 *Dashboard Action Requires Your Approval*\n\n"
            + "Action: *" + action_labels.get(action, action) + "*\n"
            + "Resource: " + str(restaurant_id or chain_id or "N/A") + "\n\n"
            + "Reply `approve " + approval_token[:8] + "` to allow\n"
            + "Reply `deny " + approval_token[:8] + "` to reject\n\n"
            + "_Expires in 10 minutes_"
        )
        try:
            async with _httpx2.AsyncClient() as _c:
                await _c.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": primary["chat_id"], "text": msg, "parse_mode": "Markdown"},
                    timeout=8,
                )
        except Exception:
            pass
    return {"approval_token": approval_token, "expires_in_seconds": 600}


@app.get("/api/auth/dashboard_action/status/{approval_token}")
def check_dashboard_action_status(approval_token: str, user: AuthenticatedUser = Depends(require_auth)):
    """Poll to check if the primary Telegram has approved/denied the action."""
    entry = _pending_dashboard_approvals.get(approval_token)
    if not entry:
        raise HTTPException(404, "Approval not found or expired.")
    if entry["email"].lower() != user.email.lower():
        raise HTTPException(403, "Not your approval request.")
    now = _dt_mod.datetime.utcnow().isoformat()
    if entry["expires_at"] < now:
        _pending_dashboard_approvals.pop(approval_token, None)
        raise HTTPException(410, "Approval expired.")
    return {"status": entry["status"], "action": entry["action"]}
