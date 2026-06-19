import sqlite3

from lab_scheduler.billing.feature_gates import (
    PREMIUM_MAX_WEEKS,
    TRIAL_MAX_WEEKS,
    activate_tenant_subscription,
    feature_gates_for_billing,
    fetch_tenant_billing,
)
from lab_scheduler.billing.stripe_checkout import create_billing_checkout_session, use_mock_stripe


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tenants (
          id TEXT PRIMARY KEY,
          subscription_status TEXT NOT NULL DEFAULT 'trial',
          stripe_customer_id TEXT,
          trial_ends_at TEXT
        );
        INSERT INTO tenants (id, subscription_status) VALUES
          ('tenant-a', 'active'),
          ('tenant-b', 'trial');
        """
    )
    return conn


def test_feature_gates_active_tenant() -> None:
    billing = fetch_tenant_billing(_memory_db(), "tenant-a")
    gates = feature_gates_for_billing(billing)
    assert gates.is_premium
    assert gates.can_export_compliance_report
    assert gates.can_export_breakroom
    assert gates.max_weeks_auto_generate == PREMIUM_MAX_WEEKS


def test_feature_gates_trial_tenant() -> None:
    billing = fetch_tenant_billing(_memory_db(), "tenant-b")
    gates = feature_gates_for_billing(billing)
    assert gates.is_trial_tier
    assert not gates.can_export_compliance_report
    assert not gates.can_export_breakroom
    assert gates.max_employees_auto_generate is None
    assert gates.max_weeks_auto_generate == TRIAL_MAX_WEEKS


def test_mock_checkout_session() -> None:
    conn = _memory_db()
    assert use_mock_stripe()
    session_id, url = create_billing_checkout_session(
        conn,
        tenant_id="tenant-b",
        tenant_slug="southbridge-health",
    )
    assert session_id.startswith("cs_test_mock_")
    assert "checkout.stripe.com" in url


def test_activate_subscription_accepts_customer_id() -> None:
    conn = _memory_db()
    billing = activate_tenant_subscription(
        conn,
        tenant_id="tenant-b",
        checkout_session_id="cs_test",
        stripe_customer_id="cus_live_123",
    )
    assert billing.subscription_status == "active"
    assert billing.stripe_customer_id == "cus_live_123"
