"""Pipeline kanban tab.

Navigation callbacks (``on_preview``, ``on_open_onboarding``) must queue tabs via
``request_business_tab`` in the parent shell before ``st.rerun()``.
"""

from __future__ import annotations

import html
import sqlite3
from collections import defaultdict
from typing import Callable, List

import streamlit as st

from lab_scheduler.business.models import Prospect, ProspectStatus
from lab_scheduler.business.prospect_service import list_prospects
from lab_scheduler.ui.business.components import (
    render_empty_state,
    render_gather_progress,
    render_html,
    render_metric_tiles,
    render_prospect_card_html,
    status_badge_html,
)
from lab_scheduler.ui.business.helpers import load_facility_enrichment

__all__ = ["render_pipeline_tab"]

_PIPELINE_COLUMNS: tuple[tuple[str, frozenset[ProspectStatus]], ...] = (
    ("New", frozenset({ProspectStatus.DISCOVERED})),
    ("Previewed", frozenset({ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED})),
    ("Active Client", frozenset({ProspectStatus.ACTIVE_CLIENT})),
    ("Passed", frozenset({ProspectStatus.DECLINED})),
)


def _top_target(prospects: List[Prospect]) -> str:
    from lab_scheduler.business.discovery import EXCLUDED_FACILITY_IDS

    candidates = [
        p
        for p in prospects
        if p.status in {ProspectStatus.DISCOVERED, ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED}
        and p.facility_id not in EXCLUDED_FACILITY_IDS
        and not p.facility.startswith("Portage Regional")
    ]
    if not candidates:
        return "—"
    candidates.sort(key=lambda p: (-p.icp_score, p.facility))
    return candidates[0].facility


def render_pipeline_tab(
    conn: sqlite3.Connection,
    *,
    on_gather: Callable[[], None],
    on_preview: Callable[[str], None],
    on_open_onboarding: Callable[[str], None],
) -> None:
    prospects = list_prospects(conn)
    active = [p for p in prospects if p.status == ProspectStatus.ACTIVE_CLIENT]
    mrr = f"${299 * len(active):,}/mo" if active else "$299/mo"

    render_metric_tiles(
        mrr_label=mrr,
        active_clients=len(active),
        in_preview=len([p for p in prospects if p.status in {ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED}]),
        top_target=_top_target(prospects),
    )

    if st.session_state.get("biz_gather_step") is not None:
        render_gather_progress(int(st.session_state["biz_gather_step"]))

    _, toolbar_right = st.columns([3, 1])
    with toolbar_right:
        if st.button("Gather prospects", type="primary", key="pipeline_gather", use_container_width=True):
            on_gather()

    if not prospects:
        if render_empty_state(
            icon="▢▢▢",
            headline="Your pipeline is clear",
            body="Run a prospect scan to fill the New column with Manitoba hospital labs.",
            cta_label="Gather prospects",
            cta_key="pipeline_empty_gather",
        ):
            on_gather()
        return

    by_column: dict[str, list[Prospect]] = defaultdict(list)
    for prospect in prospects:
        for title, statuses in _PIPELINE_COLUMNS:
            if prospect.status in statuses:
                by_column[title].append(prospect)
                break

    show_passed = st.session_state.get("biz_show_passed", False)
    cols = st.columns(4)
    for index, (title, _) in enumerate(_PIPELINE_COLUMNS):
        items = by_column.get(title, [])
        if title == "Passed" and not show_passed and not items:
            continue
        with cols[index]:
            count = len(items)
            render_html(f'<div class="biz-kanban-header">{html.escape(title)} ({count})</div>')
            if title == "Passed" and count == 0:
                st.caption("No passed prospects")
            for prospect in items[:6]:
                if prospect.status == ProspectStatus.ACTIVE_CLIENT:
                    render_html(
                        f"""
                        <div class="biz-card">
                          <div style="display:flex; justify-content:space-between;">
                            <div class="biz-card-title">{html.escape(prospect.facility)}</div>
                            {status_badge_html(prospect.status)}
                          </div>
                          <div class="biz-card-sub">{html.escape(prospect.tenant_id or 'tenant pending')}</div>
                          <div class="biz-mono revenue">Managed first</div>
                        </div>
                        """
                    )
                    if prospect.tenant_id and st.button(
                        "Open onboarding",
                        key=f"pipe_onboard_{prospect.id}",
                        use_container_width=True,
                    ):
                        on_open_onboarding(prospect.tenant_id)
                else:
                    enrichment = load_facility_enrichment(prospect)
                    render_html(render_prospect_card_html(prospect, enrichment, compact=True))
                    if st.button(
                        "Preview email",
                        key=f"pipe_preview_{prospect.id}",
                        use_container_width=True,
                    ):
                        on_preview(prospect.id)

    if by_column.get("Passed"):
        st.session_state["biz_show_passed"] = st.checkbox(
            "Show passed column",
            value=show_passed,
            key="biz_show_passed_toggle",
        )
