"""Self-serve tenant registration for Manitoba lab workspaces."""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .session import AuthenticatedSession, hash_password
from lab_scheduler.billing.feature_gates import ensure_billing_schema
from lab_scheduler.tenant.configuration import (
    ensure_tenant_configuration_schema,
    set_tenant_config_value,
)

from .onboarding import (
    DEFAULT_JURISDICTION,
    ONBOARDING_COMPLETE_KEY,
    seed_lab_infrastructure,
)

__all__ = [
    "SignupError",
    "register_tenant",
    "slugify_facility_name",
]

TRIAL_DAYS = 14
_MIN_PASSWORD_LEN = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupError(ValueError):
    """Raised when sign-up input is invalid or provisioning fails."""


@dataclass(frozen=True, slots=True)
class SignupRequest:
    facility_name: str
    email: str
    password: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify_facility_name(facility_name: str) -> str:
    """Return a URL-safe tenant slug from a facility display name."""

    text = facility_name.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if not text:
        raise SignupError("Facility name must contain at least one letter or number.")
    return text[:48]


def _unique_slug(conn: sqlite3.Connection, base_slug: str) -> str:
    candidate = base_slug
    suffix = 2
    while conn.execute(
        "SELECT 1 FROM tenants WHERE slug = ?",
        (candidate,),
    ).fetchone():
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
        if suffix > 999:
            candidate = f"{base_slug}-{uuid.uuid4().hex[:6]}"
            break
    return candidate


def _validate_signup_request(request: SignupRequest) -> tuple[str, str, str]:
    facility_name = request.facility_name.strip()
    email = request.email.strip().lower()
    password = request.password

    if len(facility_name) < 2:
        raise SignupError("Enter your facility or lab name (at least 2 characters).")
    if not _EMAIL_RE.match(email):
        raise SignupError("Enter a valid work email address.")
    if len(password) < _MIN_PASSWORD_LEN:
        raise SignupError(f"Password must be at least {_MIN_PASSWORD_LEN} characters.")

    return facility_name, email, password


def register_tenant(
    conn: sqlite3.Connection,
    *,
    facility_name: str,
    email: str,
    password: str,
) -> AuthenticatedSession:
    """Create a trial tenant, admin account, and Manitoba lab defaults."""

    facility_name, email, password = _validate_signup_request(
        SignupRequest(facility_name=facility_name, email=email, password=password)
    )

    existing = conn.execute(
        """
        SELECT 1 FROM tenant_user_accounts
        WHERE username = ? COLLATE NOCASE AND is_active = 1
        """,
        (email,),
    ).fetchone()
    if existing is not None:
        raise SignupError("An account with this email already exists. Sign in instead.")

    ensure_billing_schema(conn)
    ensure_tenant_configuration_schema(conn)

    tenant_id = f"tenant-{uuid.uuid4().hex[:12]}"
    slug = _unique_slug(conn, slugify_facility_name(facility_name))
    now = _utc_now_iso()
    trial_ends = (date.today() + timedelta(days=TRIAL_DAYS)).isoformat()
    display_name = f"{facility_name} Administrator"

    salt, pwd_hash = hash_password(password)
    account_id = f"acct-{uuid.uuid4().hex[:12]}"

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO tenants (
              id, name, slug, status, subscription_status,
              stripe_customer_id, trial_ends_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', 'trial', NULL, ?, ?, ?)
            """,
            (tenant_id, facility_name, slug, trial_ends, now, now),
        )
        conn.execute(
            """
            INSERT INTO tenant_user_accounts (
              id, username, password_salt, password_hash, tenant_id,
              display_name, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (account_id, email, salt, pwd_hash, tenant_id, display_name, now, now),
        )
        seed_lab_infrastructure(conn, tenant_id=tenant_id)
        set_tenant_config_value(
            conn,
            tenant_id=tenant_id,
            config_key=ONBOARDING_COMPLETE_KEY,
            config_value="0",
        )
        set_tenant_config_value(
            conn,
            tenant_id=tenant_id,
            config_key="jurisdiction",
            config_value=DEFAULT_JURISDICTION,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return AuthenticatedSession(
        account_id=account_id,
        username=email,
        tenant_id=tenant_id,
        tenant_name=facility_name,
        tenant_slug=slug,
        display_name=display_name,
    )
