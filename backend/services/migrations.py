"""
services/migrations.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gap 3 Fix: Database migration versioning for Supabase schema changes.

Strategy:
  - Migrations are numbered Python functions (m0001, m0002, …)
  - Applied exactly once in order, tracked in `schema_migrations` table
  - Run at startup via run_pending_migrations()
  - Safe to run multiple times (idempotent)

Usage:
  from services.migrations import run_pending_migrations
  run_pending_migrations()    # called in main.py on_startup
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import os
import datetime
import logging

log = logging.getLogger("migrations")


# =============================================================================
# MIGRATION REGISTRY
# Add new migrations at the bottom. Never edit or delete existing ones.
# =============================================================================

def m0001_create_schema_migrations_table(sb) -> None:
    """Create the migration tracking table if it doesn't exist."""
    sb.rpc("exec_sql", {"query": """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ DEFAULT NOW(),
            description TEXT
        );
    """}).execute()


def m0002_add_anonymised_flag_to_restaurants(sb) -> None:
    """Add _anonymised column to support keep-data deletion."""
    sb.rpc("exec_sql", {"query": """
        ALTER TABLE restaurants
            ADD COLUMN IF NOT EXISTS _anonymised BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS _anonymised_at TIMESTAMPTZ;
    """}).execute()


def m0003_add_email_to_restaurants(sb) -> None:
    """Add email column to restaurants for faster lookups."""
    sb.rpc("exec_sql", {"query": """
        ALTER TABLE restaurants
            ADD COLUMN IF NOT EXISTS email TEXT;
        CREATE INDEX IF NOT EXISTS idx_restaurants_email ON restaurants(email);
    """}).execute()


def m0004_create_audit_log_table(sb) -> None:
    """Create the audit_log table for Gap 4 (write audit trail)."""
    sb.rpc("exec_sql", {"query": """
        CREATE TABLE IF NOT EXISTS audit_log (
            id           BIGSERIAL PRIMARY KEY,
            ts           TIMESTAMPTZ DEFAULT NOW(),
            actor_email  TEXT,
            restaurant_id TEXT,
            action       TEXT NOT NULL,
            endpoint     TEXT,
            payload_hash TEXT,
            ip_address   TEXT,
            success      BOOLEAN DEFAULT TRUE,
            detail       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_restaurant ON audit_log(restaurant_id);
        CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts DESC);
    """}).execute()


def m0005_add_chain_name_to_chains(sb) -> None:
    """Add chain_name column for clearer chain display."""
    sb.rpc("exec_sql", {"query": """
        ALTER TABLE chains
            ADD COLUMN IF NOT EXISTS chain_name TEXT;
    """}).execute()


# ── Migration list (ordered, never reorder) ───────────────────────────────────
_MIGRATIONS = [
    ("0001", "Create schema_migrations table",           m0001_create_schema_migrations_table),
    ("0002", "Add anonymised flag to restaurants",        m0002_add_anonymised_flag_to_restaurants),
    ("0003", "Add email index to restaurants",            m0003_add_email_to_restaurants),
    ("0004", "Create audit_log table",                    m0004_create_audit_log_table),
    ("0005", "Add chain_name column to chains",           m0005_add_chain_name_to_chains),
]


# =============================================================================
# RUNNER — called once at startup
# =============================================================================

def run_pending_migrations() -> None:
    """Apply any unapplied migrations in order. Safe to call repeatedly."""
    try:
        from services.supabase_db import _sb
        if not _sb:
            log.info("Migrations skipped — Supabase not connected")
            return

        # Ensure tracking table exists first
        try:
            m0001_create_schema_migrations_table(_sb)
        except Exception:
            pass  # table may already exist; continue

        # Get already-applied versions
        try:
            resp = _sb.table("schema_migrations").select("version").execute()
            applied = {row["version"] for row in (resp.data or [])}
        except Exception:
            applied = set()

        for version, description, fn in _MIGRATIONS:
            if version in applied:
                continue
            try:
                log.info("Applying migration %s: %s", version, description)
                fn(_sb)
                _sb.table("schema_migrations").insert({
                    "version": version,
                    "description": description,
                    "applied_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }).execute()
                log.info("✅ Migration %s applied", version)
            except Exception as e:
                log.error("❌ Migration %s failed: %s", version, e)
                # Don't raise — let the app start anyway with partial migrations

    except Exception as e:
        log.error("Migration runner error: %s", e)
