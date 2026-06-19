"""Billing checkout trigger handling for the Streamlit UI."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Mapping, MutableMapping, Optional

from lab_scheduler.billing.feature_gates import FeatureGates
from lab_scheduler.billing.stripe_checkout import create_billing_checkout_session
from lab_scheduler.ui.schedule_session import billing_checkout_trigger_key


def pop_billing_checkout_trigger(
    session_state: MutableMapping[str, Any],
    tenant_id: str,
) -> bool:
    return bool(session_state.pop(billing_checkout_trigger_key(tenant_id), False))


def process_billing_checkout_trigger(
    conn: sqlite3.Connection,
    session_state: MutableMapping[str, Any],
    *,
    tenant_id: str,
    tenant_slug: str,
    customer_email: Optional[str],
    gates: FeatureGates,
    link_button: Callable[..., Any],
    info: Callable[[str], None],
    error: Callable[[str], None],
) -> None:
    """Start Stripe checkout when the user clicked an upgrade CTA."""

    if gates.is_premium:
        return
    if not pop_billing_checkout_trigger(session_state, tenant_id):
        return
    try:
        _session_id, checkout_url = create_billing_checkout_session(
            conn,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            customer_email=customer_email,
        )
    except Exception as exc:  # noqa: BLE001 — surface billing errors in UI
        error(f"Could not start checkout: {exc}")
        return
    info("Complete checkout in the new tab to unlock premium exports.")
    link_button("Continue to secure checkout", checkout_url, type="primary")
