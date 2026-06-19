from __future__ import annotations

import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

SubscriptionStatus = str

TRIAL_MAX_EMPLOYEES = 45
TRIAL_MAX_WEEKS = 2
PREMIUM_MAX_WEEKS = 8

PREMIUM_PRICE_DISPLAY = "$299 CAD / month"
PREMIUM_UPSELL_SHORT = "Unlock full 8-week block + breakroom export — $299/mo"


@dataclass(frozen=True, slots=True)
class TenantBilling:
    tenant_id: str
    subscription_status: SubscriptionStatus
    stripe_customer_id: Optional[str]
    trial_ends_at: Optional[date]


@dataclass(frozen=True, slots=True)
class FeatureGates:
    subscription_status: SubscriptionStatus
    max_employees_auto_generate: Optional[int]
    max_weeks_auto_generate: Optional[int]
    can_export_breakroom: bool
    can_export_compliance_report: bool
    is_trial_tier: bool
    is_premium: bool

    @property
    def trial_employee_cap(self) -> Optional[int]:
        return self.max_employees_auto_generate

    @property
    def trial_week_cap(self) -> Optional[int]:
        return self.max_weeks_auto_generate


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    return date.fromisoformat(text)


def fetch_tenant_billing(conn: sqlite3.Connection, tenant_id: str) -> TenantBilling:
    row = conn.execute(
        """
        SELECT subscription_status, stripe_customer_id, trial_ends_at
        FROM tenants
        WHERE id = ?
        """,
        (tenant_id,),
    ).fetchone()
    if row is None:
        return TenantBilling(
            tenant_id=tenant_id,
            subscription_status="trial",
            stripe_customer_id=None,
            trial_ends_at=None,
        )
    return TenantBilling(
        tenant_id=tenant_id,
        subscription_status=str(row[0] or "trial"),
        stripe_customer_id=row[1],
        trial_ends_at=_parse_date(row[2]),
    )


def feature_gates_for_billing(billing: TenantBilling) -> FeatureGates:
    if billing.subscription_status == "active":
        return FeatureGates(
            subscription_status=billing.subscription_status,
            max_employees_auto_generate=None,
            max_weeks_auto_generate=PREMIUM_MAX_WEEKS,
            can_export_breakroom=True,
            can_export_compliance_report=True,
            is_trial_tier=False,
            is_premium=True,
        )

    # trial and past_due share restricted feature set until upgraded
    return FeatureGates(
        subscription_status=billing.subscription_status,
        max_employees_auto_generate=None,
        max_weeks_auto_generate=TRIAL_MAX_WEEKS,
        can_export_breakroom=False,
        can_export_compliance_report=False,
        is_trial_tier=billing.subscription_status == "trial",
        is_premium=False,
    )


def apply_employee_cap(employees: List[dict], gates: FeatureGates) -> List[dict]:
    cap = gates.max_employees_auto_generate
    if cap is None:
        return employees
    return employees[:cap]


def trial_period_end(period_start: date, gates: FeatureGates) -> date:
    weeks = gates.max_weeks_auto_generate
    if weeks is None:
        raise ValueError("No trial week cap configured")
    return period_start + timedelta(days=weeks * 7 - 1)


def create_mock_checkout_session(*, tenant_id: str, tenant_slug: str) -> tuple[str, str]:
    """Return (checkout_session_id, checkout_url) for simulated Stripe Checkout."""

    token = secrets.token_hex(8)
    session_id = f"cs_test_mock_{tenant_slug}_{token}"
    checkout_url = f"https://checkout.stripe.com/c/pay/{session_id}?tenant={tenant_id}"
    return session_id, checkout_url


def activate_tenant_subscription(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    checkout_session_id: Optional[str] = None,
    stripe_customer_id: Optional[str] = None,
) -> TenantBilling:
    existing = fetch_tenant_billing(conn, tenant_id)
    customer_id = (
        stripe_customer_id
        or existing.stripe_customer_id
        or f"cus_mock_{uuid.uuid4().hex[:14]}"
    )

    conn.execute(
        """
        UPDATE tenants
        SET subscription_status = 'active',
            stripe_customer_id = ?,
            trial_ends_at = NULL
        WHERE id = ?
        """,
        (customer_id, tenant_id),
    )
    conn.commit()
    return fetch_tenant_billing(conn, tenant_id)


def seed_default_billing_state(
    conn: sqlite3.Connection,
    *,
    northstar_tenant_id: str,
    southbridge_tenant_id: str,
) -> None:
    conn.execute(
        """
        UPDATE tenants
        SET subscription_status = 'active',
            stripe_customer_id = COALESCE(stripe_customer_id, 'cus_mock_northstar'),
            trial_ends_at = NULL
        WHERE id = ?
        """,
        (northstar_tenant_id,),
    )
    conn.execute(
        """
        UPDATE tenants
        SET subscription_status = 'trial',
            stripe_customer_id = NULL,
            trial_ends_at = COALESCE(trial_ends_at, '2026-08-31T00:00:00Z')
        WHERE id = ?
        """,
        (southbridge_tenant_id,),
    )
    conn.commit()


def ensure_billing_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tenants)")}
    if "subscription_status" not in cols:
        conn.execute(
            "ALTER TABLE tenants ADD COLUMN subscription_status TEXT NOT NULL DEFAULT 'trial'"
        )
    if "stripe_customer_id" not in cols:
        conn.execute("ALTER TABLE tenants ADD COLUMN stripe_customer_id TEXT")
    if "trial_ends_at" not in cols:
        conn.execute("ALTER TABLE tenants ADD COLUMN trial_ends_at TEXT")
