"""Prospects queue tab.

Tab switches must use ``request_business_tab`` from the parent shell (see ``navigation.py``),
not direct ``session_state["business_tab"]`` writes — Streamlit binds that key to the tab radio.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

import streamlit as st

from lab_scheduler.business.models import ProspectStatus
from lab_scheduler.business.prospect_service import list_prospects, update_prospect_status
from lab_scheduler.ui.business.components import render_empty_state, render_gather_progress, render_prospect_card
from lab_scheduler.ui.business.helpers import load_facility_enrichment

__all__ = ["render_prospects_tab"]

_SORT_OPTIONS = {
    "ICP score": lambda p: (-p.icp_score, p.facility),
    "Facility name": lambda p: (p.facility.lower(),),
    "Recently added": lambda p: (p.created_at or "",),
}


def render_prospects_tab(
    conn: sqlite3.Connection,
    *,
    on_gather: Callable[[], None],
    on_preview: Callable[[str], None],
) -> None:
    if st.session_state.get("biz_gather_step") is not None:
        render_gather_progress(int(st.session_state["biz_gather_step"]))

    header_left, header_right = st.columns([3, 1])
    with header_left:
        st.markdown("#### Prospects")
    with header_right:
        if st.button("Gather prospects", type="primary", key="prospects_gather", use_container_width=True):
            on_gather()

    filter_col1, filter_col2, filter_col3, sort_col = st.columns([1, 1, 1, 1])
    with filter_col1:
        province = st.selectbox("Province", ["MB", "All"], index=0, key="biz_filter_province")
    with filter_col2:
        status_filter = st.selectbox(
            "Status",
            ["All statuses", "New", "Previewed", "Active Client", "Passed"],
            index=0,
            key="biz_filter_status",
        )
    with filter_col3:
        min_icp = st.slider("Min ICP", 0, 25, 0, key="biz_filter_min_icp")
    with sort_col:
        sort_key = st.selectbox("Sort", list(_SORT_OPTIONS), index=0, key="biz_sort")

    prospects = list_prospects(conn, province=None if province == "All" else province)

    status_map = {
        "New": {ProspectStatus.DISCOVERED},
        "Previewed": {ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED},
        "Active Client": {ProspectStatus.ACTIVE_CLIENT},
        "Passed": {ProspectStatus.DECLINED},
    }
    if status_filter != "All statuses":
        allowed = status_map[status_filter]
        prospects = [p for p in prospects if p.status in allowed]

    min_icp_backend = int(min_icp * 100 / 25)
    prospects = [p for p in prospects if p.icp_score >= min_icp_backend]
    prospects.sort(key=_SORT_OPTIONS[sort_key])

    if not prospects:
        if render_empty_state(
            icon="◎",
            headline="No prospects in queue",
            body="Import from regional_facilities.csv or run the weekly Prospector scan.",
            cta_label="Run auto-gather",
            cta_key="prospects_empty_gather",
        ):
            on_gather()
        return

    def _pass(prospect_id: str) -> None:
        update_prospect_status(conn, prospect_id, ProspectStatus.DECLINED)
        st.toast("Prospect marked as Passed")
        st.rerun()

    cols = st.columns(3)
    for index, prospect in enumerate(prospects):
        enrichment = load_facility_enrichment(prospect)
        with cols[index % 3]:
            render_prospect_card(
                prospect,
                enrichment,
                key_prefix="prospect",
                on_preview=lambda pid=prospect.id: on_preview(pid),
                on_pass=lambda pid=prospect.id: _pass(pid),
            )
