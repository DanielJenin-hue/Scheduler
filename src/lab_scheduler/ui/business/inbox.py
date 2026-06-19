"""Business Inbox tab — inbound prospect replies."""

from __future__ import annotations

import html
import sqlite3
from typing import List, Optional

import streamlit as st

from lab_scheduler.business.inbound_email import (
    ImapConfig,
    ImapNotConfiguredError,
    InboundMessage,
    count_unread_inbound,
    get_inbound_message,
    imap_setup_instructions,
    list_inbound_messages,
    log_manual_reply,
    mark_inbound_archived,
    mark_inbound_read,
    sync_inbound_from_imap,
)
from lab_scheduler.business.models import Prospect
from lab_scheduler.business.prospect_service import ProspectServiceError, get_prospect
from lab_scheduler.ui.business.components import (
    render_empty_state,
    render_html,
    render_prospect_card_html,
    render_status_badge,
)
from lab_scheduler.ui.business.helpers import load_facility_enrichment
from lab_scheduler.ui.business.navigation import request_business_tab

__all__ = ["render_inbox_tab", "unread_inbox_badge_label"]


def unread_inbox_badge_label(conn: sqlite3.Connection) -> str:
    count = count_unread_inbound(conn)
    return f"Inbox ({count})" if count else "Inbox"


def _toast(message: str) -> None:
    st.session_state["business_toast"] = message


def _open_prospect(prospect_id: str) -> None:
    st.session_state["business_prospect_id"] = prospect_id
    request_business_tab(st.session_state, "Email Preview")
    st.rerun()


def _proceed_with_client(prospect_id: str) -> None:
    st.session_state["business_prospect_id"] = prospect_id
    st.session_state[f"business_proceed_{prospect_id}"] = True
    request_business_tab(st.session_state, "Email Preview")
    st.rerun()


def _render_message_detail(
    conn: sqlite3.Connection,
    message: InboundMessage,
    prospect: Optional[Prospect],
) -> None:
    st.markdown(f"### {html.escape(message.subject or '(no subject)')}")
    st.caption(
        f"From **{html.escape(message.from_email)}** · "
        f"{html.escape(message.received_at)}"
    )
    if prospect:
        st.markdown(
            f"Linked to **{html.escape(prospect.facility)}** · "
            f"`{html.escape(prospect.id)}`",
            unsafe_allow_html=True,
        )
        render_status_badge(prospect.status)
    elif message.prospect_id:
        st.warning("Linked prospect was removed from the pipeline.")

    body = message.body_text or "(no plain-text body)"
    st.markdown(
        f'<div class="biz-email-body">{html.escape(body)}</div>',
        unsafe_allow_html=True,
    )

    action_cols = st.columns(4)
    with action_cols[0]:
        if message.status.value == "unread":
            if st.button("Mark read", key=f"inbox_read_{message.id}", use_container_width=True):
                mark_inbound_read(conn, message.id)
                _toast("Marked read")
                st.rerun()
    with action_cols[1]:
        if prospect:
            if st.button("Open prospect", key=f"inbox_prospect_{message.id}", use_container_width=True):
                _open_prospect(prospect.id)
    with action_cols[2]:
        if prospect:
            if st.button(
                "Proceed with client",
                key=f"inbox_proceed_{message.id}",
                type="primary",
                use_container_width=True,
            ):
                _proceed_with_client(prospect.id)
    with action_cols[3]:
        if prospect:
            if st.button("Schedule demo", key=f"inbox_demo_{message.id}", use_container_width=True):
                st.session_state["business_prospect_id"] = prospect.id
                request_business_tab(st.session_state, "Email Preview")
                _toast(f"Demo follow-up · {prospect.facility} — confirm time in Email Preview")
                st.rerun()
        if st.button("Archive", key=f"inbox_archive_{message.id}", use_container_width=True):
            mark_inbound_archived(conn, message.id)
            st.session_state.pop("business_inbox_selected", None)
            _toast("Archived")
            st.rerun()


def _render_thread_list(
    conn: sqlite3.Connection,
    messages: List[InboundMessage],
    prospects_by_id: dict[str, Prospect],
) -> None:
    for message in messages:
        prospect = prospects_by_id.get(message.prospect_id or "")
        facility = prospect.facility if prospect else "Unmatched"
        unread = message.status.value == "unread"
        badge = "● " if unread else ""
        label = f"{badge}{message.subject or '(no subject)'} — {facility}"
        if st.button(
            label,
            key=f"inbox_thread_{message.id}",
            use_container_width=True,
        ):
            st.session_state["business_inbox_selected"] = message.id
            st.rerun()


def _render_manual_log_form(conn: sqlite3.Connection) -> None:
    with st.expander("Log reply manually", expanded=False):
        st.caption("Paste a reply when IMAP is not configured or for offline capture.")
        prospects = []
        from lab_scheduler.business.prospect_service import list_prospects

        prospects = list_prospects(conn)
        prospect_options = {"(auto-match by email)": None}
        for prospect in prospects:
            label = f"{prospect.facility} ({prospect.email or 'no email'})"
            prospect_options[label] = prospect.id

        from_email = st.text_input("From email", key="inbox_manual_from")
        subject = st.text_input("Subject", key="inbox_manual_subject")
        body = st.text_area("Message body", key="inbox_manual_body", height=160)
        selected_label = st.selectbox(
            "Link to prospect",
            list(prospect_options.keys()),
            key="inbox_manual_prospect",
        )
        if st.button("Save reply", key="inbox_manual_save", type="primary"):
            try:
                logged = log_manual_reply(
                    conn,
                    from_email=from_email,
                    subject=subject,
                    body_text=body,
                    prospect_id=prospect_options[selected_label],
                )
            except Exception as exc:
                st.error(str(exc))
            else:
                st.session_state["business_inbox_selected"] = logged.id
                _toast("Reply logged")
                st.rerun()


def render_inbox_tab(conn: sqlite3.Connection) -> None:
    """Render the Inbox tab for inbound prospect replies."""

    unread = count_unread_inbound(conn)
    header_col, sync_col = st.columns([3, 1])
    with header_col:
        st.markdown("### Inbox")
        st.caption(
            f"{unread} unread · Replies from prospects land here after you send outreach."
        )
    with sync_col:
        if st.button("Sync inbox", key="inbox_sync", type="primary", use_container_width=True):
            try:
                result = sync_inbound_from_imap(conn)
            except ImapNotConfiguredError as exc:
                st.warning(str(exc))
            except Exception as exc:
                st.error(f"Sync failed: {exc}")
            else:
                _toast(
                    f"Sync complete · {result.inserted} new, "
                    f"{result.matched} matched, {result.skipped_duplicate} duplicates skipped"
                )
                st.rerun()

    try:
        ImapConfig.from_env()
        imap_ready = True
    except ImapNotConfiguredError:
        imap_ready = False

    messages = list_inbound_messages(conn, limit=100)
    if not messages and not imap_ready:
        st.markdown(
            f'<div class="biz-card"><pre style="white-space:pre-wrap;font-size:0.8125rem;">'
            f"{html.escape(imap_setup_instructions())}</pre></div>",
            unsafe_allow_html=True,
        )
        if render_empty_state(
            icon="📥",
            headline="Connect your inbox",
            body="Configure IMAP env vars and click Sync inbox, or log a reply manually below.",
            cta_label="Sync inbox",
            cta_key="inbox_empty_sync",
        ):
            try:
                sync_inbound_from_imap(conn)
                st.rerun()
            except ImapNotConfiguredError as exc:
                st.warning(str(exc))
        _render_manual_log_form(conn)
        return

    if not messages:
        st.info("Inbox connected — no messages yet. Click **Sync inbox** after prospects reply.")
        _render_manual_log_form(conn)
        return

    prospects_by_id: dict[str, Prospect] = {}
    for message in messages:
        if message.prospect_id and message.prospect_id not in prospects_by_id:
            try:
                prospects_by_id[message.prospect_id] = get_prospect(conn, message.prospect_id)
            except ProspectServiceError:
                pass

    list_col, detail_col = st.columns([1, 2])
    with list_col:
        st.markdown("**Threads**")
        _render_thread_list(conn, messages, prospects_by_id)

    with detail_col:
        selected_id = st.session_state.get("business_inbox_selected")
        selected: InboundMessage | None = None
        if selected_id:
            try:
                selected = get_inbound_message(conn, str(selected_id))
            except Exception:
                selected = None
        if selected is None and messages:
            selected = messages[0]
        if selected:
            prospect = prospects_by_id.get(selected.prospect_id or "")
            _render_message_detail(conn, selected, prospect)
            if prospect:
                enrichment = load_facility_enrichment(prospect)
                with st.expander("Prospect card", expanded=False):
                    render_html(render_prospect_card_html(prospect, enrichment, compact=True))

    _render_manual_log_form(conn)
