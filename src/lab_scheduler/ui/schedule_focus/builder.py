"""Focus view — fit-to-window schedule grid for ultra-wide editing."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Sequence


def focus_mode_grid_stylesheet() -> str:
    """Fit entire schedule in viewport — scales with window width (e.g. dual monitors)."""
    return """
<style>
  html, body {
    width: 100vw;
    height: 100vh;
    max-width: 100vw;
    max-height: 100vh;
    margin: 0;
    padding: 0;
    overflow: hidden;
    background: #ffffff;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit {
    position: fixed;
    inset: 0;
    width: 100vw;
    height: 100vh;
    max-width: 100vw;
    max-height: 100vh;
    overflow: hidden;
    border: none;
    border-radius: 0;
    box-shadow: none;
    display: flex;
    flex-direction: column;
    padding: 0;
    box-sizing: border-box;
    background: #ffffff;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-fullscreen-toolbar {
    position: fixed;
    top: 6px;
    right: 10px;
    z-index: 10050;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 14px;
    padding: 6px 12px;
    border-radius: 8px;
    border: 1px solid #cbd5e1;
    background: rgba(255, 255, 255, 0.95);
    box-shadow: 0 4px 14px rgba(15, 23, 42, 0.12);
    font: 600 11px/1.25 system-ui, sans-serif;
    color: #0f172a;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-fs-toolbar-title {
    font-weight: 700;
    color: #1e293b;
    letter-spacing: 0.02em;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-fs-control {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
    color: #475569;
    cursor: pointer;
    white-space: nowrap;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-fs-control input[type="range"] {
    width: 96px;
    cursor: pointer;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-fs-stretch-toggle input {
    cursor: pointer;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-tally-legend {
    display: none;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-focus-hint {
    display: none;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-focus-scaler {
    flex: 1 1 auto;
    min-height: 0;
    min-width: 0;
    overflow: hidden;
    position: relative;
    width: 100%;
    height: 100%;
    max-width: 100vw;
    max-height: 100vh;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-focus-scaler-inner {
    position: absolute;
    inset: 0;
    overflow: hidden;
    display: block;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-schedule-grid {
    transform-origin: top left;
    border-collapse: separate;
    border-spacing: 0;
    table-layout: auto;
    width: max-content;
    height: max-content;
    margin: 0;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit thead th,
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit tbody td,
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit tfoot td {
    padding: 2px 1px;
    line-height: 1.15;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit thead th.lab-emp-col,
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit tbody td.lab-emp-col,
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit tfoot td.lab-tally-label {
    position: sticky;
    left: 0;
    z-index: 3;
    width: 188px;
    min-width: 168px;
    max-width: 220px;
    font-size: 10px;
    padding: 4px 6px;
    vertical-align: top;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-emp-cell-compact {
    display: flex;
    flex-direction: column;
    gap: 3px;
    line-height: 1.25;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-emp-primary {
    font-size: 11px;
    font-weight: 600;
    line-height: 1.25;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-emp-meta-row {
    display: block;
    font-size: 9px;
    line-height: 1.35;
    color: #475569;
    white-space: normal;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-emp-sub {
    display: inline;
    margin: 0;
    font-size: inherit;
    line-height: inherit;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-emp-sub + .lab-emp-sub::before {
    content: " · ";
    color: #94a3b8;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit tbody td.lab-emp-col {
    overflow: visible;
    white-space: normal;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit table.lab-schedule-grid tfoot tr.tally-row td {
    position: relative !important;
    bottom: auto !important;
    z-index: 2;
    vertical-align: middle;
    box-shadow: none;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit table.lab-schedule-grid tfoot tr.tally-row td.lab-tally-label {
    position: sticky;
    left: 0;
    z-index: 3;
    min-width: 168px;
    max-width: 220px;
    width: 188px;
    white-space: normal;
    line-height: 1.25;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit table.lab-schedule-grid tfoot tr.tally-row td.lab-tally-label .lab-emp-sub {
    display: block;
    font-size: clamp(5px, 0.72vh, 8px);
    font-weight: 500;
    color: #475569;
    margin-top: 1px;
    line-height: 1.2;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit table.lab-schedule-grid tfoot tr.tally-row td.lab-tally-label .lab-emp-sub::before {
    content: none;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit table.lab-schedule-grid tfoot tr.tally-row td.tally-cell {
    min-width: 28px;
    width: 36px;
    padding: 2px 1px;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit thead th.lab-day-col {
    width: 36px;
    min-width: 28px;
    font-size: clamp(6px, 0.85vh, 10px);
    padding: 3px 1px;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-shift-inline-select,
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-shift-pill-readonly,
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-shift-pill {
    min-width: 0;
    width: clamp(22px, 2.2vw, 40px);
    max-width: 40px;
    height: clamp(18px, 2.2vh, 28px);
    font-size: clamp(7px, 0.9vh, 11px);
    border-radius: 5px;
    padding: 0;
    -moz-appearance: none;
    appearance: none;
    -webkit-appearance: none;
    background-image: none !important;
    text-align: center;
    text-align-last: center;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-shift-inline-select::-ms-expand {
    display: none;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit tfoot tr.tally-row td {
    font-size: clamp(7px, 0.88vh, 10px);
    padding: 2px 1px;
  }
</style>
"""


def focus_mode_page_stylesheet() -> str:
    """Streamlit page chrome while focus mode is active (sidebar kept, header hidden)."""
    return """
<style>
  body.lab-schedule-focus-mode {
    background: #ffffff !important;
    color-scheme: light;
    overflow: hidden !important;
    position: fixed !important;
    inset: 0 !important;
    height: 100vh !important;
    width: 100vw !important;
    margin: 0 !important;
    padding: 0 !important;
    touch-action: none;
  }
  html:has(body.lab-schedule-focus-mode) {
    background: #ffffff !important;
    overflow: hidden !important;
    height: 100% !important;
  }
  body.lab-schedule-focus-mode [data-testid="stApp"],
  body.lab-schedule-focus-mode [data-testid="stAppViewContainer"],
  body.lab-schedule-focus-mode [data-testid="stAppViewContainer"] > section.main,
  body.lab-schedule-focus-mode [data-testid="stMainBlockContainer"],
  body.lab-schedule-focus-mode section.main,
  body.lab-schedule-focus-mode section.main > div.block-container,
  body.lab-schedule-focus-mode div[data-testid="stVerticalBlock"],
  body.lab-schedule-focus-mode div[data-testid="stVerticalBlockBorderWrapper"],
  body.lab-schedule-focus-mode [data-testid="stElementContainer"],
  body.lab-schedule-focus-mode div[data-testid="stHtml"],
  body.lab-schedule-focus-mode iframe[data-testid="stIFrame"] {
    max-width: 100% !important;
    width: 100% !important;
    background: #ffffff !important;
  }
  body.lab-schedule-focus-mode [data-testid="stAppViewContainer"],
  body.lab-schedule-focus-mode section.main,
  body.lab-schedule-focus-mode section.main > div.block-container {
    min-height: 100vh !important;
    height: 100vh !important;
    overflow: hidden !important;
  }
  body.lab-schedule-focus-mode section.main > div.block-container {
    padding-top: 0 !important;
    padding-bottom: 0 !important;
    padding-left: 0.25rem !important;
    padding-right: 0.25rem !important;
    max-width: 100vw !important;
  }
  body.lab-schedule-focus-mode header[data-testid="stHeader"],
  body.lab-schedule-focus-mode [data-testid="stToolbar"],
  body.lab-schedule-focus-mode [data-testid="stDecoration"],
  body.lab-schedule-focus-mode [data-testid="stStatusWidget"],
  body.lab-schedule-focus-mode footer {
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    max-height: 0 !important;
    overflow: hidden !important;
    visibility: hidden !important;
  }
  body.lab-schedule-focus-mode [data-testid="stAppViewContainer"] > header {
    display: none !important;
  }
  body.lab-schedule-focus-mode .stApp,
  body.lab-schedule-focus-mode [data-testid="stAppViewContainer"],
  body.lab-schedule-focus-mode section.main {
    background: #ffffff !important;
  }
  body.lab-schedule-focus-mode [data-testid="stSidebar"],
  body.lab-schedule-focus-mode [data-testid="stSidebarCollapsedControl"],
  body.lab-schedule-focus-mode [data-testid="collapsedControl"] {
    z-index: 1005 !important;
  }
  body.lab-schedule-focus-mode section.main {
    max-width: 100% !important;
  }
  body.lab-schedule-focus-mode .lab-schedule-fs-compact,
  body.lab-schedule-focus-mode .lab-focus-compact-hidden {
    display: none !important;
  }
  body.lab-schedule-focus-mode div[data-testid="stVerticalBlock"] {
    gap: 0.2rem !important;
  }
  body.lab-schedule-focus-mode [data-testid="stElementContainer"] {
    margin-bottom: 0 !important;
    padding-bottom: 0 !important;
  }
  body.lab-schedule-focus-mode .lab-ops-ribbon {
    margin-bottom: 0.15rem !important;
  }
  body.lab-schedule-focus-mode .lab-focus-bottom-hidden {
    display: none !important;
  }
  body.lab-schedule-focus-mode #lab-focus-sizer-root {
    display: none !important;
    height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
  }
  body.lab-schedule-focus-mode [data-testid="stHeading"],
  body.lab-schedule-focus-mode [data-testid="stCaptionContainer"],
  body.lab-schedule-focus-mode .lab-ops-ribbon,
  body.lab-schedule-focus-mode [data-testid="stRadio"],
  body.lab-schedule-focus-mode [data-testid="stRadio"] + div {
    display: none !important;
  }
  body.lab-schedule-focus-mode .lab-focus-grid-pinned {
    position: fixed !important;
    z-index: 1002 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    background: #ffffff !important;
  }
  body.lab-schedule-focus-mode div[data-testid="stHtml"] iframe,
  body.lab-schedule-focus-mode iframe[data-testid="stIFrame"] {
    width: 100% !important;
    max-width: 100% !important;
    height: 100% !important;
    min-height: 100% !important;
    max-height: 100% !important;
    display: block;
    border: none;
    background: #ffffff !important;
  }
</style>
"""


def _normalize_cell_token(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in ("", "—", "-", "OFF", "NONE", "NAN", "."):
        return ""
    if text in ("D", "M"):
        return "D"
    if text in ("E", "N"):
        return text
    return text[:1] if text[:1] in {"D", "E", "N"} else ""


def lines_from_schedule_frame(
    frame: object,
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
) -> List[Dict[str, Any]]:
    import pandas as pd

    from lab_scheduler.scheduling.schedule_tallies import is_daily_tally_employee_id

    lines: List[Dict[str, Any]] = []
    if not isinstance(frame, pd.DataFrame):
        return lines
    for employee in employees:
        employee_id = str(employee.get("id", "") or "")
        if not employee_id:
            continue
        row_match = frame[frame["employee_id"] == employee_id]
        if row_match.empty:
            continue
        if is_daily_tally_employee_id(employee_id):
            continue
        row = row_match.iloc[0]
        cells = [
            _normalize_cell_token(row.get(day.isoformat(), "")) for day in dates
        ]
        lines.append(
            {
                "lineId": employee_id,
                "employeeId": employee_id,
                "label": str(employee.get("full_name") or row.get("Employee", "")),
                "contractLine": str(employee.get("contract_line_type") or "D/E"),
                "cells": cells,
            }
        )
    return lines
