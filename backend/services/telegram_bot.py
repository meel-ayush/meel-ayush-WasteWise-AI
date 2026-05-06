import os
import sys
import asyncio
import datetime
import json
import io
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import httpx
from dotenv import load_dotenv

from services.nlp import (
    process_ai_data_ingestion, register_owner_event, detect_intent,
    load_database, save_database, _get_restaurant, _do_generate_forecast,
    process_image_upload,
)
from services.ai_provider import call_ai
from services.file_processor import process_upload, extract_image_mime
from services import auth
from services.bom_ai import ask_bom_conversational

load_dotenv()

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TG_API          = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
MAX_FILE_BYTES  = 5_000_000
ALLOWED_DOC_EXT = {".csv", ".txt", ".xlsx", ".xls"}
ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

GREETINGS = {
    "hi","hello","hey","hye","yo","sup","start","hai","helo","oi","woi",
    "good morning","good afternoon","good evening","selamat pagi",
    "selamat petang","selamat malam","assalamualaikum","salam",
}

_session_state: dict = {}
_session_data:  dict = {}

# OTP validity message helper
def _otp_minutes_note() -> str:
    return f"(valid for {auth.OTP_TTL_SECONDS} seconds)"


async def _api(client: httpx.AsyncClient, method: str, **kwargs) -> dict:
    url  = f"{TG_API}/{method}"
    resp = await client.post(url, json=kwargs, timeout=30)
    return resp.json()


async def _send(client: httpx.AsyncClient, chat_id: int, text: str,
                parse_mode: str = "Markdown", reply_markup=None) -> None:
    params = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    await _api(client, "sendMessage", **params)


async def _typing(client: httpx.AsyncClient, chat_id: int) -> None:
    await _api(client, "sendChatAction", chat_id=chat_id, action="typing")


async def _download_file(client: httpx.AsyncClient, file_id: str) -> bytes | None:
    resp = await _api(client, "getFile", file_id=file_id)
    if not resp.get("ok"):
        return None
    path = resp["result"]["file_path"]
    r    = await client.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}", timeout=60)
    return r.content if r.status_code == 200 else None


def _keyboard(rows: list) -> dict:
    return {"keyboard": rows, "resize_keyboard": True, "one_time_keyboard": True}

def _inline_keyboard(rows: list) -> dict:
    return {"inline_keyboard": rows}

def _rest_keyboard() -> dict:
    db = load_database()
    rows = [[{"text": r["name"]}] for r in db.get("restaurants", [])]
    rows.append([{"text": "➕ Register my restaurant"}])
    return _keyboard(rows)

def _get_state(chat_id: int) -> str | None:
    return _session_state.get(chat_id)

def _set_state(chat_id: int, state: str | None) -> None:
    if state is None:
        _session_state.pop(chat_id, None)
    else:
        _session_state[chat_id] = state

def _get_data(chat_id: int) -> dict:
    return _session_data.get(chat_id, {})

def _set_data(chat_id: int, **kwargs) -> None:
    _session_data.setdefault(chat_id, {}).update(kwargs)

def _clear_data(chat_id: int, *keys) -> None:
    if chat_id in _session_data:
        for k in keys:
            _session_data[chat_id].pop(k, None)

def _get_rest_id(chat_id: int) -> str | None:
    stored = _get_data(chat_id).get("restaurant_id")
    if stored:
        return stored
    # Check if this chat_id is linked to a restaurant in the DB
    db = load_database()
    for r in db.get("restaurants", []):
        if r.get("telegram_chat_id") == chat_id:
            _set_data(chat_id, restaurant_id=r["id"])
            return r["id"]
    return None


def _validate_file(filename: str, data: bytes) -> tuple:
    if not filename:
        return False, "File has no name."
    ext = os.path.splitext(filename.lower())[1]
    if ext not in (ALLOWED_DOC_EXT | ALLOWED_IMG_EXT):
        return False, f"File type '{ext}' is not supported. Please send: CSV, Excel (.xlsx), JPG, or PNG."
    if len(data) > MAX_FILE_BYTES:
        return False, f"File is too large ({len(data)/1_000_000:.1f} MB). Max 5 MB."
    if len(data) < 10:
        return False, "File appears empty or corrupted."
    if ext in {".csv", ".txt"}:
        sample = data[:2000].decode("utf-8", errors="ignore").lower()
        for bad in ["<script", "<?php", "subprocess", "__import__", "exec(", "eval("]:
            if bad in sample:
                return False, "File contains suspicious content and cannot be processed."
    return True, "ok"


def _strip_image_metadata(data: bytes) -> bytes:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", optimize=True)
        clean = buf.getvalue()
        return clean if len(clean) > 1000 else data
    except Exception:
        return data


# ── Registration flow ─────────────────────────────────────────────────────────

PRIVACY_NOTICE = (
    "🔒 *Privacy Notice*\n\n"
    "Before we start, here's what WasteWise AI stores:\n\n"
    "• Your restaurant name and location type\n"
    "• Menu items and daily sales quantities you upload\n"
    "• Photos you send (processed immediately, not stored permanently)\n"
    "• Telegram chat ID (to send you daily check-ins)\n\n"
    "Your data is used *only* to improve your own forecasts. "
    "Cross-restaurant signals use anonymised category trends — never your restaurant's name or revenue.\n\n"
    "Do you agree to continue?"
)


async def start_registration(client: httpx.AsyncClient, chat_id: int) -> None:
    _set_state(chat_id, "reg_privacy")
    kb = _inline_keyboard([
        [{"text": "✅ I agree", "callback_data": "reg:privacy_accept"},
         {"text": "❌ No thanks", "callback_data": "reg:privacy_decline"}]
    ])
    await _send(client, chat_id, PRIVACY_NOTICE, reply_markup=kb)


async def _reg_step_name(client: httpx.AsyncClient, chat_id: int) -> None:
    _set_state(chat_id, "reg_name")
    await _send(client, chat_id,
        "Great! Let's set up your restaurant.\n\n"
        "What is your restaurant's name? (e.g. *Ali Nasi Lemak*)"
    )


async def _reg_step_owner(client: httpx.AsyncClient, chat_id: int) -> None:
    _set_state(chat_id, "reg_owner")
    await _send(client, chat_id,
        "What's your name? (Just your first name or nickname is fine)"
    )


async def _reg_step_type(client: httpx.AsyncClient, chat_id: int) -> None:
    _set_state(chat_id, "reg_type")
    kb = _keyboard([
        [{"text": "🍛 Hawker / Gerai"}, {"text": "🍵 Mamak"}],
        [{"text": "☕ Café / Kopitiam"}, {"text": "🍽️ Restaurant"}],
        [{"text": "🍦 Dessert Stall"}, {"text": "🍡 Other"}],
    ])
    await _send(client, chat_id, "What type of food business is it?", reply_markup=kb)


async def _reg_step_region(client: httpx.AsyncClient, chat_id: int) -> None:
    _set_state(chat_id, "reg_region")
    await _send(client, chat_id,
        "Which area or city is your restaurant in?\n\n"
        "Just type naturally — for example:\n"
        "_Subang SS15_, _Chow Kit KL_, _Georgetown Penang_, _Setia Alam Shah Alam_",
        reply_markup={"remove_keyboard": True}
    )


async def _reg_confirm(client: httpx.AsyncClient, chat_id: int) -> None:
    data = _get_data(chat_id)
    name   = data.get("reg_name", "?")
    owner  = data.get("reg_owner", "?")
    rtype  = data.get("reg_type", "?")
    region = data.get("reg_region", "?")
    closing = data.get("reg_closing_time", "21:00")
    kb = _inline_keyboard([
        [{"text": "✅ Looks good!", "callback_data": "reg:confirm"},
         {"text": "🔁 Start over",   "callback_data": "reg:restart"}]
    ])
    await _send(client, chat_id,
        f"*Here's what I've got:*\n\n"
        f"🏪 Restaurant: *{name}*\n"
        f"👤 Owner: *{owner}*\n"
        f"📋 Type: *{rtype}*\n"
        f"📍 Area: *{region}*\n"
        f"⏰ Closing Time: *{closing}*\n\n"
        "Is this correct?",
        reply_markup=kb
    )


async def _reg_complete(client: httpx.AsyncClient, chat_id: int) -> None:
    data   = _get_data(chat_id)
    name   = data.get("reg_name", "My Restaurant")
    owner  = data.get("reg_owner", "Owner")
    rtype  = data.get("reg_type", "hawker").lower().split("/")[0].strip().replace(" ", "_").replace("é","e")
    region = data.get("reg_region", "Malaysia")
    closing_time = data.get("reg_closing_time", "21:00")

    rest_id = "rest_" + str(uuid.uuid4())[:8]
    new_rest = {
        "id":                  rest_id,
        "name":                name,
        "region":              region,
        "type":                rtype,
        "owner_name":          owner,
        "telegram_chat_id":    chat_id,
        "privacy_accepted":    True,
        "registered_at":       datetime.datetime.now().isoformat(),
        "specialty_weather":   "neutral",
        "bom":                 {},
        "menu":                [],
        "recent_feedback_memory": [],
        "active_events":       [],
        "daily_records":       [],
        "closing_time":        closing_time,
        "discount_pct":        30,
        "marketplace_enabled": True,
    }

    db = load_database()
    db.setdefault("restaurants", []).append(new_rest)
    # Add region if it doesn't exist yet (new location)
    if region not in db.get("regions", {}):
        db.setdefault("regions", {})[region] = {
            "type":                "General Area",
            "foot_traffic_baseline": 500,
            "weekend_multiplier":  1.1,
            "holiday_multiplier":  1.0,
            "rain_impact":         -0.2,
        }
    save_database(db)

    _set_data(chat_id, restaurant_id=rest_id)
    _set_state(chat_id, None)
    _clear_data(chat_id, "reg_name", "reg_owner", "reg_type", "reg_region", "reg_closing_time")

    await _send(client, chat_id,
        f"🎉 *Welcome to WasteWise AI, {owner}!*\n\n"
        f"*{name}* is now registered.\n"
        f"⏰ Closing time set to *{closing_time}* — I'll send you an inventory report automatically!\n\n"
        "Let's get started! Add your first menu items — just tell me what you sell:\n\n"
        "_'I sell Nasi Lemak, Teh Tarik, and Kuih Seri Muka'_\n\n"
        "Or upload a CSV/Excel with your menu.\n\n"
        "Once you have at least 3 days of sales data, I'll start making forecasts for you.",
        reply_markup={"remove_keyboard": True}
    )


async def _reg_step_closing_time(client: httpx.AsyncClient, chat_id: int) -> None:
    _set_state(chat_id, "reg_closing_time")
    kb = _keyboard([
        [{"text": "🕐 21:00 (9 PM)"}, {"text": "🕐 22:00 (10 PM)"}],
        [{"text": "🕐 20:00 (8 PM)"}, {"text": "🕐 23:00 (11 PM)"}],
        [{"text": "🕐 18:00 (6 PM)"}, {"text": "🕐 19:00 (7 PM)"}],
    ])
    await _send(client, chat_id,
        "⏰ At what time do you usually *close* your shop?\n\n"
        "This is when I'll automatically send you an inventory report and list any remaining food "
        "at a discount on the customer marketplace!\n\n"
        "Pick one or type your own time (e.g. *21:30*)",
        reply_markup=kb,
    )


async def _ask_bom_interactive(client: httpx.AsyncClient, chat_id: int, item_name: str) -> None:
    """Ask the owner about ingredients interactively. Offers 'don't know' option."""
    _set_state(chat_id, f"bom_item:{item_name}")
    await _send(client, chat_id,
        f"🥘 What ingredients go into *{item_name}*?\n\n"
        "Example: _'200g rice, 50ml coconut milk, 20g dried anchovies'_\n\n"
        "Or type *don't know* — I'll look up the typical recipe for your area."
    )


async def _set_bom_for_item(client: httpx.AsyncClient, chat_id: int, item_name: str, bom_text: str) -> None:
    """Parse and save owner-defined BOM for a menu item."""
    rest_id = _get_rest_id(chat_id)
    if not rest_id:
        return
    db   = load_database()
    rest = _get_restaurant(db, rest_id)
    if not rest:
        return

    prompt = (
        f"The owner of a Malaysian restaurant defined the ingredients for '{item_name}':\n"
        f"'{bom_text}'\n\n"
        "Extract as a JSON dict where keys are ingredient names (use snake_case with _g for grams, "
        "_ml for millilitres) and values are numbers per serving.\n"
        "Example: {\"rice_g\": 200, \"coconut_milk_ml\": 50, \"egg\": 1}\n"
        "Also estimate cost_rm (raw material cost in Malaysian Ringgit) per serving.\n"
        "Return ONLY valid JSON with no other text."
    )
    result = call_ai(prompt, json_mode=True)
    try:
        import json as _json
        if isinstance(result, str):
            bom = _json.loads(result)
        else:
            bom = result or {}
    except Exception:
        bom = {}

    if bom:
        rest.setdefault("bom", {})[item_name] = bom
        save_database(db)
        ingredients = [f"{k}: {v}" for k, v in bom.items() if k != "cost_rm"]
        cost = bom.get("cost_rm", "?")
        await _send(client, chat_id,
            f"✅ Saved ingredient ratios for *{item_name}*:\n"
            + "\n".join(f"  • {i}" for i in ingredients)
            + f"\n  • Estimated cost: RM {cost} per serving\n\n"
            "Your shopping list will now be accurate for this item."
        )
    else:
        await _send(client, chat_id,
            f"⚠️ Could not parse that. Please try again with a clearer format:\n"
            f"_'200g rice, 50ml coconut milk, 1 egg'_"
        )


# ── File processing helpers ───────────────────────────────────────────────────

async def _process_csv(client: httpx.AsyncClient, chat_id: int, csv_type: str) -> None:
    data    = _get_data(chat_id)
    raw     = data.get("pending_csv")
    rest_id = _get_rest_id(chat_id)
    if not raw:
        await _send(client, chat_id, "⚠️ No file data found. Please upload again.")
        return
    mode_map = {"sales": "none", "add_menu": "append", "replace_menu": "overwrite"}
    labels   = {"none": "sales data", "append": "add menu items", "overwrite": "replace menu"}
    mode     = mode_map.get(csv_type, "none")
    await _typing(client, chat_id)
    result   = process_ai_data_ingestion(rest_id, raw, menu_mode=mode)
    _set_data(chat_id, last_action=f"csv_{csv_type}", last_result=result)
    await _send(client, chat_id, f"✅ Processed as *{labels[mode]}*.\n\n{result}")
    await asyncio.sleep(2)
    await _typing(client, chat_id)
    forecast = _do_generate_forecast(rest_id)
    await _send(client, chat_id, f"🔄 *Updated forecast:*\n\n{forecast}")
    _clear_data(chat_id, "pending_csv", "pending_csv_type")
    _set_state(chat_id, None)


async def _process_photo(client: httpx.AsyncClient, chat_id: int, photo_data: bytes, intent: str) -> None:
    rest_id = _get_rest_id(chat_id)
    _set_state(chat_id, None)
    await _send(client, chat_id, "📸 Analysing your photo…")
    await _typing(client, chat_id)
    clean  = _strip_image_metadata(photo_data)
    result = process_image_upload(rest_id, clean, "image/jpeg")
    _set_data(chat_id, last_action=f"photo_{intent}", last_result=result)
    _clear_data(chat_id, "pending_photo_data")

    skip = ["Cannot process", "cannot be used", "Low confidence", "not reliable", "no readable"]
    if any(p.lower() in result.lower() for p in skip):
        await _send(client, chat_id, result)
        await _send(client, chat_id,
            "💡 For best results:\n"
            "• Make sure numbers are clearly visible\n"
            "• Good lighting — not too dark\n"
            "• Hold the camera still\n\n"
            "Or type the numbers directly: _'Nasi Lemak 95, Teh Tarik 62'_"
        )
        return
    await _send(client, chat_id, result)
    await asyncio.sleep(2)
    await _typing(client, chat_id)
    forecast = _do_generate_forecast(rest_id)
    await _send(client, chat_id, f"🔄 *Updated forecast:*\n\n{forecast}")


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_callback(client: httpx.AsyncClient, callback: dict) -> None:
    chat_id = callback["message"]["chat"]["id"]
    msg_id  = callback["message"]["message_id"]
    data    = callback.get("data", "")
    await _api(client, "answerCallbackQuery", callback_query_id=callback["id"])
    await _api(client, "editMessageReplyMarkup",
               chat_id=chat_id, message_id=msg_id,
               reply_markup=json.dumps({"inline_keyboard": []}))

    if data == "reg:privacy_accept":
        await _reg_step_name(client, chat_id)
    elif data == "reg:privacy_decline":
        _set_state(chat_id, None)
        await _send(client, chat_id,
            "No problem! You can use WasteWise AI without registering by logging in to an existing restaurant.\n"
            "Type *login* to see the list.")
    elif data == "reg:confirm":
        await _reg_complete(client, chat_id)
    elif data == "reg:restart":
        await start_registration(client, chat_id)

    # ── Delete account choices ─────────────────────────────────────────────────
    elif data == "delete:keep":
        account = auth.get_account_by_telegram(chat_id)
        primary = next((s for s in (account or {}).get("sessions",[]) if s.get("is_primary") and s.get("chat_id") == chat_id), None)
        if not primary:
            await _send(client, chat_id, "❌ Only the primary account can delete.")
        else:
            _set_state(chat_id, "confirm_delete")
            await _send(client, chat_id,
                "🌿 *Confirm Anonymise Account*\n\n"
                "Your restaurant name, owner info, and Telegram link will be removed.\n"
                "Anonymised sales history is kept to help improve AI forecasts for other hawkers.\n\n"
                "Type *YES DELETE MY ACCOUNT* to confirm, or anything else to cancel."
            )
    elif data == "delete:hard":
        account = auth.get_account_by_telegram(chat_id)
        primary = next((s for s in (account or {}).get("sessions",[]) if s.get("is_primary") and s.get("chat_id") == chat_id), None)
        if not primary:
            await _send(client, chat_id, "❌ Only the primary account can delete.")
        else:
            _set_state(chat_id, "confirm_delete_hard")
            await _send(client, chat_id,
                "💣 *Confirm Permanent Delete*\n\n"
                "⚠️ This will erase EVERYTHING — account, restaurant, ALL sales history.\n"
                "This *cannot be undone*.\n\n"
                "Type *YES DELETE MY ACCOUNT* to confirm, or anything else to cancel."
            )
    elif data == "delete:cancel":
        _set_state(chat_id, None)
        await _send(client, chat_id, "✅ Deletion cancelled. Your account is safe. 🌿")

    # ── Chain creation confirmation ────────────────────────────────────────────
    elif data.startswith("chain:create:"):
        chain_name = data[len("chain:create:"):]
        account = auth.get_account_by_telegram(chat_id)
        if not account:
            await _send(client, chat_id, "❌ Please login first.")
        else:
            import uuid as _uuid_bot
            chain_id = "chain_" + str(_uuid_bot.uuid4())[:8]
            db_ch = load_database()
            db_ch.setdefault("chains", []).append({
                "chain_id": chain_id,
                "name": chain_name,
                "owner_email": account["email"],
                "chain_type": "franchise",
                "created_at": datetime.datetime.now().isoformat(),
            })
            save_database(db_ch)
            await _send(client, chat_id,
                f"✅ *Chain created: {chain_name}*\n\n"
                f"Chain ID: `{chain_id}`\n\n"
                "Now link your restaurants:\n"
                f"`add to chain {chain_id}`"
            )
    elif data == "chain:cancel":
        _set_state(chat_id, None)
        await _send(client, chat_id, "✅ Chain creation cancelled.")

    # ── Security session buttons ────────────────────────────────────────────────
    elif data.startswith("sec:remove:"):
        sid_prefix = data[len("sec:remove:"):]  # exactly 8 chars
        account = auth.get_account_by_telegram(chat_id)
        if not account:
            await _send(client, chat_id, "❌ Not logged in.")
        else:
            sessions = auth.get_sessions_for_account(account["email"])
            # Match session whose first 8 chars == sid_prefix (safe: fixed-length comparison)
            target = next((s for s in sessions if s["session_id"][:8] == sid_prefix), None)
            if not target:
                await _send(client, chat_id, "⚠️ Session not found or already removed.")
            elif target.get("is_primary"):
                await _send(client, chat_id, "❌ Cannot remove the primary session.")
            else:
                is_primary_caller = any(s.get("is_primary") and s.get("chat_id") == chat_id for s in sessions)
                target_type = target.get("type", "web")
                # Primary can remove any non-primary. Web sessions can only be removed by primary.
                if not is_primary_caller:
                    await _send(client, chat_id, "❌ Only the primary account can remove other sessions.")
                else:
                    removed = auth.remove_session(account["email"], target["session_id"])
                    label = target.get("telegram_username") or target.get("label", "session")
                    if removed:
                        await _send(client, chat_id, f"✅ Session *{label}* removed successfully.")
                    else:
                        await _send(client, chat_id, "⚠️ Could not remove session.")

    elif data.startswith("sec:mkprimary:"):
        target_uname = data[len("sec:mkprimary:"):]
        account = auth.get_account_by_telegram(chat_id)
        if not account:
            await _send(client, chat_id, "❌ Not logged in.")
        else:
            sessions = auth.get_sessions_for_account(account["email"])
            is_primary_caller = any(s.get("is_primary") and s.get("chat_id") == chat_id for s in sessions)
            if not is_primary_caller:
                await _send(client, chat_id, "❌ Only the current primary account can transfer primary status.")
            else:
                target_s = next((s for s in sessions if s.get("telegram_username", "").lower() == target_uname.lower() and s.get("type") == "telegram"), None)
                if not target_s:
                    await _send(client, chat_id, f"❌ No Telegram session found for @{target_uname}.")
                elif target_s.get("is_primary"):
                    await _send(client, chat_id, f"⭐ @{target_uname} is already the primary.")
                else:
                    db_p = load_database()
                    acc_p = next((a for a in db_p.get("accounts", []) if a["email"].lower() == account["email"].lower()), None)
                    if acc_p:
                        for s in acc_p.get("sessions", []):
                            s["is_primary"] = (s["session_id"] == target_s["session_id"])
                        save_database(db_p)
                        await _send(client, chat_id, f"✅ *Primary transferred to @{target_uname}!*\n\nThey now hold primary status.")
                        new_chat_id = target_s.get("chat_id")
                        if new_chat_id:
                            await _send(client, new_chat_id,
                                f"⭐ *You are now the PRIMARY account!*\n\n"
                                f"Account: {account['email']}\n"
                                "You can now approve logins and manage account settings."
                            )
    elif data.startswith("photo:"):
        intent     = data.split(":")[1]
        photo_data = _get_data(chat_id).get("pending_photo_data")
        if not photo_data:
            await _send(client, chat_id, "Photo expired. Please send it again.")
            _set_state(chat_id, None)
            return
        labels = {"sales": "📊 Sales / Receipt", "menu": "📋 Menu Board"}
        await _send(client, chat_id, f"Got it — processing as *{labels.get(intent, intent)}*…")
        await _process_photo(client, chat_id, photo_data, intent)
    elif data.startswith("csv:"):
        csv_type = data.split(":")[1]
        labels   = {"sales": "📊 Sales Data", "add_menu": "➕ Add Menu Items",
                    "replace_menu": "🔄 Replace Full Menu"}
        await _send(client, chat_id, f"Got it — processing as *{labels.get(csv_type, csv_type)}*…")
        _set_state(chat_id, None)
        await _process_csv(client, chat_id, csv_type)



# ── Document + photo handlers ─────────────────────────────────────────────────

async def handle_document(client: httpx.AsyncClient, chat_id: int, document: dict) -> None:
    rest_id = _get_rest_id(chat_id)
    if not rest_id:
        await _send(client, chat_id, "Please login or register first.")
        return
    db   = load_database()
    rest = _get_restaurant(db, rest_id)
    if not rest:
        await _send(client, chat_id, "Please login or register first.")
        return

    filename  = document.get("file_name", "upload")
    file_size = document.get("file_size", 0)
    ext       = os.path.splitext(filename.lower())[1]

    if ext not in (ALLOWED_DOC_EXT | ALLOWED_IMG_EXT):
        await _send(client, chat_id,
            "📁 Unsupported file type.\n\nI can read: *CSV, Excel (.xlsx), JPG, PNG*")
        return
    if file_size > MAX_FILE_BYTES:
        await _send(client, chat_id,
            f"❌ File too large ({file_size/1_000_000:.1f} MB). Max 5 MB.")
        return

    await _send(client, chat_id, "📁 Downloading your file…")
    data = await _download_file(client, document["file_id"])
    if not data:
        await _send(client, chat_id, "⚠️ Could not download the file. Please try again.")
        return

    ok, err = _validate_file(filename, data)
    if not ok:
        await _send(client, chat_id, f"❌ {err}")
        return

    if ext in ALLOWED_IMG_EXT:
        mime  = extract_image_mime(filename)
        clean = _strip_image_metadata(data)
        _set_data(chat_id, pending_photo_data=clean)
        _set_state(chat_id, "choosing_photo_type")
        kb = _inline_keyboard([
            [{"text": "📊 Sales / Receipt",        "callback_data": "photo:sales"},
             {"text": "📋 Menu Board / New Items",  "callback_data": "photo:menu"}],
        ])
        await _send(client, chat_id,
            "📸 Image file received!\n\nWhat is this a photo of?",
            reply_markup=kb)
        return

    text, fmt = process_upload(filename, data)
    if not text.strip():
        await _send(client, chat_id, "⚠️ Could not read data from this file. Please check it.")
        return

    _set_data(chat_id, pending_csv=text)
    _set_state(chat_id, "choosing_csv_type")
    lines = len(text.strip().splitlines())
    kb    = _inline_keyboard([
        [{"text": "📊 Sales Data",        "callback_data": "csv:sales"},
         {"text": "➕ Add Menu Items",    "callback_data": "csv:add_menu"}],
        [{"text": "🔄 Replace Full Menu", "callback_data": "csv:replace_menu"}],
    ])
    await _send(client, chat_id,
        f"📁 *File received!* ({fmt.upper()}, {lines} rows)\n\n"
        f"What type of data is this for *{rest['name']}*?",
        reply_markup=kb)


async def handle_photo(client: httpx.AsyncClient, chat_id: int, photos: list) -> None:
    rest_id = _get_rest_id(chat_id)
    if not rest_id:
        await _send(client, chat_id, "Please login or register first.")
        return

    best  = max(photos, key=lambda p: p.get("file_size", 0))
    data  = await _download_file(client, best["file_id"])
    if not data:
        await _send(client, chat_id, "⚠️ Could not download photo. Please try again.")
        return

    _set_data(chat_id, pending_photo_data=data)
    _set_state(chat_id, "choosing_photo_type")
    db   = load_database()
    rest = _get_restaurant(db, rest_id)
    kb   = _inline_keyboard([
        [{"text": "📊 Sales / Receipt",        "callback_data": "photo:sales"},
         {"text": "📋 Menu Board / New Items",  "callback_data": "photo:menu"}],
    ])
    await _send(client, chat_id,
        "📸 Photo received!\n\n"
        "What is this a photo of?\n\n"
        "• *Sales / Receipt* — Your receipt, whiteboard with today's sales, or handwritten totals\n"
        "• *Menu Board / New Items* — A menu board or list of dishes to add to your menu",
        reply_markup=kb)


def _parse_stock_reply(text: str, menu_map: dict) -> tuple[dict, int | None]:
    """
    Parse shopkeeper reply into {canonical_item_name: qty} and optional discount %.
    Supports:
      "Nasi Lemak 15, Teh Tarik 8"
      "Nasi Lemak 15 at 20%, Teh Tarik 8"
      "15 Nasi Lemak, 8 Teh Tarik"
      "nasi lemak: 15\nteh tarik: 8"
    Returns: (parsed_dict, custom_discount_pct_or_None)
    """
    import re as _rep

    tl = text.lower()
    custom_pct: int | None = None

    # Extract custom discount if mentioned: "at 25%" / "25% off" / "discount 25"
    pct_match = _rep.search(r'(?:at|discount|@)\s*(\d{1,2})\s*%|(\d{1,2})\s*%\s*off', tl)
    if pct_match:
        raw = pct_match.group(1) or pct_match.group(2)
        if raw:
            custom_pct = int(raw)
        # Remove discount part from text to avoid confusing the qty parser
        text = _rep.sub(r'(?:at|discount|@)\s*\d{1,2}\s*%|\d{1,2}\s*%\s*off', '', text, flags=_rep.IGNORECASE).strip()

    parsed: dict = {}

    # Pattern 1: "Item Name: 15" or "Item Name 15"
    for m in _rep.finditer(r'([A-Za-z][A-Za-z\s\'/\-]+?)\s*:?\s*(\d+)', text):
        item_raw, qty_raw = m.group(1).strip(), int(m.group(2))
        if qty_raw > 0 and len(item_raw) > 1:
            # Fuzzy match to menu
            canonical = _fuzzy_match_menu(item_raw, menu_map)
            if canonical:
                parsed[canonical] = qty_raw

    # Pattern 2: "15 Item Name"
    for m in _rep.finditer(r'(\d+)\s+([A-Za-z][A-Za-z\s\'/\-]+?)(?:,|;|\n|$)', text):
        qty_raw, item_raw = int(m.group(1)), m.group(2).strip()
        if qty_raw > 0 and len(item_raw) > 1:
            canonical = _fuzzy_match_menu(item_raw, menu_map)
            if canonical and canonical not in parsed:
                parsed[canonical] = qty_raw

    return parsed, custom_pct


def _fuzzy_match_menu(name: str, menu_map: dict) -> str | None:
    """Return canonical menu item name or None."""
    key = name.lower().strip()
    if key in menu_map:
        return menu_map[key]["item"]
    for mk, mv in menu_map.items():
        if key in mk or mk in key:
            return mv["item"]
    return name if len(name) > 1 else None   # keep unknown items


# ── Stage 1 handler: pre-closing inventory (2hrs before) ─────────────────────

async def _handle_pre_closing_reply(
    client: httpx.AsyncClient,
    chat_id: int,
    text: str,
    restaurant: dict,
    db: dict,
    today_str: str,
) -> None:
    """
    Shopkeeper replied to the 2-hr-before-close question.
    Parses stock + optional custom discount.
    Posts to marketplace. Saves pre_closing_stock for Stage 2 comparison.
    """
    tl = text.strip().lower()
    menu_map = {m["item"].lower(): m for m in restaurant.get("menu", [])}
    default_disc = restaurant.get("discount_pct", 30)
    marketplace_enabled = restaurant.get("marketplace_enabled", True)

    # Sold-out reply
    sold_out_words = {"none","nothing","0","zero","all sold","habis","sold out","kosong","tiada","no stock","all gone"}
    if tl in sold_out_words or all(w in tl for w in ["all","sold"]):
        restaurant.pop(f"awaiting_pre_closing_inventory_{today_str}", None)
        save_database(db)
        await _send(client, chat_id,
            "✅ *Impressive — already sold out before closing!* 🎉\n\n"
            "Zero waste today. AI is noting your perfect sell-through.\n"
            "📈 _Tomorrow's forecast will stay similar or increase slightly._"
        )
        return

    parsed, custom_pct = _parse_stock_reply(text, menu_map)
    disc_pct = custom_pct if custom_pct is not None else default_disc

    if not parsed:
        await _send(client, chat_id,
            "⚠️ Couldn't read that. Try:\n\n"
            "_'Nasi Lemak 15, Teh Tarik 8'_\n"
            "_'Nasi Lemak 15 at 20%, Teh Tarik 8'_ (custom discount)\n\n"
            "Or type *none* if sold out."
        )
        return

    # Build marketplace closing stock
    closing_stock = []
    lines = []
    for item_name, qty in parsed.items():
        menu_entry = next((m for m in restaurant.get("menu",[]) if m["item"].lower() == item_name.lower()), None)
        orig = menu_entry.get("profit_margin_rm", 3.0) if menu_entry else 5.0
        disc_price = round(orig * (1 - disc_pct / 100), 2)
        closing_stock.append({
            "item": item_name,
            "qty_available": qty,
            "original_price_rm": orig,
            "discounted_price_rm": disc_price,
            "discount_pct": disc_pct,
            "source": "shopkeeper",
        })
        lines.append(f"• {item_name}: {qty} portions → RM {disc_price:.2f} ({disc_pct}% off)")

    if marketplace_enabled:
        restaurant["closing_stock"]            = closing_stock
        restaurant["closing_stock_date"]       = today_str
        restaurant["closing_stock_time"]       = datetime.datetime.now().strftime("%H:%M")
        restaurant["pre_closing_stock"]        = {s["item"]: s["qty_available"] for s in closing_stock}
        restaurant["pre_closing_discount_pct"] = disc_pct

    restaurant.pop(f"awaiting_pre_closing_inventory_{today_str}", None)
    save_database(db)

    total = sum(s["qty_available"] for s in closing_stock)
    src_note = f" (your custom {disc_pct}% set)" if custom_pct else f" (your default {disc_pct}%)"
    await _send(client, chat_id,
        f"🛍️ *Marketplace Updated!*\n\n"
        f"📦 *{total} portions* now live at {disc_pct}% off{src_note}:\n" +
        "\n".join(lines) +
        "\n\n⏰ _At closing time, I'll ask what's actually unsold — that data improves tomorrow's forecast!_"
    )


# ── Stage 2 handler: post-closing final leftover ───────────────────────────────

async def _handle_post_closing_reply(
    client: httpx.AsyncClient,
    chat_id: int,
    text: str,
    restaurant: dict,
    db: dict,
    today_str: str,
) -> None:
    """
    Shopkeeper replied after closing: how much is actually unsold.
    Records: full-price sold, discount sold, zero-profit waste.
    Saves to daily_records for AI learning.
    """
    from services.inventory import record_post_closing_learning, format_post_closing_telegram

    tl = text.strip().lower()
    menu_map = {m["item"].lower(): m for m in restaurant.get("menu", [])}
    pre_stock = restaurant.get("pre_closing_stock", {})

    # All sold out
    sold_out_words = {"none","nothing","0","zero","all sold","habis","sold out","kosong","tiada","no stock","all gone"}
    if tl in sold_out_words or all(w in tl for w in ["all","sold"]):
        leftover = {item: 0 for item in pre_stock}
    else:
        parsed, _ = _parse_stock_reply(text, menu_map)
        if not parsed and pre_stock:
            await _send(client, chat_id,
                "⚠️ Couldn't read that. Reply like:\n_'Nasi Lemak 3, Teh Tarik 0'_\n\nOr 'none' if everything was sold!"
            )
            return
        leftover = {item: parsed.get(item, parsed.get(item.lower(), 0)) for item in pre_stock}
        # Also add any new items mentioned that weren't in pre_closing_stock
        for item_name, qty in parsed.items():
            if item_name not in leftover:
                leftover[item_name] = qty

    # Record and compute analysis
    analysis = record_post_closing_learning(restaurant, leftover)
    restaurant.pop(f"awaiting_post_closing_inventory_{today_str}", None)
    save_database(db)

    # Send beautiful report
    report = format_post_closing_telegram(restaurant, analysis)
    await _send(client, chat_id, report)


# ── Main message handler ───────────────────────────────────────────────────────

async def handle_text(client: httpx.AsyncClient, chat_id: int, text: str, username: str = "") -> None:

    state = _get_state(chat_id)
    tl    = text.strip().lower()


    # Registration flow states
    if state == "reg_name":
        if len(text.strip()) < 2:
            await _send(client, chat_id, "Restaurant name must be at least 2 characters. Try again:")
            return
        _set_data(chat_id, reg_name=text.strip())
        await _reg_step_owner(client, chat_id)
        return

    if state == "reg_owner":
        _set_data(chat_id, reg_owner=text.strip())
        await _reg_step_type(client, chat_id)
        return

    if state == "reg_type":
        _set_data(chat_id, reg_type=text.strip())
        await _reg_step_region(client, chat_id)
        return

    if state == "reg_region":
        if len(text.strip()) < 3:
            await _send(client, chat_id, "Please enter a valid area or city name:")
            return
        _set_data(chat_id, reg_region=text.strip())
        await _reg_step_closing_time(client, chat_id)
        return

    if state == "reg_closing_time":
        # Accept "🕐 21:00 (9 PM)" format or plain "21:00"
        import re as _re2
        match = _re2.search(r'(\d{1,2}:\d{2})', text.strip())
        if not match:
            await _send(client, chat_id, "Please enter a valid time (e.g. *21:00* or *9:30 PM*).")
            return
        closing_time = match.group(1).zfill(5)  # ensure HH:MM
        _set_data(chat_id, reg_closing_time=closing_time)
        await _reg_confirm(client, chat_id)
        return

    if state == "choosing_photo_type":
        if any(w in tl for w in ("sales", "receipt", "sold", "whiteboard", "jualan")):
            photo_data = _get_data(chat_id).get("pending_photo_data")
            if photo_data:
                await _process_photo(client, chat_id, photo_data, "sales")
            else:
                await _send(client, chat_id, "Photo expired. Please send it again.")
                _set_state(chat_id, None)
        elif any(w in tl for w in ("inventory", "stok", "shelf", "stock", "ingredients", "scan")):
            photo_data = _get_data(chat_id).get("pending_photo_data")
            if photo_data:
                # CV inventory scan
                rest_id = _get_rest_id(chat_id)
                db      = load_database()
                rest    = _get_restaurant(db, rest_id) if rest_id else None
                if rest and photo_data:
                    await _typing(client, chat_id)
                    try:
                        from services.computer_vision_inventory import scan_inventory_from_image
                        result  = scan_inventory_from_image(photo_data, rest)
                        ingr    = result.get("detected_ingredients", [])
                        summary = result.get("summary", "")
                        lines   = ["📦 *CV Inventory Scan Result*\n"]
                        if ingr:
                            for ing in ingr:
                                name = ing.get("name") or ing.get("ingredient") or str(ing)
                                qty  = f" — {ing['quantity']} {ing.get('unit','')}" if isinstance(ing, dict) and ing.get("quantity") else ""
                                lines.append(f"• {name}{qty}")
                        else:
                            lines.append("No ingredients detected. Try a clearer photo.")
                        if summary:
                            lines.append(f"\n💡 {summary}")
                        await _send(client, chat_id, "\n".join(lines))
                    except Exception as e:
                        await _send(client, chat_id, f"❌ Scan error: {e}")
                else:
                    await _send(client, chat_id, "Please login first, then send the photo again.")
                _set_state(chat_id, None)
            else:
                await _send(client, chat_id, "Photo expired. Please send it again.")
                _set_state(chat_id, None)
        elif any(w in tl for w in ("menu", "add", "board", "chalk", "new item")):
            photo_data = _get_data(chat_id).get("pending_photo_data")
            if photo_data:
                await _process_photo(client, chat_id, photo_data, "menu")
            else:
                await _send(client, chat_id, "Photo expired. Please send it again.")
                _set_state(chat_id, None)
        else:
            kb = _inline_keyboard([
                [{"text": "📊 Sales / Receipt",       "callback_data": "photo:sales"},
                 {"text": "📋 Menu Board / New Items", "callback_data": "photo:menu"}],
                [{"text": "📦 Inventory Scan (CV AI)", "callback_data": "photo:inventory"}],
            ])
            await _send(client, chat_id,
                "Please tap one of the buttons to tell me what type of photo this is.",
                reply_markup=kb)
        return

    if state == "choosing_csv_type":
        if any(w in tl for w in ("sales", "sold", "sell", "jualan")):
            await _process_csv(client, chat_id, "sales")
        elif any(w in tl for w in ("add", "new", "tambah", "append")):
            await _process_csv(client, chat_id, "add_menu")
        elif any(w in tl for w in ("replace", "overwrite", "full", "ganti")):
            await _process_csv(client, chat_id, "replace_menu")
        else:
            kb = _inline_keyboard([
                [{"text": "📊 Sales Data",        "callback_data": "csv:sales"},
                 {"text": "➕ Add Menu Items",    "callback_data": "csv:add_menu"}],
                [{"text": "🔄 Replace Full Menu", "callback_data": "csv:replace_menu"}],
            ])
            await _send(client, chat_id, "Please choose the data type:", reply_markup=kb)
        return

    if state == "bot_login_email":
        await _handle_bot_login_email(client, chat_id, text.strip())
        return

    if state == "bot_login_otp":
        email = _get_data(chat_id).get("login_email", "")
        if auth.verify_otp(email, text.strip(), "bot_login"):
            account = auth.get_account_by_email(email)
            if account:
                _set_data(chat_id, restaurant_id=account["restaurant_id"])
                _set_state(chat_id, None)
                await _send(client, chat_id,
                    f"✅ Logged in as *{email}*!\n\n"
                    "Say 'forecast' to get today's numbers.",
                    reply_markup={"remove_keyboard": True})
                await _typing(client, chat_id)
                forecast = _do_generate_forecast(account["restaurant_id"])
                await _send(client, chat_id, forecast)
            else:
                _set_state(chat_id, None)
                await _send(client, chat_id, "Something went wrong. Please try again.")
        else:
            await _send(client, chat_id,
                "❌ Wrong or expired OTP. Type *login* to try again.")
            _set_state(chat_id, None)
        return

    if state == "bot_awaiting_approval":
        await _send(client, chat_id,
            "⏳ Waiting for approval from your primary account. "
            "They will see a message to approve or deny your login.")
        return

    # BOM definition state
    if state and state.startswith("bom_item:"):
        item_name = state.split(":", 1)[1]
        rest_id   = _get_rest_id(chat_id)
        db2       = load_database()
        rest2     = _get_restaurant(db2, rest_id) if rest_id else None
        if rest2:
            region   = rest2.get("region", "Malaysia")
            rest_type = rest2.get("type", "hawker")
            bom = ask_bom_conversational(item_name, region, rest_type, text.strip())
            if bom:
                rest2.setdefault("bom", {})[item_name] = bom
                save_database(db2)
                cost = bom.get("cost_rm", "?")
                ingr = [f"{k}: {v}" for k, v in bom.items() if k != "cost_rm"]
                await _send(client, chat_id,
                    f"✅ Ingredients saved for *{item_name}*:\n"
                    + "\n".join(f"  • {i}" for i in ingr)
                    + f"\n  • Cost: RM {cost} per serving\n\n"
                    "Your shopping list will now be accurate for this item."
                )
            else:
                await _send(client, chat_id,
                    "⚠️ Could not parse ingredients. Try: _'200g rice, 50ml coconut milk, 1 egg'_")
        _set_state(chat_id, None)
        return

    if state == "choosing_restaurant":
        db      = load_database()
        rest_id = next(
            (r["id"] for r in db.get("restaurants", [])
             if r["name"].lower() in text.lower() or r["id"] in text),
            None
        )
        if text.strip().lower() in ("register", "➕ register my restaurant"):
            await start_registration(client, chat_id)
            return
        if not rest_id:
            await _send(client, chat_id,
                "Could not find that restaurant. Please pick from the list or tap *➕ Register my restaurant*.",
                reply_markup=_rest_keyboard())
            return
        _set_data(chat_id, restaurant_id=rest_id)
        db_rest = _get_restaurant(db, rest_id)
        if db_rest and not db_rest.get("telegram_chat_id"):
            db_rest["telegram_chat_id"] = chat_id
            save_database(db)
        _set_state(chat_id, None)
        name = db_rest["name"] if db_rest else rest_id
        await _send(client, chat_id,
            f"✅ Logged in as *{name}*!\n\nSay 'forecast' to get today's numbers.",
            reply_markup={"remove_keyboard": True})
        await _typing(client, chat_id)
        forecast = _do_generate_forecast(rest_id)
        await _send(client, chat_id, forecast)
        return

    # Check if user is trying to define BOM for an item
    # e.g. "set ingredients for Nasi Lemak: 200g rice, 50ml coconut milk"
    if "ingredients for" in tl or "recipe for" in tl or ("contain" in tl and "g" in tl):
        rest_id = _get_rest_id(chat_id)
        if rest_id:
            db   = load_database()
            rest = _get_restaurant(db, rest_id)
            if rest:
                for item in rest.get("menu", []):
                    if item["item"].lower() in tl:
                        remainder = text
                        for sep in [":", "-", "is", "contains", "has"]:
                            if sep in remainder.lower():
                                idx = remainder.lower().index(sep)
                                remainder = remainder[idx+len(sep):].strip()
                                break
                        await _set_bom_for_item(client, chat_id, item["item"], remainder)
                        return

    # ── Two-Stage Closing Inventory reply handlers ────────────────────────────
    rest_id_check = _get_rest_id(chat_id)
    if rest_id_check:
        db_check = load_database()
        rest_check = _get_restaurant(db_check, rest_id_check)
        today_str_check = datetime.date.today().isoformat()
        if rest_check:
            # Stage 1: 2hrs before closing — shopkeeper reports pre-closing stock
            if rest_check.get(f"awaiting_pre_closing_inventory_{today_str_check}"):
                await _handle_pre_closing_reply(client, chat_id, text, rest_check, db_check, today_str_check)
                return
            # Stage 2: at closing — shopkeeper reports final unsold leftover
            if rest_check.get(f"awaiting_post_closing_inventory_{today_str_check}"):
                await _handle_post_closing_reply(client, chat_id, text, rest_check, db_check, today_str_check)
                return

    # ── Order pickup confirmation: done/miss commands ─────────────────────────
    # Accepts: done ord_xxx | miss ord_xxx | collected ord_xxx | missed ord_xxx
    _order_cmd_map = {"done": "completed", "collected": "completed",
                      "miss": "missed", "missed": "missed"}
    tl_parts = tl.strip().split()
    if len(tl_parts) >= 2 and tl_parts[0].lstrip("/") in _order_cmd_map:
        cmd       = tl_parts[0].lstrip("/")
        order_ref = tl_parts[1].lower().lstrip("#")
        new_status = _order_cmd_map[cmd]
        rest_id    = _get_rest_id(chat_id)
        if rest_id:
            from services.nlp import load_database, save_database
            db_ord  = load_database()
            rest_ord = _get_restaurant(db_ord, rest_id)
            matched_order = None
            today_str_ord = datetime.date.today().isoformat()
            if rest_ord:
                for o in rest_ord.get("marketplace_orders", []):
                    oid = o.get("order_id", "").lower()
                    # Match by order number (e.g. '5') OR by partial order_id
                    match_num = (o.get("date") == today_str_ord and str(o.get("order_num", "")) == order_ref)
                    match_id  = oid == order_ref or oid.endswith(order_ref) or order_ref in oid
                    if match_num or match_id:
                        matched_order = o
                        break
            if matched_order:
                matched_order["status"]       = new_status
                matched_order["confirmed_at"] = datetime.datetime.now().isoformat()
                matched_order["confirmed_by"] = "telegram"
                save_database(db_ord)
                order_label = f"Order #{matched_order.get('order_num', matched_order['order_id'])}"
                if new_status == "completed":
                    await _send(client, chat_id,
                        f"✅ *{order_label} confirmed as collected!*\n\n"
                        f"Customer: *{matched_order.get('customer_name','Customer')}*\n"
                        f"💰 Revenue RM {matched_order.get('total_rm',0):.2f} recorded."
                    )
                else:
                    await _send(client, chat_id,
                        f"❌ *{order_label} marked as missed.*\n\n"
                        f"Customer: *{matched_order.get('customer_name','Customer')}*\n"
                        f"📦 Inventory released. No-show recorded for AI learning."
                    )
            else:
                await _send(client, chat_id,
                    f"⚠️ Order `{order_ref}` not found for today. "
                    "Check the order number and try again.")
            return

    await handle_natural_language(client, chat_id, text, username=username)




async def handle_natural_language(client: httpx.AsyncClient, chat_id: int, text: str, username: str = "") -> None:
    # Check if this chat_id is already linked to an account
    linked_account = auth.get_account_by_telegram(chat_id)
    if linked_account and not _get_rest_id(chat_id):
        rest_id = linked_account.get("restaurant_id")
        if rest_id:
            _set_data(chat_id, restaurant_id=rest_id)

    rest_id = _get_rest_id(chat_id)
    if not rest_id:
        tl = text.strip().lower()
        # Check if this user has a pending web registration
        if username:
            db_check = load_database()
            now_str  = datetime.datetime.utcnow().isoformat()
            pending  = next((
                pr for pr in db_check.get("pending_registrations", [])
                if pr.get("telegram_username","").lower() == username.lower().lstrip("@")
                and pr.get("expires_at","") > now_str
            ), None)
            if pending:
                import uuid as _uuid2
                rest_data = pending.get("restaurant_data", {})
                new_rest  = rest_data.get("new_rest", {})
                region    = rest_data.get("region", "")
                new_rest["telegram_chat_id"]  = chat_id
                new_rest["telegram_username"] = username.lstrip("@")
                email = pending.get("email", "")
                db2   = load_database()
                db2.setdefault("restaurants", []).append(new_rest)
                if region and region not in db2.get("regions", {}):
                    db2.setdefault("regions", {})[region] = {"type":"General Area","foot_traffic_baseline":500,
                        "weekend_multiplier":1.1,"holiday_multiplier":1.0,"rain_impact":-0.2}
                db2["pending_registrations"] = [p for p in db2.get("pending_registrations",[])
                                                 if p.get("email") != email]
                save_database(db2)
                try:
                    from services import auth as _auth2
                    _auth2.create_account(email, new_rest["id"], chat_id, username.lstrip("@"))
                    _set_data(chat_id, restaurant_id=new_rest["id"])
                    await _send(client, chat_id,
                        f"✅ *Welcome to WasteWise AI!*\n\n"
                        f"Your account for *{new_rest.get('name','your restaurant')}* is now active.\n"
                        "Your web dashboard will update automatically.\n\n"
                        "Just talk to me naturally — tell me your sales, ask for a forecast, or add your menu."
                    )
                    return
                except Exception as e:
                    await _send(client, chat_id, f"⚠️ Could not complete registration: {e}")
                    return

        if any(w in tl for w in ("login", "log in", "sign in")):
            await _start_bot_login(client, chat_id)
            return

        await _send(client, chat_id,
            "👋 Welcome to *WasteWise AI*!\n\n"
            "I help Malaysian restaurants reduce food waste through AI forecasting.\n\n"
            "• Type *register* to set up your restaurant\n"
            "• Type *login* to sign in with your email")
        return

    db   = load_database()
    rest = _get_restaurant(db, rest_id)
    if not rest:
        await _send(client, chat_id, "Please type *login* to reconnect.")
        return

    tl = text.strip().lower()

    if tl in GREETINGS:
        now = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
        await _send(client, chat_id,
            f"👋 Hey! WasteWise AI here, managing *{rest['name']}*.\n\n"
            "Just talk to me naturally! For example:\n\n"
            "• *Forecast* — _'What should I prepare today?'_\n"
            "• *Sales* — _'Sold 95 Nasi Lemak today'_\n"
            "• *Events* — _'Tomorrow wedding with 300 guests'_\n"
            "• *Menu* — _'Add Milo Ais to menu'_ or _'Show my menu'_\n"
            "• *Ingredients* — _'Set ingredients for Nasi Lemak: 200g rice, 50ml coconut milk'_\n"
            "• *Photo* — Send a photo of your receipt or whiteboard\n"
            "• *File* — Send a CSV or Excel file\n"
            "• *Sessions* — _'Who is logged in?'_ or `security`\n"
            "• *Orders* — _'done 5'_ or _'miss 3'_ to confirm/miss orders\n"
            "• *Chain* — _'create chain'_ or _'show my chains'_\n\n"
            f"📅 {now}"
        )
        return

    # Check for direct commands before intent detection
    if tl in ("inventory", "/inventory", "stok", "remaining", "baki", "sisa"):
        from services.inventory import compute_remaining_inventory
        remaining = compute_remaining_inventory(rest)
        lines = [f"📦 *Remaining Inventory — {rest['name']}*\n"]
        total_remaining = 0
        discount_pct = rest.get("discount_pct", 30)
        for item in remaining:
            if item["remaining"] > 0:
                disc = round(item['profit_margin_rm'] * (1 - discount_pct / 100), 2)
                lines.append(f"• {item['item']}: *{item['remaining']}* portions (RM {disc:.2f} at {discount_pct}% off)")
                total_remaining += item["remaining"]
        if total_remaining == 0:
            lines.append("✅ All sold out — great day!")
        else:
            lines.append(f"\nTotal: *{total_remaining}* portions at {discount_pct}% discount")
        await _send(client, chat_id, "\n".join(lines))
        return

    if tl in ("orders", "/orders", "pesanan"):
        today_str2 = datetime.date.today().isoformat()
        orders2 = [o for o in rest.get("marketplace_orders", []) if o.get("date") == today_str2]
        if not orders2:
            await _send(client, chat_id, f"📋 No marketplace orders for today yet.")
        else:
            revenue2 = sum(o["total_rm"] for o in orders2 if o.get("status") != "cancelled")
            lines2 = [f"🛍️ *Today's Orders — {rest['name']}*\n"]
            for o in orders2[-10:]:
                status_emoji = {"pending": "⏳", "completed": "✅", "cancelled": "❌"}.get(o["status"], "❓")
                items_str = ", ".join(f"{oi['qty']}x{oi['item']}" for oi in o.get("items", []))
                lines2.append(f"{status_emoji} {o['customer_name']} — {items_str} (RM {o['total_rm']:.2f})")
            lines2.append(f"\n💰 Revenue: *RM {revenue2:.2f}* | Your share: *RM {revenue2*0.90:.2f}*")
            await _send(client, chat_id, "\n".join(lines2))
        return

    if tl in ("sales", "/sales", "jualan", "pendapatan", "profit"):
        from services.inventory import get_today_profit_summary
        s = get_today_profit_summary(rest)
        lines3 = [
            f"💰 *Today's Sales — {rest['name']}*\n",
            f"📊 Regular sales: RM {s['regular_sales_rm']:.2f}",
            f"🛍️ Marketplace: RM {s['marketplace_revenue_rm']:.2f}",
            f"🏦 Your earnings: *RM {s['shopkeeper_earnings_rm']:.2f}*",
            f"📱 Platform fee: RM {s['platform_fee_rm']:.2f}",
            f"📦 Orders: {s['total_orders']}",
        ]
        await _send(client, chat_id, "\n".join(lines3))
        return

    await _typing(client, chat_id)
    intent_list = detect_intent(text, rest)   # Now returns list of intent objects
    if not isinstance(intent_list, list):
        intent_list = [intent_list]            # Backward-compat guard

    # ── Multi-intent: pre-process actionable intents from ALL items ───────────
    # order_confirm, order_miss, update_discount can appear in ANY position.
    # We handle them all upfront, then dispatch the primary remaining intent.
    side_replies = []
    remaining_intents = []
    for _idata in intent_list:
        _intent = _idata.get("intent", "general")

        if _intent == "order_confirm":
            _oid = (_idata.get("order_id") or "").lower().strip()
            _matched = None
            for _o in rest.get("marketplace_orders", []):
                _oid_db = _o.get("order_id", "").lower()
                if _oid_db == _oid or _oid_db.endswith(_oid) or _oid in _oid_db:
                    _matched = _o; break
            if _matched:
                _confirmed_at = datetime.datetime.now().isoformat()
                # Load fresh DB, find order, update, save — single clean operation
                _db_oc = load_database()
                _rest_oc = _get_restaurant(_db_oc, rest_id)
                if _rest_oc:
                    for _o2 in _rest_oc.get("marketplace_orders", []):
                        if _o2.get("order_id") == _matched["order_id"]:
                            _o2["status"]       = "completed"
                            _o2["confirmed_at"] = _confirmed_at
                            _o2["confirmed_by"] = "nlp"
                    save_database(_db_oc)
                _matched["status"] = "completed"   # keep local ref in sync
                side_replies.append(
                    f"\u2705 Order `{_matched['order_id']}` from "
                    f"*{_matched.get('customer_name','Customer')}* marked *collected*. Revenue recorded!"
                )
            else:
                side_replies.append(f"\u26a0\ufe0f Order `{_oid or '?'}` not found.")

        elif _intent == "order_miss":
            _oid = (_idata.get("order_id") or "").lower().strip()
            _matched = None
            _db_m = load_database(); _rest_m = _get_restaurant(_db_m, rest_id)
            if _rest_m:
                for _o in _rest_m.get("marketplace_orders", []):
                    _oid_db = _o.get("order_id", "").lower()
                    if _oid_db == _oid or _oid_db.endswith(_oid) or _oid in _oid_db:
                        _matched = _o; break
            if _matched:
                _matched["status"]       = "missed"
                _matched["confirmed_at"] = datetime.datetime.now().isoformat()
                _matched["confirmed_by"] = "nlp"
                save_database(_db_m)
                side_replies.append(
                    f"\u274c Order `{_matched['order_id']}` from "
                    f"*{_matched.get('customer_name','Customer')}* marked *not picked up*. "
                    f"Inventory released."
                )
            else:
                side_replies.append(f"\u26a0\ufe0f Order `{_oid or '?'}` not found.")

        elif _intent == "update_discount":
            _item_name = _idata.get("item")
            _disc_pct  = _idata.get("discount_pct")
            if _disc_pct is not None:
                _disc_pct = max(0, min(70, int(_disc_pct)))
                _db_d = load_database(); _rest_d = _get_restaurant(_db_d, rest_id)
                if _rest_d:
                    if _item_name:
                        # Item-specific discount
                        _listings = _rest_d.setdefault("marketplace_listings", {})
                        _cfg = _listings.get(_item_name, {})
                        _cfg["discount_pct"] = _disc_pct
                        _listings[_item_name] = _cfg
                        side_replies.append(
                            f"\ud83c\udff7\ufe0f *{_item_name}* discount set to *{_disc_pct}% off*."
                        )
                    else:
                        # Global discount
                        _rest_d["discount_pct"] = _disc_pct
                        side_replies.append(
                            f"\ud83c\udff7\ufe0f Global closing discount set to *{_disc_pct}% off* for all items."
                        )
                    save_database(_db_d)
            else:
                side_replies.append("\u26a0\ufe0f Could not read discount percentage.")

        else:
            remaining_intents.append(_idata)   # Will be handled by main if/elif below

    # Send all side-intent replies first
    if side_replies:
        await _send(client, chat_id, "\n".join(side_replies))

    # If ALL intents were handled above, we're done
    if not remaining_intents:
        return

    # Use the first remaining intent for the main handler
    intent_data = remaining_intents[0]
    intent      = intent_data.get("intent", "general")

    if intent == "greeting":
        now = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
        await _send(client, chat_id, f"👋 WasteWise AI for *{rest['name']}*. 📅 {now}")

    elif tl in ("inventory", "/inventory", "stok", "remaining", "baki", "sisa"):
        from services.inventory import compute_remaining_inventory
        remaining = compute_remaining_inventory(rest)
        lines = [f"📦 *Remaining Inventory — {rest['name']}*\n"]
        total_remaining = 0
        discount_pct = rest.get("discount_pct", 30)
        for item in remaining:
            if item["remaining"] > 0:
                disc = round(item['profit_margin_rm'] * (1 - discount_pct / 100), 2)
                lines.append(f"• {item['item']}: *{item['remaining']}* portions (RM {disc:.2f} at {discount_pct}% off)")
                total_remaining += item["remaining"]
        if total_remaining == 0:
            lines.append("✅ All sold out — great day!")
        else:
            lines.append(f"\nTotal: *{total_remaining}* portions available at {discount_pct}% discount")
        await _send(client, chat_id, "\n".join(lines))

    elif tl in ("orders", "/orders", "pesanan"):
        today_str = datetime.date.today().isoformat()
        orders = [o for o in rest.get("marketplace_orders", []) if o.get("date") == today_str]
        if not orders:
            await _send(client, chat_id, f"📋 No marketplace orders for today yet.\n\nShare your store link to get orders!")
        else:
            revenue = sum(o["total_rm"] for o in orders if o.get("status") != "cancelled")
            lines = [f"🛍️ *Today's Orders — {rest['name']}*\n"]
            for o in orders[-10:]:
                status_emoji = {"pending": "⏳", "completed": "✅", "cancelled": "❌"}.get(o["status"], "❓")
                items_str = ", ".join(f"{oi['qty']}x{oi['item']}" for oi in o.get("items", []))
                lines.append(f"{status_emoji} {o['customer_name']} — {items_str} (RM {o['total_rm']:.2f})")
            lines.append(f"\n💰 Total revenue: *RM {revenue:.2f}*")
            lines.append(f"🏦 Your share (90%): *RM {revenue*0.90:.2f}*")
            await _send(client, chat_id, "\n".join(lines))

    elif tl in ("sales", "/sales", "jualan", "pendapatan", "profit"):
        from services.inventory import get_today_profit_summary, get_weekly_profit_data
        today_summary = get_today_profit_summary(rest)
        lines = [
            f"💰 *Today's Sales — {rest['name']}*\n",
            f"📊 Regular sales: RM {today_summary['regular_sales_rm']:.2f}",
            f"🛍️ Marketplace orders: RM {today_summary['marketplace_revenue_rm']:.2f}",
            f"🏦 Your earnings: *RM {today_summary['shopkeeper_earnings_rm']:.2f}*",
            f"📱 Platform fee: RM {today_summary['platform_fee_rm']:.2f}",
            f"📦 Orders today: {today_summary['total_orders']}",
        ]
        await _send(client, chat_id, "\n".join(lines))

    elif intent == "forecast":
        forecast = _do_generate_forecast(rest_id)
        await _send(client, chat_id, forecast)

    elif intent == "menu_show":
        if not rest.get("menu"):
            await _send(client, chat_id,
                "No menu items yet!\n\n"
                "Tell me what you sell: _'I sell Nasi Lemak, Teh Tarik, and Kuih'_\n"
                "Or use *set ingredients for [item]: [ingredients]* to define ingredient ratios.")
            return
        lines = [f"📋 *{rest['name']} — Menu*\n"]
        has_bom = rest.get("bom", {})
        for m in rest["menu"]:
            bom_note = " ✅" if m["item"] in has_bom else " _(no ingredients set)_"
            lines.append(f"• {m['item']} — RM {m['profit_margin_rm']:.2f} margin{bom_note}")
        if not has_bom:
            lines.append("\n_Tip: Set ingredient ratios for accurate shopping lists._\n"
                         "_Example: 'Set ingredients for Nasi Lemak: 200g rice, 50ml coconut milk'_")
        await _send(client, chat_id, "\n".join(lines))

    elif intent == "login":
        await _send(client, chat_id, "Select your restaurant:", reply_markup=_rest_keyboard())
        _set_state(chat_id, "choosing_restaurant")

    if tl in ("help", "/help"):
        await _send(client, chat_id,
            "🌿 *WasteWise AI — Help*\n\n"
            "Just talk normally in English or Malay!\n\n"
            "📊 *Forecast* — _'What to cook today?'_, _'How many Roti Canai?'_\n"
            "📋 *Menu* — _'Show menu'_, _'Add Milo Ais'_, _'Remove Ice Cream'_\n"
            "📈 *Sales* — _'Sold 95 Nasi Lemak today'_\n"
            "🎉 *Events* — _'Wedding tomorrow 300 guests'_\n"
            "🥘 *Ingredients* — _'Set ingredients for Roti Canai: 120g flour, 20g ghee'_\n"
            "📦 *Inventory* — `inventory` or `stok`\n"
            "🛍️ *Orders* — `orders` — today's orders\n"
            "✅ *Confirm order* — `done 5` or `collected 5` (today's order number)\n"
            "❌ *Miss order* — `miss 5` or `missed 5`\n"
            "💰 *Profit* — `sales` or `profit`\n\n"
            "👥 *Sessions / Devices*\n"
            "  • _'Who is logged in?'_ or `security`\n"
            "  • _'Show all devices'_ or `sessions`\n"
            "  • `/remove_xxxxxxxx` — remove a linked device\n"
            "  • `/make_primary @username` — transfer primary to another Telegram\n\n"
            "🔗 *Chain Management*\n"
            "  • `create chain My Group` — create a restaurant chain\n"
            "  • `my chains` — list your chains\n"
            "  • `add to chain chain_xxxx` — link this restaurant to a chain\n\n"
            "🗑️ *Delete Account*\n"
            "  • `/delete_account` — starts the deletion flow\n\n"
            "🔑 *login* — Switch restaurant\n"
            "📝 *register* — Add a new restaurant"
        )
        return

    elif intent == "event":
        description = intent_data.get("description") or "Special event"
        headcount   = max(1, min(100_000, intent_data.get("headcount") or 50))
        days        = max(1, min(30, intent_data.get("days") or 1))
        summary     = intent_data.get("summary", description)
        register_owner_event(rest_id, description, headcount, days)
        await _typing(client, chat_id)
        forecast = _do_generate_forecast(rest_id)
        await _send(client, chat_id, f"✅ Event registered: {summary}\n\n{forecast}")

    elif intent == "sales":
        result = process_ai_data_ingestion(rest_id, text, menu_mode="none")
        await _send(client, chat_id, f"📊 {result}")
        await asyncio.sleep(3)
        await _typing(client, chat_id)
        forecast = _do_generate_forecast(rest_id)
        await _send(client, chat_id, f"🔄 *Updated forecast:*\n\n{forecast}")

    elif intent == "menu_add":
        items_before = {m["item"] for m in rest.get("menu", [])}
        result       = process_ai_data_ingestion(rest_id, text, menu_mode="append")
        db2          = load_database()
        rest2        = _get_restaurant(db2, rest_id)
        items_after  = {m["item"] for m in (rest2.get("menu", []) if rest2 else [])}
        newly_added  = list(items_after - items_before)
        _set_data(chat_id, last_action="menu_add", last_result=result, last_added_items=newly_added)
        await _send(client, chat_id, f"📋 {result}")
        if newly_added:
            await _send(client, chat_id, f"✅ Added: *{', '.join(newly_added)}*")
            # Ask about ingredients immediately for the first new item
            db3  = load_database()
            rest3 = _get_restaurant(db3, rest_id)
            if rest3 and newly_added[0] not in rest3.get("bom", {}):
                await _ask_bom_interactive(client, chat_id, newly_added[0])
                return  # State is now bom_item:xxx — wait for owner's response
        await _typing(client, chat_id)
        forecast = _do_generate_forecast(rest_id)
        await _send(client, chat_id, forecast)

    elif intent == "menu_remove":
        result = process_ai_data_ingestion(rest_id, text, menu_mode="none")
        await _send(client, chat_id, f"📋 {result}")

    elif intent == "causal_analysis":
        await _typing(client, chat_id)
        import datetime as _dt2
        yesterday = (_dt2.date.today() - _dt2.timedelta(days=1)).isoformat()
        try:
            from services.causal_ai import format_causal_report_telegram
            report = format_causal_report_telegram(rest, yesterday)
            await _send(client, chat_id, report)
        except Exception as e:
            await _send(client, chat_id, f"❌ Causal analysis error: {e}")

    elif intent == "menu_engineering":
        await _typing(client, chat_id)
        if not rest.get("menu"):
            await _send(client, chat_id, "❌ Add menu items first before I can analyse your menu.")
        else:
            try:
                from services.menu_engineering import classify_menu_items, generate_menu_recommendations
                classification  = classify_menu_items(rest)
                recommendations = generate_menu_recommendations(rest)
                emoji_map = {"star": "⭐", "ploughhorse": "🐴", "puzzle": "❓", "dog": "🐶"}
                lines = [f"🧠 *Menu Engineering — {rest['name']}*\n"]
                for item, cat in classification.items():
                    lines.append(f"{emoji_map.get(cat, '')} {item} — _{cat.capitalize()}_")
                if recommendations:
                    lines.append("\n💡 *Recommendations:*")
                    for r in recommendations[:5]:
                        lines.append(f"• {r}")
                lines.append("\n_⭐ Star = high profit + popular | 🐴 Ploughhorse = popular but low margin_")
                lines.append("_❓ Puzzle = high margin but slow | 🐶 Dog = low on both — consider removing_")
                await _send(client, chat_id, "\n".join(lines))
            except Exception as e:
                await _send(client, chat_id, f"❌ Menu analysis error: {e}")

    elif intent == "cv_inventory":
        _set_state(chat_id, "awaiting_cv_inventory_photo")
        await _send(client, chat_id,
            "📸 *Inventory Scan*\n\n"
            "Send me a photo of your ingredient shelf or storage area.\n"
            "I'll use computer vision to detect what you have and how much.\n\n"
            "_Make sure the photo is clear and well-lit for best results._")

    else:
        # Multi-intent: if there are more remaining intents after the primary one, note it
        if len(remaining_intents) > 1:
            await _send(client, chat_id,
                f"\u2139\ufe0f _{len(remaining_intents)-1} more action(s) from your message were processed above._")
        now_str      = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
        data_ctx     = _get_data(chat_id)
        last_action  = data_ctx.get("last_action", "")
        last_result  = data_ctx.get("last_result", "")
        last_added   = data_ctx.get("last_added_items", [])
        context_note = ""
        if last_action and last_result:
            context_note = f"Last action: {last_action}. Result: {last_result[:200]}\n"
        if last_added:
            context_note += f"Recently added to menu: {last_added}\n"
        prompt = (
            f"You are WasteWise AI for {rest['name']} ({rest.get('region','Malaysia')}).\n"
            f"Time: {now_str}\n"
            f"Menu ({len(rest.get('menu',[]))} items): {[m['item'] for m in rest.get('menu', [])]}\n"
            f"Recent owner notes: {[m['message'] for m in rest.get('recent_feedback_memory', [])[-3:]]}\n"
            f"{context_note}\n"
            f"Owner said: \"{text}\"\n\n"
            "Answer the specific question directly and concisely (under 80 words). "
            "If they ask what was just added, use the 'Recently added' context above — give the specific item name. "
            "If they ask about a specific item, answer about that item only. "
            "Do not dump a full list when a specific answer is expected."
        )
        reply = call_ai(prompt, json_mode=False)
        if reply:
            await _send(client, chat_id, reply)
        elif any(w in tl for w in ["time", "masa", "pukul", "jam"]):
            now = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
            await _send(client, chat_id, f"🕐 *{now}*")
        else:
            await _send(client, chat_id, "Got it! Ask me about your forecast, sales, menu, or events.")


# ── Update dispatcher ─────────────────────────────────────────────────────────

async def _start_bot_login(client: httpx.AsyncClient, chat_id: int) -> None:
    """Start the email-based login flow from Telegram."""
    _set_state(chat_id, "bot_login_email")
    await _send(client, chat_id,
        "🔑 *Login to WasteWise AI*\n\n"
        "Enter your registered email address:"
    )


async def _handle_bot_login_email(client: httpx.AsyncClient, chat_id: int, email: str) -> None:
    """Send OTP to primary Telegram UUID for login."""
    account = auth.get_account_by_email(email)
    if not account:
        await _send(client, chat_id,
            "❌ No account found for that email.\n\n"
            "Type *register* to create a new account, or try a different email."
        )
        _set_state(chat_id, None)
        return

    primary = next((s for s in account.get("sessions",[]) if s.get("is_primary") and s.get("chat_id")), None)
    if not primary:
        await _send(client, chat_id, "⚠️ Could not find your primary Telegram account to send OTP.")
        _set_state(chat_id, None)
        return

    # If this is the primary chat_id logging in, send OTP here
    # If it's a different chat_id, send approval request to primary
    is_same_as_primary = primary["chat_id"] == chat_id
    if is_same_as_primary:
        otp = auth.create_otp(email, "bot_login")
        await _send(client, chat_id,
            f"🔐 Your OTP: *{otp}*\n"
            f"Enter it here {_otp_minutes_note()}:"
        )
        _set_data(chat_id, login_email=email)
        _set_state(chat_id, "bot_login_otp")
    else:
        # New Telegram UUID — send approval request to primary
        db   = load_database()
        rest = _get_restaurant(db, account["restaurant_id"])
        username = (await _api(
            httpx.AsyncClient(timeout=10),
            "getChat", chat_id=chat_id
        )).get("result", {}).get("username", f"id:{chat_id}")

        approval_id = auth.create_approval_request(primary["chat_id"], chat_id, f"@{username}")

        kb = _inline_keyboard([
            [{"text": "✅ Approve", "callback_data": f"approve:{approval_id}"},
             {"text": "❌ Deny",    "callback_data": f"deny:{approval_id}"}]
        ])
        await _send(client, primary["chat_id"],
            f"🔔 *New login request*\n\n"
            f"@{username} wants to link to your WasteWise account ({account['email']}).\n\n"
            "Do you approve this?",
            reply_markup=kb
        )
        await _send(client, chat_id,
            "✅ Approval request sent to your primary Telegram account.\n"
            "Once approved, you'll be logged in automatically."
        )
        _set_data(chat_id, login_email=email, pending_approval_id=approval_id)
        _set_state(chat_id, "bot_awaiting_approval")


async def _handle_security_menu(client: httpx.AsyncClient, chat_id: int) -> None:
    """
    Show account security with inline tap-buttons per session.
    Primary sees: Remove button per non-primary session, Make Primary button per non-primary Telegram session.
    Non-primary sees: read-only list + message to contact primary.
    """
    account = auth.get_account_by_telegram(chat_id)
    if not account:
        await _send(client, chat_id, "Please login first. Type `login`.")
        return

    sessions = auth.get_sessions_for_account(account["email"])
    is_primary_caller = any(s.get("is_primary") and s.get("chat_id") == chat_id for s in sessions)

    # Build summary text
    lines = [f"🔐 *Security — {account['email']}*\n"]
    lines.append(f"Total sessions: *{len(sessions)}*\n")

    # Send individual message per session with inline buttons
    await _send(client, chat_id, "\n".join(lines))

    for s in sessions:
        stype = s.get("type", "web")
        sid   = s.get("session_id", "")[:8]
        is_p  = s.get("is_primary", False)
        uname = s.get("telegram_username", "")
        label = s.get("label", "")
        exp   = s.get("expires_at", "")

        if stype == "telegram":
            icon = "⭐" if is_p else "📱"
            name = f"@{uname}" if uname else f"Telegram"
        else:
            icon = "🌐"
            name = label or "Web browser"

        if is_p:
            exp_str = "never expires"
        elif exp:
            exp_str = f"exp. {exp[:10]}"
        else:
            exp_str = "no expiry set"

        session_text = (
            f"{icon} *{name}*\n"
            f"Type: {stype} · {exp_str}\n"
            f"ID: `{sid}`"
        )

        # Build inline buttons for this session
        buttons = []
        if not is_p:
            buttons.append({"text": "🗑 Remove this session", "callback_data": f"sec:remove:{sid}"})
        if stype == "telegram" and not is_p and is_primary_caller and uname:
            buttons.append({"text": "⭐ Make Primary", "callback_data": f"sec:mkprimary:{uname}"})

        kb = _inline_keyboard([buttons]) if buttons else None
        await _send(client, chat_id, session_text, reply_markup=kb)

    # Footer message
    if is_primary_caller:
        await _send(client, chat_id,
            "─────────────────────────────\n"
            "⭐ *You are the primary account.*\n"
            "• Tap ⭐ Make Primary on a Telegram session to transfer\n"
            "• Tap 🗑 Remove on any session to revoke access\n"
            "• `/delete_account` — delete this account"
        )
    else:
        await _send(client, chat_id,
            "─────────────────────────────\n"
            "_You are a secondary device._\n"
            "Contact the primary Telegram account to remove sessions or transfer primary status."
        )


async def process_update(client: httpx.AsyncClient, update: dict) -> None:
    try:
        if "callback_query" in update:
            await handle_callback(client, update["callback_query"])
            return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat_id  = msg["chat"]["id"]
        username = msg.get("from", {}).get("username", "") or ""
        if "photo" in msg:
            await handle_photo(client, chat_id, msg["photo"])
        elif "document" in msg:
            await handle_document(client, chat_id, msg["document"])
        elif "text" in msg:
            # Handle /start, /help, /login, /register without requiring the slash
            text = msg["text"]
            raw_text = text
            if text.startswith("/"):
                text = text[1:].split("@")[0]
            tl = text.strip().lower()
            if tl in ("register", "start register"):
                await start_registration(client, chat_id)
            elif any(phrase in tl for phrase in (
                "who is logged in", "who's logged in", "show devices", "show all devices",
                "security", "sessions", "my sessions", "linked accounts",
                "who logged in", "list devices", "list sessions", "show sessions",
                "who can access", "active sessions",
            )):
                await _handle_security_menu(client, chat_id)
            elif tl.startswith("make_primary ") or tl.startswith("/make_primary "):
                # /make_primary @username or make_primary username
                account = auth.get_account_by_telegram(chat_id)
                primary_s = next((s for s in (account or {}).get("sessions",[]) if s.get("is_primary") and s.get("chat_id") == chat_id), None)
                if not primary_s:
                    await _send(client, chat_id, "❌ Only the current primary account can transfer primary status.")
                else:
                    target_uname = tl.split(" ", 1)[1].lstrip("@").strip()
                    sessions = auth.get_sessions_for_account(account["email"])
                    target_s = next((s for s in sessions if s.get("telegram_username","").lower() == target_uname.lower() and s.get("type") == "telegram"), None)
                    if not target_s:
                        await _send(client, chat_id, f"❌ No Telegram session found for @{target_uname}.\n\nType `security` to see all linked sessions.")
                    elif target_s["session_id"] == primary_s["session_id"]:
                        await _send(client, chat_id, "⭐ You are already the primary.")
                    else:
                        # Transfer primary
                        db_p = load_database()
                        acc_p = next((a for a in db_p.get("accounts",[]) if a["email"].lower() == account["email"].lower()), None)
                        if acc_p:
                            for s in acc_p.get("sessions", []):
                                s["is_primary"] = (s["session_id"] == target_s["session_id"])
                            save_database(db_p)
                            await _send(client, chat_id,
                                f"✅ *Primary transferred to @{target_uname}.*\n\n"
                                f"They now hold primary status and can approve logins, transfers, and deletions."
                            )
                            # Notify new primary
                            new_chat_id = target_s.get("chat_id")
                            if new_chat_id:
                                await _send(client, new_chat_id,
                                    f"⭐ *You are now the PRIMARY account for {account['email']}!*\n\n"
                                    "You can now approve new device logins and manage account settings."
                                )
            elif tl == "delete_account" or tl == "/delete_account":
                account = auth.get_account_by_telegram(chat_id)
                primary = next((s for s in (account or {}).get("sessions",[]) if s.get("is_primary") and s.get("chat_id") == chat_id), None)
                if not primary:
                    await _send(client, chat_id, "❌ Only the primary Telegram account can delete the account.")
                else:
                    _set_state(chat_id, "confirm_delete_choice")
                    kb = _inline_keyboard([
                        [{"text": "🌿 Anonymise & keep AI data", "callback_data": "delete:keep"}],
                        [{"text": "💣 Delete everything permanently", "callback_data": "delete:hard"}],
                        [{"text": "❌ Cancel", "callback_data": "delete:cancel"}],
                    ])
                    await _send(client, chat_id,
                        "⚠️ *Delete Account — Choose how:*\n\n"
                        "🌿 *Anonymise & keep data* — Your restaurant info and PII are removed, "
                        "but anonymised sales history is kept to help improve AI for other hawkers.\n\n"
                        "💣 *Delete everything* — All data permanently erased. Cannot be undone.\n\n"
                        "Which would you prefer?",
                        reply_markup=kb
                    )
            elif tl == "yes delete my account":
                if _get_state(chat_id) in ("confirm_delete", "confirm_delete_hard"):
                    keep_data = _get_state(chat_id) == "confirm_delete"
                    account = auth.get_account_by_telegram(chat_id)
                    if account:
                        email = account["email"]
                        rest_id = account.get("restaurant_id")
                        auth.delete_account(email)
                        if rest_id:
                            db3 = load_database()
                            if keep_data:
                                rest3 = next((r for r in db3.get("restaurants",[]) if r["id"] == rest_id), None)
                                if rest3:
                                    rest3["name"] = f"[Anonymised Stall {rest_id[-4:]}]"
                                    rest3["owner_name"] = "[Anonymised]"
                                    rest3["telegram_chat_id"] = None
                                    rest3["telegram_username"] = None
                                    rest3["_anonymised"] = True
                            else:
                                db3["restaurants"] = [r for r in db3.get("restaurants",[]) if r["id"] != rest_id]
                            save_database(db3)
                        _set_state(chat_id, None)
                        _clear_data(chat_id, "restaurant_id")
                        msg_del = ("✅ Your account has been anonymised. Sales data is kept to help other hawkers. Thank you! 🌿"
                                   if keep_data else
                                   "✅ Your account and all data have been permanently deleted. Thank you for using WasteWise AI.")
                        await _send(client, chat_id, msg_del)
                    else:
                        _set_state(chat_id, None)
                        await _send(client, chat_id, "No account found to delete.")
                else:
                    _set_state(chat_id, None)
                    await _send(client, chat_id, "Delete cancelled.")

            # ── Chain Management Commands ──────────────────────────────────────────

            elif tl.startswith("create chain") or tl.startswith("/create_chain"):
                chain_name = tl.replace("create chain", "").replace("/create_chain", "").strip()
                if not chain_name:
                    await _send(client, chat_id, "Please specify a chain name.\n_Example: `create chain My Hawker Group`_")
                else:
                    account = auth.get_account_by_telegram(chat_id)
                    if not account:
                        await _send(client, chat_id, "❌ Please login first.")
                    else:
                        kb = _inline_keyboard([
                            [{"text": "✅ Confirm Create Chain", "callback_data": f"chain:create:{chain_name[:20]}"}],
                            [{"text": "❌ Cancel", "callback_data": "chain:cancel"}],
                        ])
                        await _send(client, chat_id,
                            f"🔗 *Create chain: \"{chain_name}\"?*\n\n"
                            "This will create a new restaurant chain. "
                            "You can then add your restaurants as branches.\n\n"
                            "_(No Telegram approval needed \u2014 you are already on Telegram!)_",
                            reply_markup=kb
                        )

            elif any(phrase in tl for phrase in ("my chains", "show chains", "list chains", "my chain")):
                account = auth.get_account_by_telegram(chat_id)
                if not account:
                    await _send(client, chat_id, "❌ Please login first.")
                else:
                    db_c = load_database()
                    email_c = account["email"].lower()
                    my_chains = [c for c in db_c.get("chains", []) if c.get("owner_email", "").lower() == email_c]
                    if not my_chains:
                        await _send(client, chat_id,
                            "🔗 *No chains yet.*\n\n"
                            "Type `create chain My Group Name` to create one.")
                    else:
                        lines = ["🔗 *Your Restaurant Chains*\n"]
                        for ch in my_chains:
                            cid = ch["chain_id"]
                            branches = [r["name"] for r in db_c.get("restaurants",[]) if r.get("chain_id") == cid]
                            lines.append(f"• *{ch.get('name',cid)}* ({ch.get('chain_type','franchise')})")
                            lines.append(f"  ID: `{cid}`")
                            lines.append(f"  Branches: {', '.join(branches) if branches else 'none'}")
                        await _send(client, chat_id, "\n".join(lines))

            elif tl.startswith("add to chain") or tl.startswith("/add_to_chain"):
                rest_id_c = _get_rest_id(chat_id)
                chain_ref = tl.replace("add to chain", "").replace("/add_to_chain", "").strip()
                if not rest_id_c or not chain_ref:
                    await _send(client, chat_id,
                        "Usage: `add to chain chain_xxxxxxxx`\n"
                        "Get your chain ID with `my chains`.")
                else:
                    db_c2 = load_database()
                    chain_c2 = next((c for c in db_c2.get("chains",[]) if c["chain_id"] == chain_ref), None)
                    if not chain_c2:
                        await _send(client, chat_id, f"❌ Chain `{chain_ref}` not found. Check with `my chains`.")
                    else:
                        rest_obj = next((r for r in db_c2.get("restaurants",[]) if r["id"] == rest_id_c), None)
                        if rest_obj:
                            rest_obj["chain_id"] = chain_ref
                            save_database(db_c2)
                            await _send(client, chat_id,
                                f"✅ *{rest_obj['name']}* added to chain *{chain_c2.get('name', chain_ref)}*!")
                        else:
                            await _send(client, chat_id, "❌ Could not find your restaurant.")

            # ── Approve/Deny dashboard actions ────────────────────────────────────

            elif tl.startswith("approve ") or tl.startswith("deny "):
                parts = tl.split(" ", 1)
                decision = parts[0] == "approve"
                prefix = parts[1].strip() if len(parts) > 1 else ""
                if prefix:
                    # Directly mutate the in-process pending approvals dict (same process — no HTTP needed)
                    try:
                        from main import _pending_dashboard_approvals
                        match = next(
                            ((tok, e) for tok, e in _pending_dashboard_approvals.items()
                             if tok.startswith(prefix) and e["primary_chat_id"] == chat_id),
                            None,
                        )
                        if not match:
                            await _send(client, chat_id, "⚠️ Approval request not found or already handled.")
                        else:
                            token, entry = match
                            entry["status"] = "approved" if decision else "denied"
                            emoji = "✅" if decision else "❌"
                            await _send(client, chat_id,
                                f"{emoji} Dashboard action *{entry.get('action','')}* "
                                f"{'approved' if decision else 'denied'}."
                            )
                    except Exception as _e_ap:
                        await _send(client, chat_id, f"⚠️ Could not process approval: {_e_ap}")

            elif tl.startswith("remove_") and len(tl) > 7:
                session_prefix = tl[7:]
                account = auth.get_account_by_telegram(chat_id)
                if account:
                    sessions = auth.get_sessions_for_account(account["email"])
                    target   = next((s for s in sessions if s["session_id"].startswith(session_prefix)), None)
                    if target and not target.get("is_primary"):
                        removed = auth.remove_session(account["email"], target["session_id"])
                        label   = target.get("telegram_username") or target.get("label","session")
                        await _send(client, chat_id,
                            f"✅ Removed session: @{label}" if removed else "⚠️ Could not remove that session.")
                    else:
                        await _send(client, chat_id, "❌ Cannot remove that session (not found or is primary).")

            # ── AI Feature Commands ────────────────────────────────────────────────

            elif tl in ("causal", "why", "root_cause", "analysis"):
                rest_id = _get_rest_id(chat_id)
                if not rest_id:
                    await _send(client, chat_id, "❌ Please login first.")
                else:
                    db   = load_database()
                    rest = _get_restaurant(db, rest_id)
                    if not rest:
                        await _send(client, chat_id, "Please type *login* to reconnect.")
                    else:
                        await _typing(client, chat_id)
                        import datetime as _dt
                        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
                        try:
                            from services.causal_ai import analyse_underperformance, format_causal_report_telegram
                            report = format_causal_report_telegram(rest, yesterday)
                            await _send(client, chat_id, report)
                        except Exception as e:
                            await _send(client, chat_id, f"❌ Causal analysis error: {e}")

            elif tl in ("menueng", "menu_report", "menu_analysis", "bcg"):
                rest_id = _get_rest_id(chat_id)
                if not rest_id:
                    await _send(client, chat_id, "❌ Please login first.")
                else:
                    db   = load_database()
                    rest = _get_restaurant(db, rest_id)
                    if not rest:
                        await _send(client, chat_id, "Please type *login* to reconnect.")
                    elif not rest.get("menu"):
                        await _send(client, chat_id, "❌ Add menu items first before running menu analysis.")
                    else:
                        await _typing(client, chat_id)
                        try:
                            from services.menu_engineering import classify_menu_items, generate_menu_recommendations
                            classification  = classify_menu_items(rest)
                            recommendations = generate_menu_recommendations(rest)
                            emoji_map = {"star":"⭐","ploughhorse":"🐴","puzzle":"❓","dog":"🐶"}
                            lines = [f"🧠 *Menu Engineering — {rest['name']}*\n"]
                            for item, cat in classification.items():
                                lines.append(f"{emoji_map.get(cat,'')}{item} — _{cat.capitalize()}_")
                            if recommendations:
                                lines.append("\n💡 *Recommendations:*")
                                for r in recommendations[:5]:
                                    lines.append(f"• {r}")
                            await _send(client, chat_id, "\n".join(lines))
                        except Exception as e:
                            await _send(client, chat_id, f"❌ Menu analysis error: {e}")

            elif tl in ("scan_inventory", "scan", "cv_scan", "photo_scan"):
                rest_id = _get_rest_id(chat_id)
                if not rest_id:
                    await _send(client, chat_id, "❌ Please login first.")
                else:
                    _set_state(chat_id, "awaiting_cv_inventory_photo")
                    await _send(client, chat_id,
                        "📸 *Inventory Scan*\n\n"
                        "Send me a photo of your ingredient shelf or storage area.\n"
                        "I'll detect ingredients and quantities using computer vision.\n\n"
                        "_Make sure the photo is clear and well-lit._")
            else:
                await handle_text(client, chat_id, text, username=username)
    except Exception as e:
        print(f"[Bot] Update error: {e}")
        import traceback; traceback.print_exc()


async def main() -> None:
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_TOKEN not set in .env")
        return
    print()
    print("  WasteWise AI — Telegram Bot")
    print("  Reducing food waste for Malaysian SMEs")
    print(f"  Started: {datetime.datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print()
    offset = 0
    async with httpx.AsyncClient(timeout=60) as client:
        me = await _api(client, "getMe")
        if me.get("ok"):
            print(f"  Bot: @{me['result'].get('username','unknown')}")
            print("  Listening for messages...\n")
        else:
            print(f"  ERROR: {me}")
            return
        while True:
            try:
                resp = await _api(client, "getUpdates",
                                  offset=offset, timeout=25,
                                  allowed_updates=["message","callback_query"])
                if resp.get("ok") and resp.get("result"):
                    for update in resp["result"]:
                        offset = update["update_id"] + 1
                        asyncio.create_task(process_update(client, update))
            except httpx.TimeoutException:
                pass
            except httpx.NetworkError as e:
                print(f"[Bot] Network error, retrying: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[Bot] Error: {e}")
                await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
