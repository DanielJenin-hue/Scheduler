"""Stripe Checkout, Customer Portal, and webhook helpers."""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Optional
from urllib.parse import urlencode

from lab_scheduler.billing.feature_gates import (
    activate_tenant_subscription,
    create_mock_checkout_session,
    fetch_tenant_billing,
)

__all__ = [
    "create_billing_checkout_session",
    "create_billing_portal_session",
    "process_stripe_webhook",
    "stripe_configured",
    "use_mock_stripe",
]

STRIPE_SECRET_KEY_ENV = "STRIPE_SECRET_KEY"
STRIPE_PRICE_ID_ENV = "STRIPE_PRICE_ID"
STRIPE_WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"
APP_BASE_URL_ENV = "APP_BASE_URL"
USE_MOCK_STRIPE_ENV = "USE_MOCK_STRIPE"


def use_mock_stripe() -> bool:
    flag = os.environ.get(USE_MOCK_STRIPE_ENV, "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return not stripe_configured()


def stripe_configured() -> bool:
    return bool(
        os.environ.get(STRIPE_SECRET_KEY_ENV, "").strip()
        and os.environ.get(STRIPE_PRICE_ID_ENV, "").strip()
    )


def _stripe_client():
    import stripe

    stripe.api_key = os.environ[STRIPE_SECRET_KEY_ENV].strip()
    return stripe


def _app_base_url() -> str:
    return os.environ.get(APP_BASE_URL_ENV, "http://localhost:8501").rstrip("/")


def create_billing_checkout_session(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    tenant_slug: str,
    customer_email: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(session_id, checkout_url)`` for subscription upgrade."""

    if use_mock_stripe():
        return create_mock_checkout_session(tenant_id=tenant_id, tenant_slug=tenant_slug)

    stripe = _stripe_client()
    billing = fetch_tenant_billing(conn, tenant_id)
    base = _app_base_url()
    success_url = f"{base}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base}/?checkout=cancel"

    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": os.environ[STRIPE_PRICE_ID_ENV].strip(), "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": tenant_id,
        "metadata": {"tenant_id": tenant_id, "tenant_slug": tenant_slug},
        "subscription_data": {"metadata": {"tenant_id": tenant_id}},
    }
    if customer_email:
        params["customer_email"] = customer_email
    if billing.stripe_customer_id:
        params["customer"] = billing.stripe_customer_id
        params.pop("customer_email", None)

    session = stripe.checkout.Session.create(**params)
    return str(session.id), str(session.url)


def create_billing_portal_session(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
) -> Optional[str]:
    """Return a Stripe Customer Portal URL, or ``None`` when unavailable."""

    if use_mock_stripe():
        query = urlencode({"tenant": tenant_id, "mock": "1"})
        return f"https://billing.stripe.com/p/login/mock?{query}"

    billing = fetch_tenant_billing(conn, tenant_id)
    if not billing.stripe_customer_id:
        return None

    stripe = _stripe_client()
    session = stripe.billing_portal.Session.create(
        customer=billing.stripe_customer_id,
        return_url=_app_base_url(),
    )
    return str(session.url)


def process_stripe_webhook(
    conn: sqlite3.Connection,
    *,
    payload: bytes,
    signature_header: str,
) -> str:
    """Verify and apply Stripe webhook events. Returns a short status message."""

    if use_mock_stripe():
        raise RuntimeError("Stripe webhooks are disabled while USE_MOCK_STRIPE is enabled.")

    secret = os.environ.get(STRIPE_WEBHOOK_SECRET_ENV, "").strip()
    if not secret:
        raise RuntimeError(f"{STRIPE_WEBHOOK_SECRET_ENV} is not configured.")

    stripe = _stripe_client()
    event = stripe.Webhook.construct_event(payload, signature_header, secret)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        tenant_id = (
            (session.get("metadata") or {}).get("tenant_id")
            or session.get("client_reference_id")
        )
        if not tenant_id:
            return "ignored: missing tenant_id"
        customer_id = session.get("customer")
        activate_tenant_subscription(
            conn,
            tenant_id=str(tenant_id),
            checkout_session_id=str(session.get("id") or ""),
            stripe_customer_id=str(customer_id) if customer_id else None,
        )
        return f"activated:{tenant_id}"

    if event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        tenant_id = (subscription.get("metadata") or {}).get("tenant_id")
        if tenant_id:
            conn.execute(
                """
                UPDATE tenants
                SET subscription_status = 'trial', updated_at = datetime('now')
                WHERE id = ?
                """,
                (str(tenant_id),),
            )
            conn.commit()
            return f"downgraded:{tenant_id}"
        return "ignored: missing tenant_id"

    return f"ignored:{event['type']}"
