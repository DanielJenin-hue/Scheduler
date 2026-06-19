"""Billing upgrade trigger opens Stripe checkout from session flag."""

from __future__ import annotations

from unittest.mock import MagicMock

from lab_scheduler.ui.billing_ui import (
    pop_billing_checkout_trigger,
    process_billing_checkout_trigger,
)


def test_pop_billing_checkout_trigger() -> None:
    state = {"billing_checkout_trigger_tenant-1": True}
    assert pop_billing_checkout_trigger(state, "tenant-1") is True
    assert "billing_checkout_trigger_tenant-1" not in state


def test_process_billing_checkout_trigger_shows_link() -> None:
    import lab_scheduler.ui.billing_ui as billing_ui

    mock_checkout = MagicMock(
        return_value=("sess_123", "https://checkout.stripe.test/sess_123")
    )
    billing_ui.create_billing_checkout_session = mock_checkout
    state = {"billing_checkout_trigger_tenant-1": True}
    gates = MagicMock(is_premium=False)
    link_button = MagicMock()
    info = MagicMock()
    error = MagicMock()

    billing_ui.process_billing_checkout_trigger(
        MagicMock(),
        state,
        tenant_id="tenant-1",
        tenant_slug="acme-lab",
        customer_email="mgr@acme.test",
        gates=gates,
        link_button=link_button,
        info=info,
        error=error,
    )

    mock_checkout.assert_called_once()
    link_button.assert_called_once_with(
        "Continue to secure checkout",
        "https://checkout.stripe.test/sess_123",
        type="primary",
    )
    info.assert_called_once()
    error.assert_not_called()


def test_process_billing_skips_premium() -> None:
    import lab_scheduler.ui.billing_ui as billing_ui

    mock_checkout = MagicMock()
    billing_ui.create_billing_checkout_session = mock_checkout
    state = {"billing_checkout_trigger_tenant-1": True}
    gates = MagicMock(is_premium=True)
    billing_ui.process_billing_checkout_trigger(
        MagicMock(),
        state,
        tenant_id="tenant-1",
        tenant_slug="acme-lab",
        customer_email=None,
        gates=gates,
        link_button=MagicMock(),
        info=MagicMock(),
        error=MagicMock(),
    )
    mock_checkout.assert_not_called()
