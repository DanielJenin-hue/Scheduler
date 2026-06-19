"""Streamlit helpers for the master schedule grid iframe."""

from __future__ import annotations

from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import pandas as pd
import streamlit as st

from lab_scheduler.policy.policy_engine import ContractTrackingRow


def streamlit_html_component(
    html_doc: str,
    *,
    height: int,
    scrolling: bool = False,
    component_key: str | None = None,
    interactive: bool = False,
) -> object | None:
    """Render HTML; interactive grids use iframe display plus the v2 storage bridge."""

    if not html_doc.strip():
        st.warning("Schedule grid could not be rendered (empty HTML).")
        return None
    if interactive:
        container_key = component_key or "master_schedule_shift_grid"
        with st.container(key=container_key):
            return st.components.v1.html(
                html_doc,
                height=height,
                scrolling=False,
            )
    del component_key, scrolling
    st.iframe(html_doc, height=height, width="stretch")
    return None


def inject_ops_ribbon_live_metrics_listener() -> None:
    """Apply live contract/gap metric updates posted from the schedule grid iframe."""

    st.components.v1.html(
        """
<script>
(function () {
  if (window.__labOpsRibbonListenerReady) {
    if (window.Streamlit && window.Streamlit.setComponentReady) {
      window.Streamlit.setComponentReady();
    }
    return;
  }
  window.__labOpsRibbonListenerReady = true;
  function labGridTopRoot() {
    try { if (window.top) return window.top; } catch (err) {}
    try { if (window.parent && window.parent !== window) return window.parent; } catch (err2) {}
    return window;
  }
  function mergeGridStorePayload(target, incoming) {
    if (!incoming || !incoming.changes) return target;
    if (!target.changes) target.changes = [];
    incoming.changes.forEach(function (item) {
      if (!item || !item.employee_id || !item.date) return;
      var existing = target.changes.findIndex(function (entry) {
        return entry.employee_id === item.employee_id && entry.date === item.date;
      });
      if (existing >= 0) target.changes[existing] = item;
      else target.changes.push(item);
    });
    if (incoming.lock_toggles) target.lock_toggles = incoming.lock_toggles;
    if (incoming.tally_select) target.tally_select = incoming.tally_select;
    return target;
  }
  function ensureTopGridPersistListener() {
    var root = labGridTopRoot();
    if (root.__labGridPersistListenerReady) return;
    root.__labGridPersistListenerReady = true;
    root.addEventListener("message", function (event) {
      if (!event.data || event.data.type !== "lab-grid-persist") return;
      var storageKey = event.data.storageKey;
      var payload = event.data.payload;
      if (!storageKey || !payload) return;
      if (!root.__labGridPendingStore) root.__labGridPendingStore = {};
      var stored = root.__labGridPendingStore[storageKey] || { changes: [] };
      root.__labGridPendingStore[storageKey] = mergeGridStorePayload(stored, payload);
    });
  }
  ensureTopGridPersistListener();
  function applyOpsRibbonMetrics(payload) {
    if (!payload || payload.type !== "lab-ops-metrics") return;
    var rev = Number(payload.stagingRevision || 0);
    if (!window.__labOpsMetricsRevision) window.__labOpsMetricsRevision = 0;
    if (rev > 0 && rev < window.__labOpsMetricsRevision) return;
    if (rev > 0) window.__labOpsMetricsRevision = rev;
    var hoursEl = document.getElementById("lab-ops-hours-deficit");
    if (hoursEl && payload.hoursDelta !== undefined && payload.hoursDelta !== null) {
      var delta = Number(payload.hoursDelta);
      if (Math.abs(delta) < 0.5) {
        hoursEl.textContent = "Balanced";
        hoursEl.className = "lab-ops-metric-value lab-ops-metric-ok";
      } else {
        hoursEl.textContent = (delta >= 0 ? "+" : "") + Math.round(delta) + "h";
        hoursEl.className = "lab-ops-metric-value lab-ops-metric-warn";
      }
    }
    var gapEl = document.getElementById("lab-ops-gap-count");
    if (gapEl && payload.gapCount !== undefined && payload.gapCount !== null) {
      gapEl.textContent = String(Math.max(0, Math.round(Number(payload.gapCount))));
    }
  }
  window.addEventListener("message", function (event) {
    applyOpsRibbonMetrics(event.data);
  });
  if (window.Streamlit && window.Streamlit.setComponentReady) {
    window.Streamlit.setComponentReady();
  }
})();
</script>
        """,
        height=0,
        scrolling=False,
    )


def inject_schedule_grid_layout_css() -> None:
    """Normal mode: grid iframe sizing with internal scroll."""

    st.markdown(
        """
        <style>
          div[data-testid="stHtml"],
          div[data-testid="stIFrame"] {
            width: 100% !important;
            max-width: 100% !important;
          }
          div[data-testid="stHtml"] iframe,
          iframe[data-testid="stIFrame"] {
            width: 100% !important;
            max-width: 100% !important;
            border: none;
            display: block;
            height: min(72vh, 820px) !important;
            min-height: 420px;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_master_schedule_shift_grid(
    matrix: pd.DataFrame,
    view_dates: List,
    *,
    period_id: str,
    view_chunk_index: int = 0,
    staging_revision: int = 0,
    edit_mode: bool = False,
    fullscreen: bool = False,
    contract_rows: Optional[Mapping[str, ContractTrackingRow]] = None,
    full_employee_matrix: Optional[pd.DataFrame] = None,
    all_date_keys: Optional[Sequence[str]] = None,
    hours_per_shift: float = 8.0,
    equity_drift_by_employee: Optional[Mapping[str, object]] = None,
    locked_cells: Optional[Set[Tuple[str, object]]] = None,
    period_start: Optional[object] = None,
    period_end: Optional[object] = None,
    focus_fit: bool = False,
    tally_matrix: Optional[pd.DataFrame] = None,
    role_suffix: Optional[str] = None,
    ops_metrics_json: Optional[str] = None,
    health_focus_date: Optional[object] = None,
    build_grid_html: callable,
) -> Optional[Dict[str, object]]:
    row_count = len(matrix.index)
    grid_body = 130 + (row_count * 52) + (3 * 40) + 48
    grid_height = max(720, min(960, grid_body + 48)) if focus_fit else min(820, max(480, grid_body + 48))
    html_doc = build_grid_html(
        matrix,
        view_dates,
        period_id=period_id,
        edit_mode=edit_mode,
        fullscreen=fullscreen,
        contract_rows=contract_rows,
        full_employee_matrix=full_employee_matrix,
        all_date_keys=all_date_keys,
        hours_per_shift=hours_per_shift,
        equity_drift_by_employee=equity_drift_by_employee,
        locked_cells=locked_cells,
        period_start=period_start,
        period_end=period_end,
        focus_fit=focus_fit,
        tally_matrix=tally_matrix,
        staging_revision=staging_revision,
        ops_metrics_json=ops_metrics_json,
        health_focus_date=health_focus_date,
    )
    suffix = role_suffix or ("focus_fit" if focus_fit else "main")
    return streamlit_html_component(
        html_doc,
        height=grid_height,
        scrolling=False,
        component_key=(
            "master_schedule_shift_grid_"
            f"{period_id}_{view_chunk_index}_{int(edit_mode)}_{suffix}_rev{staging_revision}"
        ),
        interactive=True,
    )
