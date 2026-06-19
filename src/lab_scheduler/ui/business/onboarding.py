"""Client onboarding checklist tab."""

from __future__ import annotations

import sqlite3
from typing import Callable, Optional

import streamlit as st

from lab_scheduler.business.models import ProspectStatus
from lab_scheduler.business.prospect_service import list_prospects, update_prospect
from lab_scheduler.ui.business.components import render_empty_state, render_status_badge
from lab_scheduler.ui.business.helpers import (
    DEFAULT_ONBOARDING_TASKS,
    load_onboarding_tasks,
    save_onboarding_tasks,
)

__all__ = ["render_onboarding_tab"]

TASK_ACTIONS: dict[str, str] = {
    "send_outreach": "View sent draft",
    "collect_roster": "Open import guide",
    "create_period": "Open manager workspace",
    "distribute_fill_save": "Open manager workspace",
    "rsi_gate": "Run check",
    "deliver_breakroom": "Open Print tab",
    "invoice": "Add note",
}


def _resolve_onboarding_context(conn: sqlite3.Connection) -> tuple[Optional[str], Optional[dict]]:
    tenant_id = st.session_state.get("business_onboarding_tenant_id")
    if tenant_id:
        row = conn.execute(
            "SELECT id, name, slug FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if row:
            prospect = next(
                (p for p in list_prospects(conn, status=ProspectStatus.ACTIVE_CLIENT) if p.tenant_id == tenant_id),
                None,
            )
            return tenant_id, {"tenant": row, "prospect": prospect}

    active = list_prospects(conn, status=ProspectStatus.ACTIVE_CLIENT)
    if len(active) == 1 and active[0].tenant_id:
        st.session_state["business_onboarding_tenant_id"] = active[0].tenant_id
        row = conn.execute(
            "SELECT id, name, slug FROM tenants WHERE id = ?",
            (active[0].tenant_id,),
        ).fetchone()
        return active[0].tenant_id, {"tenant": row, "prospect": active[0]}
    return None, None


def render_onboarding_tab(
    conn: sqlite3.Connection,
    *,
    on_view_pipeline: Callable[[], None],
    on_view_email: Callable[[str], None],
) -> None:
    tenant_id, ctx = _resolve_onboarding_context(conn)
    if not tenant_id or ctx is None:
        if render_empty_state(
            icon="☑",
            headline="No clients onboarding",
            body="When you proceed with a prospect, their setup checklist appears here.",
            cta_label="View Pipeline",
            cta_key="onboard_empty_pipeline",
        ):
            on_view_pipeline()
        return

    tenant = ctx["tenant"]
    prospect = ctx.get("prospect")
    facility_name = tenant[1] if tenant else "Client"
    slug = tenant[2] if tenant else ""

    header_left, header_right = st.columns([4, 1])
    with header_left:
        st.markdown(f"### Client Onboarding — {facility_name}")
        st.caption(f"{slug} · Managed first · Active workspace")
    with header_right:
        render_status_badge(ProspectStatus.ACTIVE_CLIENT)

    tasks = load_onboarding_tasks(conn, tenant_id)
    completed = sum(1 for value in tasks.values() if value)
    total = len(DEFAULT_ONBOARDING_TASKS)
    pct = int(completed / total * 100) if total else 0

    st.markdown(
        f"""
        <div style="margin: 16px 0;">
          <div class="biz-metric-label">Setup progress · {completed}/{total}</div>
          <div class="biz-icp-bar"><div class="biz-icp-fill" style="width:{pct}%"></div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    notes_key = f"biz_onboard_notes_{tenant_id}"
    if notes_key not in st.session_state and prospect and prospect.notes:
        st.session_state[notes_key] = prospect.notes

    for task_id, label in DEFAULT_ONBOARDING_TASKS:
        row_left, row_mid, row_right = st.columns([0.5, 3, 1.5])
        with row_left:
            checked = st.checkbox(
                label,
                value=tasks.get(task_id, False),
                key=f"biz_task_{tenant_id}_{task_id}",
                label_visibility="collapsed",
            )
            tasks[task_id] = checked
        with row_mid:
            if task_id == "create_tenant" and checked:
                st.markdown(f"☑ **{label}** · Done automatically")
            else:
                st.markdown(f"{'☑' if checked else '☐'} **{label}**")
        with row_right:
            action = TASK_ACTIONS.get(task_id)
            if action and task_id == "send_outreach" and prospect:
                if st.button(action, key=f"biz_action_{task_id}_{tenant_id}"):
                    on_view_email(prospect.id)
            elif action and task_id in {"create_period", "distribute_fill_save", "deliver_breakroom"}:
                st.link_button(action, "/manager_app", key=f"biz_link_{task_id}_{tenant_id}")
            elif action:
                st.button(action, key=f"biz_action_{task_id}_{tenant_id}", disabled=task_id == "rsi_gate")

    save_onboarding_tasks(conn, tenant_id, tasks)

    st.markdown("#### Notes")
    st.text_area(
        "Onboarding notes",
        key=notes_key,
        height=120,
        label_visibility="collapsed",
        placeholder="Contact details, conference notes, delivery timeline…",
    )

    if prospect and st.session_state.get(notes_key):
        update_prospect(conn, prospect.id, notes=st.session_state[notes_key])
