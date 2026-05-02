"""
marketplace_auth.py — Marketplace customer authentication via Supabase Auth.
This is completely separate from hawker/restaurant auth.
Customers register with email + password. Hawkers use Telegram OTP.
"""
import os
from supabase import create_client, Client
from fastapi import HTTPException

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

_supabase: Client | None = None


def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
        _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase


def register_customer(email: str, password: str, name: str, phone: str = "") -> dict:
    """Register a new marketplace customer. Raises ValueError on failure."""
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if not name.strip():
        raise ValueError("Name is required.")

    supabase = get_supabase()
    try:
        result = supabase.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"name": name.strip(), "phone": phone.strip()}
        })
    except Exception as e:
        msg = str(e).lower()
        if "already" in msg or "exists" in msg or "registered" in msg:
            raise ValueError("An account with this email already exists.")
        raise ValueError(f"Registration failed. Please try again.")

    if result.user is None:
        raise ValueError("Registration failed. Please try again.")

    # Create extended profile record
    try:
        supabase.table("customer_profiles").insert({
            "id": result.user.id,
            "name": name.strip(),
            "phone": phone.strip()
        }).execute()
    except Exception:
        pass  # Profile creation failure is non-fatal

    return {"user_id": result.user.id, "email": email, "name": name.strip()}


def login_customer(email: str, password: str) -> dict:
    """Login marketplace customer. Returns session dict or raises ValueError."""
    supabase = get_supabase()
    try:
        result = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })
    except Exception:
        raise ValueError("Invalid email or password.")

    if result.user is None or result.session is None:
        raise ValueError("Invalid email or password.")

    return {
        "access_token": result.session.access_token,
        "user_id": result.user.id,
        "email": result.user.email,
        "name": result.user.user_metadata.get("name", ""),
    }


def validate_customer_token(token: str) -> dict | None:
    """Validate marketplace customer JWT. Returns user dict or None."""
    if not token:
        return None
    supabase = get_supabase()
    try:
        result = supabase.auth.get_user(token)
        if result.user:
            return {
                "user_id": result.user.id,
                "email": result.user.email,
                "name": result.user.user_metadata.get("name", "")
            }
    except Exception:
        return None
    return None


def delete_customer_account(user_id: str, token: str) -> None:
    """Permanently delete a customer account and all their data. GDPR compliant."""
    supabase = get_supabase()

    # Security check: token must match the user requesting deletion
    user = validate_customer_token(token)
    if not user or user["user_id"] != user_id:
        raise ValueError("Unauthorized.")

    # Anonymise orders (keep for restaurant records, remove personal data)
    try:
        supabase.table("marketplace_orders").update({
            "customer_id": None,
            "order_email": "[deleted]",
            "order_name": "[deleted]"
        }).eq("customer_id", user_id).execute()
    except Exception:
        pass

    # Delete active reservations
    try:
        supabase.table("marketplace_reservations").delete().eq("customer_id", user_id).execute()
    except Exception:
        pass

    # Delete profile
    try:
        supabase.table("customer_profiles").delete().eq("id", user_id).execute()
    except Exception:
        pass

    # Delete auth user (cascades on customer_profiles via FK)
    supabase.auth.admin.delete_user(user_id)
