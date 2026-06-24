"""Manager workspace tab renderers."""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Mapping, Optional, Set

import pandas as pd
import streamlit as st

from lab_scheduler.billing.feature_gates import FeatureGates, PREMIUM_UPSELL_SHORT
from lab_scheduler.compliance.engine import JurisdictionRules


def render_manager_print_tab(
    *,
    period: object,
    facility_name: str,
    export_employees: List[Dict],
    dates: List[date],
    assignments: List[Dict],
    templates: Dict[str, Dict],
    blocked_map: Dict[str, Dict[date, str]],
    gates: FeatureGates,
    rules: JurisdictionRules,
    qual_codes: Optional[Dict[str, str]],
    qual_ids_by_employee: Optional[Dict[str, Set[str]]],
    contract_target_hours: Optional[Dict[str, float]],
    publish_state: Mapping[str, object],
    schedule_archetype: str,
    posting_readiness: Optional[object],
    export_ready: bool,
    build_breakroom_document: callable,
    breakroom_posting_context: callable,
) -> None:
    saved_filled = int(publish_state.get("saved_filled") or 0)
    saved_total = int(publish_state.get("saved_total") or 0)

    st.markdown("#### Posting checklist")
    st.markdown(f"- **Saved database schedule:** {saved_filled}/{saved_total} slots")
    st.markdown(
        f"- **Breakroom export:** {'Ready' if export_ready and gates.can_export_breakroom else 'Blocked — fix tallies, contract hours, or compliance'}"
    )

    if not export_ready:
        bullets = ()
        if posting_readiness is not None:
            bullets = getattr(posting_readiness, "attention_bullets", ())
        bullet_lines = "\n".join(f"- {item}" for item in bullets)
        if bullet_lines:
            st.warning(
                f"**Schedule saved; breakroom export blocked until checks pass:**\n\n{bullet_lines}"
            )
        else:
            st.warning(
                "**Schedule saved; breakroom export blocked** — resolve compliance or contract checks."
            )
    elif not gates.can_export_breakroom:
        st.info(PREMIUM_UPSELL_SHORT)
    else:
        pdf_html = build_breakroom_document(
            facility_name=facility_name,
            period=period,
            employees=export_employees,
            dates=dates,
            assignments=assignments,
            templates=templates,
            blocked_map=blocked_map,
            rules=rules,
            qual_codes=qual_codes,
            qual_ids_by_employee=qual_ids_by_employee,
            contract_target_hours=contract_target_hours,
            schedule_archetype=schedule_archetype,
            posting_context=breakroom_posting_context(
                publish_state,
                is_premium=gates.is_premium,
            ),
        )
        st.download_button(
            "Download breakroom schedule (HTML for print)",
            data=pdf_html.encode("utf-8"),
            file_name=f"breakroom_schedule_{getattr(period, 'id', 'period')}.html",
            mime="text/html",
            width="stretch",
            key=f"manager_print_breakroom_{getattr(period, 'id', 'period')}",
            disabled=not export_ready,
            help="Legal/Ledger landscape layout for hospital breakroom posting.",
        )
        if export_ready:
            st.caption("Open the HTML in Chrome/Edge → Print → Save as PDF if needed.")


def render_manager_analytics_tab(
    *,
    employee_stats_df: pd.DataFrame,
    meta_view: pd.DataFrame,
    meta_column_config: Mapping[str, object],
    metadata_editor_key: str,
) -> pd.DataFrame:
    st.markdown("#### Employee analytics")
    st.caption(
        "Per-line shift mix, contract hours, and equity targets from the current grid view. "
        "Alt % is evening share on D/E lines and night share on D/N lines."
    )
    if employee_stats_df.empty:
        st.caption("No roster lines in this view.")
    else:
        st.dataframe(
            employee_stats_df,
            hide_index=True,
            width="stretch",
            column_config={
                "#": st.column_config.NumberColumn("#", width="small"),
                "Line": st.column_config.TextColumn("Line", width="medium"),
                "Contract": st.column_config.TextColumn("Contract", width="small"),
                "FTE": st.column_config.NumberColumn("FTE", format="%.2f", width="small"),
                "Day": st.column_config.NumberColumn("Day", width="small"),
                "Evening": st.column_config.NumberColumn("Evening", width="small"),
                "Night": st.column_config.NumberColumn("Night", width="small"),
                "Weekend": st.column_config.NumberColumn("Weekend", width="small"),
                "Alt %": st.column_config.TextColumn("Alt %", width="small"),
                "Scheduled h": st.column_config.NumberColumn("Scheduled h", format="%.1f"),
                "Target h": st.column_config.NumberColumn("Target h", format="%.1f"),
                "Variance h": st.column_config.NumberColumn("Variance h", format="%.1f"),
                "Alt vs target": st.column_config.TextColumn("Alt vs target", width="small"),
                "Wknd vs target": st.column_config.TextColumn("Wknd vs target", width="small"),
            },
        )

    st.markdown("---")
    st.markdown("#### Roster lines")
    st.caption("FTE and contract type for each schedule row.")
    edited_meta = st.data_editor(
        meta_view,
        column_config=dict(meta_column_config),
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        key=metadata_editor_key,
        column_order=["#", "Employee", "fte", "contract_line_type"],
    )
    if "#" in edited_meta.columns:
        edited_meta = edited_meta.drop(columns=["#"])

    return edited_meta
