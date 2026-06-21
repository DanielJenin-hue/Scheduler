from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Literal

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
SQL_DIR = ROOT / "sql"
UI_LOGGER = logging.getLogger("lab_scheduler.ui")
if os.environ.get("LAB_ALLOW_DEMO_ACCOUNTS") is None:
    if os.environ.get("LAB_SCHEDULER_ENV", "development").strip().lower() != "production":
        os.environ["LAB_ALLOW_DEMO_ACCOUNTS"] = "1"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
else:
    sys.path.remove(str(SRC_DIR))
    sys.path.insert(0, str(SRC_DIR))

# Drop partially-loaded lab_scheduler modules (Streamlit hot-reload can leave these broken).
for _mod in list(sys.modules):
    if _mod == "lab_scheduler" or _mod.startswith("lab_scheduler."):
        del sys.modules[_mod]

from lab_scheduler.auth import (  # noqa: E402
    AuthenticatedSession,
    SignupError,
    authenticate_user,
    count_active_employees,
    create_schedule_period,
    default_test_accounts,
    ensure_demo_account_credentials,
    is_onboarding_complete,
    load_portage_demo_roster,
    mark_onboarding_complete,
    register_tenant,
    seed_default_accounts,
    tenant_has_schedule_period,
)
from lab_scheduler.audit import (  # noqa: E402
    fetch_audit_logs,
    format_shift_code_display,
    log_auto_generation,
    log_constraint_violation,
    log_manual_edit,
    log_seniority_bypass,
    ensure_seniority_cba_schema,
)
from lab_scheduler.availability import (  # noqa: E402
    AVAILABILITY_OFF_CODES,
    OFF_CODE_SICK,
    OFF_CODE_VACATION,
    AvailabilityException,
    blocked_dates_by_employee,
    compute_employee_target_hours,
    expand_blocked_dates,
    is_availability_off_code,
    off_code_label,
    reason_to_off_code,
)
from lab_scheduler.compliance import (  # noqa: E402
    ComplianceAuditSummary,
    ComplianceReport,
    DEFAULT_JURISDICTION_NAME,
    JURISDICTIONS,
    JurisdictionRules,
    ScheduledShift,
    ShiftTemplateInfo,
    TenantMetadata,
    evaluate_schedule,
    generate_audit_export,
    get_jurisdiction,
)
from lab_scheduler.scheduling.assignment_validation import validate_assignment_change  # noqa: E402
from lab_scheduler.scheduling.open_shift_slots import list_open_shift_slots  # noqa: E402
from lab_scheduler.scheduling.profiles import EmployeeProfile  # noqa: E402
from lab_scheduler.scheduling.schedule_families import (  # noqa: E402
    ScheduleFamily,
    is_portage_roster,
    resolve_schedule_family,
)
from lab_scheduler.scheduling.persist_validation import find_core_persist_violations  # noqa: E402
from lab_scheduler.scheduling.shift_cell_locks import (  # noqa: E402
    apply_shift_cell_lock_toggles,
    fetch_shift_cell_locks,
    ensure_shift_cell_locks_schema,
    set_shift_cell_lock,
)
from lab_scheduler.scheduling.contract_payroll import HOURS_PER_SHIFT, paid_hours_per_shift  # noqa: E402
from lab_scheduler.scheduling.strategies import (  # noqa: E402
    ScheduleArchetype,
    schedule_archetype_display_label,
)
from lab_scheduler.scheduling.provisional_compliance import (  # noqa: E402
    ProvisionalAssignment,
    approved_stretch_from_system_note,
    is_approved_contract_line_exception_note,
)
from lab_scheduler.scheduling.provisional_approval import (  # noqa: E402
    approve_provisional_assignment,
    attach_assignment_ids,
    clear_provisional_stretch_state,
    load_pending_provisional_assignments,
)
from lab_scheduler.paths import resolve_project_path  # noqa: E402
from lab_scheduler.scheduling.agency_fulfillment import (  # noqa: E402
    apply_agency_placements_to_schedule_rows,
    persist_agency_placeholder_assignment,
)
from lab_scheduler.scheduling.agency_worker import (  # noqa: E402
    create_line_item_placeholders,
    find_latest_agency_request,
    line_item_id,
    load_agency_request,
    mark_agency_request_closed_unfilled,
    mark_agency_request_fulfilled,
    mark_agency_request_sent,
    STATUS_CLOSED_UNFILLED,
    STATUS_DRAFT_PENDING_APPROVAL,
    STATUS_FULFILLED,
    STATUS_PARTIALLY_FULFILLED,
    STATUS_SENT,
)
from lab_scheduler.scheduling.breakroom_print import (
    FTE_TOPUP_TOKEN,
    WORKED_SHIFT_TOKENS,
    BreakroomPostingContext,
    ContractTrackingRow,
    breakroom_posting_context_from_publish_state,
    build_coverage_gaps_by_day,
    format_schedule_employee_label,
    generate_breakroom_print_html,
    infer_role_code_from_employee,
    template_short_to_breakroom_token,
)
from lab_scheduler.scheduling.persist_validation import (
    FULLTIME_CONTRACT_HOUR_TOLERANCE,
    count_subfloor_evening_night_days,
)
from lab_scheduler.scheduling.schedule_health import (  # noqa: E402
    ScheduleHealthSnapshot,
    build_schedule_health_snapshot,
    chunk_index_for_date,
    format_tally_issue_message,
)
from lab_scheduler.scheduling.schedule_export import (
    EMPTY_SHIFT_DISPLAY as EXPORT_EMPTY_SHIFT_DISPLAY,
    TRIAGE_ESCALATED_CELL_TAG,
    apply_triage_escalation_tags,
    build_schedule_export_rows,
    filter_breakroom_export_rows,
    is_schedule_date_column,
    load_triage_escalation_payload,
    render_breakroom_schedule_html,
    shift_code_to_display_token,
    template_record_to_display_token,
)  # noqa: E402
from lab_scheduler.scheduling.display_labels import (  # noqa: E402
    shift_template_display_name,
    staff_line_display_name,
)
from lab_scheduler.scheduling.portage_template import portage_roster_sort_key  # noqa: E402
from lab_scheduler.staff import (  # noqa: E402
    DeactivationResult,
    StaffLifecycleError,
    VacatedShift,
    bulk_target_weekly_hours_options,
    create_vacant_line,
    deactivate_employee,
    fetch_archived_employees,
    fill_vacated_shift,
    log_audit_warning,
    log_snapshot_restore,
    update_employee_roster_line,
)
from lab_scheduler.data import (  # noqa: E402
    ExistingEmployeeRecord,
    RosterImportError,
    SnapshotError,
    build_import_preview,
    commit_import_preview,
    create_snapshot,
    list_recent_snapshots,
    next_employee_code,
    parse_roster_file,
    preview_from_dict,
    preview_to_dict,
    restore_snapshot,
)
from lab_scheduler.data.schedule_archive import (  # noqa: E402
    ScheduleArchiveError,
    default_saved_schedules_dir,
    export_period_schedule,
    import_period_schedule,
    list_named_archives,
    load_named_archive,
    save_named_archive,
    touch_schedule_reload_stamp,
)
from lab_scheduler.compliance.explainability import (  # noqa: E402
    format_rule_violation_tooltip,
    format_unfilled_slot_tooltip,
)
from lab_scheduler.simulation.load_test import (  # noqa: E402
    LoadTestSummary,
    run_portage_load_test,
)
from lab_scheduler.time import workweek_for  # noqa: E402
from lab_scheduler.finance.forecast import (  # noqa: E402
    DEFAULT_HOURLY_RATE_MLA,
    DEFAULT_HOURLY_RATE_MLT,
)
from lab_scheduler.engine.constraints import (
    IMPOSSIBLE_COVERAGE_TOOLTIP,
    assess_impossible_coverage_slots,
)
from lab_scheduler.engine.manager_dashboard import (
    ManagerHealthSnapshot,
    build_manager_health_snapshot,
    build_under_target_roster,
    count_open_shift_gaps,
    evaluate_period_coverage,
)
from lab_scheduler.engine.demand import infer_qual_code, is_transition_burnout_violation
from lab_scheduler.engine.swap_controller import (
    ScheduleState,
    _has_assignment_on_date,
    format_manual_assignment_warning,
    get_eligible_swap_candidates,
)
from lab_scheduler.policy import (
    CellMutation,
    PolicyViewModel,
    SchedulePolicyEngine,
    cell_mutation_from_dict,
    cell_mutation_to_dict,
    get_shortfall_fill_candidates,
)
from lab_scheduler.policy.frame_bridge import (
    assignments_from_schedule_frame,
    build_schedule_state_from_frame,
    count_open_shift_gaps_from_frame,
    normalize_grid_shift_token,
    schedule_frame_row_index_by_employee_id,
)
from lab_scheduler.ui.billing_ui import process_billing_checkout_trigger
from lab_scheduler.ui.business import render_business_section
from lab_scheduler.ui.business.navigation import (
    apply_pending_app_section,
    request_app_section,
    request_business_tab,
)
from lab_scheduler.ui.schedule_grid.bidi_bridge import (
    mount_lab_grid_storage_bridge as _mount_lab_grid_storage_bridge,
)
from lab_scheduler.ui.schedule_grid.browser_storage import (
    GRID_BROWSER_STORAGE_JS as _GRID_SHARED_SESSION_STORAGE_JS,
)
from lab_scheduler.ui.schedule_grid.component import (
    inject_ops_ribbon_live_metrics_listener as _inject_ops_ribbon_live_metrics_listener,
    inject_schedule_grid_layout_css as _inject_schedule_grid_layout_css,
    render_master_schedule_shift_grid as _render_master_schedule_shift_grid_impl,
    streamlit_html_component as _streamlit_html_component,
)
from lab_scheduler.ui.manager_tabs import (
    render_manager_analytics_tab,
    render_manager_print_tab,
)
from lab_scheduler.ui.save_pipeline import (
    ensure_schedule_tab_for_pending_save,
    handle_save_button_click,
    maybe_complete_deferred_save as _complete_deferred_save,
)
from lab_scheduler.ui import schedule_session as schedule_sess
from lab_scheduler.ui.ops_ribbon import (
    live_gap_count_from_draft,
    live_policy_view_from_draft,
    refresh_ops_ribbon_slot,
)
from lab_scheduler.ui.schedule_truth import (
    assignments_from_truth_frame,
    open_shift_gaps_from_frame,
    schedule_truth_frame,
)
from lab_scheduler.scheduling.schedule_tallies import (
    ALL_DAILY_TALLY_ROW_NAMES,
    count_shift_band_in_column,
    is_daily_tally_employee_id,
    is_daily_tally_row,
    shift_band_from_template_code,
    shift_target_for_date,
    weekday_day_tally_status,
)
from lab_scheduler.models.employee import (
    CONTRACT_LINE_TYPES,
    ensure_contract_line_schema,
    is_critical_contract_line_violation,
)
from lab_scheduler.validation import (  # noqa: E402
    UnionComplianceReport,
    generate_union_compliance_report,
)
from lab_scheduler.billing import (  # noqa: E402
    PREMIUM_PRICE_DISPLAY,
    PREMIUM_UPSELL_SHORT,
    TRIAL_MAX_EMPLOYEES,
    TRIAL_MAX_WEEKS,
    FeatureGates,
    TenantBilling,
    activate_tenant_subscription,
    apply_employee_cap,
    create_billing_checkout_session,
    create_billing_portal_session,
    create_mock_checkout_session,
    ensure_billing_schema,
    feature_gates_for_billing,
    fetch_tenant_billing,
    seed_default_billing_state,
    trial_period_end,
    use_mock_stripe,
)
from lab_scheduler.tenant.configuration import (  # noqa: E402
    MANAGER_MODE_KEY,
    ensure_tenant_configuration_schema,
    get_tenant_config_value,
    get_tenant_schedule_archetype,
    set_tenant_schedule_archetype,
)
from lab_scheduler.telemetry import (  # noqa: E402
    deploy_sentry_hotfix,
    fetch_sentry_logs,
    format_unified_patch_diff,
    generate_llm_diagnostic_packet,
    sentry_exception_guard,
    session_context_from_mapping,
    ensure_sentry_schema,
)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402


def _local_app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "LabStaffingScheduler"


def _sqlite_path_is_cloud_synced(db_path: Path) -> bool:
    normalized = str(db_path.resolve()).replace("\\", "/").lower()
    markers = ("onedrive", "dropbox", "google drive", "icloud")
    return any(marker in normalized for marker in markers)


def _sqlite_integrity_ok(db_path: Path) -> bool:
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row and str(row[0]).lower() == "ok")
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _quarantine_corrupt_db(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = db_path.with_name(f"{db_path.name}.corrupt-{stamp}")
    db_path.rename(quarantine)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.is_file():
            sidecar.rename(quarantine.with_name(quarantine.name + suffix))
    return quarantine


def _project_roster_backup_path() -> Optional[Path]:
    candidates = sorted(ROOT.glob("demo.sqlite3.bak-*"), reverse=True)
    for candidate in candidates:
        if _sqlite_integrity_ok(candidate):
            return candidate
    return None


def _northstar_active_employee_count(db_path: Path) -> int:
    if not db_path.is_file() or not _sqlite_integrity_ok(db_path):
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM employees
                WHERE tenant_id = ? AND is_active = 1
                """,
                (NORTHSTAR_TENANT_ID,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _maybe_restore_from_project_backup(db_path: Path) -> None:
    """Recover the Portage roster when a fresh seed DB replaced a full workspace."""

    if _northstar_active_employee_count(db_path) > 3:
        return
    backup = _project_roster_backup_path()
    if backup is None:
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pre_restore = db_path.with_name(f"{db_path.name}.pre-restore-{stamp}")
        try:
            shutil.copy2(db_path, pre_restore)
        except OSError:
            pass
    shutil.copy2(backup, db_path)
    st.session_state["lab_roster_restore_notice"] = (
        f"Restored the Portage roster (25 staff) from `{backup.name}`. "
        "Any edits made after that backup date may still be missing."
    )


def _resolve_db_path() -> Path:
    env_path = os.environ.get("LAB_SCHEDULER_DB_PATH", "").strip()
    if env_path:
        return Path(env_path)

    local_db = _local_app_data_dir() / "demo.sqlite3"
    legacy_db = ROOT / "demo.sqlite3"
    if local_db.is_file():
        return local_db
    if legacy_db.is_file():
        if _sqlite_integrity_ok(legacy_db):
            local_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_db, local_db)
            return local_db
        try:
            _quarantine_corrupt_db(legacy_db)
        except OSError:
            pass
    return local_db


DB_PATH = _resolve_db_path()
SNAPSHOTS_DIR = ROOT / "snapshots"
SAVED_SCHEDULES_DIR = default_saved_schedules_dir(ROOT)
NORTHSTAR_TENANT_ID = "tenant-northstar-lab"
SOUTHBRIDGE_TENANT_ID = "tenant-southbridge-health"
DEFAULT_NORTHSTAR_PERIOD_ID = "period-2026-summer"
_DEMO_OPS_TENANT_IDS = frozenset({NORTHSTAR_TENANT_ID, SOUTHBRIDGE_TENANT_ID})
SCHEDULE_GRID_ANCHOR = "schedule_grid_anchor"
# Master rotations (8-week) show in one scrollable grid; longer periods split into 8-week chunks.
SCHEDULE_GRID_VIEW_WEEKS = 8
QUALIFICATION_CODES: Tuple[str, ...] = ("MLT", "MLA")
ALLOWED_SHIFT_CODES: Tuple[str, ...] = ("D", "E", "N", "M")
EMPTY_SHIFT_DISPLAY = EXPORT_EMPTY_SHIFT_DISPLAY
SHIFT_EDITOR_OPTIONS: Tuple[str, ...] = (
    EMPTY_SHIFT_DISPLAY,
    "D",
    "E",
    "N",
    OFF_CODE_VACATION,
    OFF_CODE_SICK,
)

if any("manager_app" in str(arg) for arg in sys.argv):
    os.environ.setdefault("LAB_SCHEDULER_MANAGER_ENTRY", "1")


def _is_production_runtime() -> bool:
    return os.environ.get("LAB_SCHEDULER_ENV", "development").strip().lower() == "production"


def _is_authenticated_session() -> bool:
    return bool(st.session_state.get("authenticated"))


def _production_requires_login() -> bool:
    return _is_production_runtime() and not _is_authenticated_session()


def _manager_entry_requested() -> bool:
    if os.environ.get("LAB_SCHEDULER_MANAGER_ENTRY", "").strip() == "1":
        return True
    return bool(st.session_state.get("manager_mode"))


def _tenant_exists(conn: sqlite3.Connection, tenant_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return row is not None


def _demo_northstar_password() -> str | None:
    for username, password, *_rest in default_test_accounts():
        if username == "northstar_admin":
            return password
    return None


def _apply_demo_manager_session(conn: sqlite3.Connection, tenant_id: str) -> None:
    st.session_state["authenticated"] = True
    st.session_state["tenant_id"] = tenant_id
    st.session_state.setdefault("username", "northstar_admin")
    if not st.session_state.get("tenant_name"):
        row = conn.execute("SELECT name FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        st.session_state["tenant_name"] = row[0] if row else "Northstar Regional Lab"
    if _manager_entry_requested():
        st.session_state["manager_mode"] = True


def _resolve_local_tenant_id(conn: sqlite3.Connection) -> str:
    """Return a usable tenant id for the local demo/manager app."""
    existing = st.session_state.get("tenant_id")
    if existing and _tenant_exists(conn, str(existing)):
        if _is_production_runtime() and not _is_authenticated_session():
            for key in ("tenant_id", "tenant_name", "tenant_slug", "username", "display_name", "account_id"):
                st.session_state.pop(key, None)
        else:
            if _manager_entry_requested():
                st.session_state["manager_mode"] = True
            return str(existing)

    if _is_production_runtime():
        if _is_authenticated_session():
            tenant = st.session_state.get("tenant_id")
            if tenant and _tenant_exists(conn, str(tenant)):
                if _manager_entry_requested():
                    st.session_state["manager_mode"] = True
                return str(tenant)
        return ""

    try:
        demo_password = _demo_northstar_password()
        if demo_password and _attempt_login(conn, username="northstar_admin", password=demo_password):
            logged_in = st.session_state.get("tenant_id")
            if logged_in and _tenant_exists(conn, str(logged_in)):
                if _manager_entry_requested():
                    st.session_state["manager_mode"] = True
                return str(logged_in)
    except Exception:
        UI_LOGGER.exception("Demo auto-login failed; falling back to Northstar tenant id")

    _apply_demo_manager_session(conn, NORTHSTAR_TENANT_ID)
    return NORTHSTAR_TENANT_ID


def _drag_fill_palette_tokens_for_contract_line(contract_line: object) -> Tuple[str, ...]:
    """Worked/off tokens offered by drag-fill for a roster line (matches cell dropdowns)."""

    line = str(contract_line or "D/E").strip().upper()
    if line == "D/N":
        return (EMPTY_SHIFT_DISPLAY, "D", "N")
    return (EMPTY_SHIFT_DISPLAY, "D", "E")


def _shift_editor_options_for_contract_line(contract_line: object) -> Tuple[str, ...]:
    """Contract band options: D/E lines get D+E only; D/N lines get D+N only."""

    line = str(contract_line or "D/E").strip().upper()
    off_options = (EMPTY_SHIFT_DISPLAY, OFF_CODE_VACATION, OFF_CODE_SICK)
    worked = _drag_fill_palette_tokens_for_contract_line(line)[1:]
    return off_options[:1] + worked + off_options[1:]

# Shift cell palette for the master schedule grid (short code -> styles).
SHIFT_CELL_STYLES: Dict[str, Dict[str, str]] = {
    "D": {"bg": "#dbeafe", "fg": "#1e3a8a", "label": "Day"},
    "M": {"bg": "#dbeafe", "fg": "#1e3a8a", "label": "Morning"},
    "E": {"bg": "#fef3c7", "fg": "#78350f", "label": "Evening"},
    "N": {"bg": "#1e293b", "fg": "#f8fafc", "label": "Night"},
    OFF_CODE_VACATION: {"bg": "#fef9c3", "fg": "#854d0e", "label": "Off (Vacation)"},
    OFF_CODE_SICK: {"bg": "#fce7f3", "fg": "#9d174d", "label": "Off (Sick)"},
    ".": {"bg": "#ffffff", "fg": "#64748b", "label": "Off"},
}


def _is_demo_ops_tenant(tenant_id: str) -> bool:
    return tenant_id in _DEMO_OPS_TENANT_IDS


def _is_self_serve_trial(tenant_id: str, gates: FeatureGates) -> bool:
    return gates.is_trial_tier and not _is_demo_ops_tenant(tenant_id)


def _business_console_allowed(tenant_id: str, gates: FeatureGates) -> bool:
    """Revenue pipeline / Business CRM is operator-only — not self-serve trials."""

    if _is_demo_ops_tenant(tenant_id):
        return True
    return not _is_self_serve_trial(tenant_id, gates)


def _is_manager_mode(conn: sqlite3.Connection, tenant_id: str) -> bool:
    """Manager-first UI: hide ops/dev surfaces (default on except demo ops tenants)."""

    if st.session_state.get("force_ops_console"):
        return False
    if _manager_entry_requested():
        return True
    if _is_demo_ops_tenant(tenant_id):
        return False
    if "manager_mode" in st.session_state:
        return bool(st.session_state["manager_mode"])
    configured = get_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=MANAGER_MODE_KEY,
        default="true",
    )
    return str(configured or "true").lower() in {"1", "true", "yes", "on"}


def _manager_workspace_tab_key(period_id: str) -> str:
    return schedule_sess.manager_workspace_tab_key(period_id)


@dataclass(frozen=True)
class SchedulePostingReadiness:
    is_ready: bool
    attention_bullets: Tuple[str, ...]
    using_preview: bool
    last_persist_ok: bool
    has_failed_preview_available: bool
    hours_delta: float
    below_evening_days: int
    below_night_days: int
    pending_mutations: int


def _assignments_to_planned_for_tallies(assignments: List[Dict]) -> List:
    from lab_scheduler.scheduling.models import PlannedAssignment

    return [
        PlannedAssignment(
            employee_id=str(row["employee_id"]),
            shift_template_id=str(row["shift_template_id"]),
            assignment_date=row["assignment_date"],
        )
        for row in assignments
    ]


def _evaluate_schedule_posting_readiness(
    *,
    assignments: List[Dict],
    employees: List[Dict],
    period: TenantPeriod,
    template_info: Dict[str, ShiftTemplateInfo],
    hours_delta: float,
    using_preview: bool = False,
    pending_mutations: int = 0,
    check_portage_tallies: bool = False,
    schedule_frame: Optional[pd.DataFrame] = None,
    templates: Optional[Dict[str, Dict]] = None,
    dates: Optional[List[date]] = None,
) -> SchedulePostingReadiness:
    del using_preview
    tally_source = assignments
    if schedule_frame is not None and templates is not None and dates is not None:
        tally_source = assignments_from_truth_frame(
            schedule_frame,
            employees=employees,
            dates=dates,
            templates=templates,
        )
    below_e = 0
    below_n = 0
    if check_portage_tallies and template_info:
        below_e, below_n = count_subfloor_evening_night_days(
            assignments=_assignments_to_planned_for_tallies(tally_source),
            shift_templates=template_info,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
        )

    bullets: List[str] = []
    if abs(hours_delta) > FULLTIME_CONTRACT_HOUR_TOLERANCE:
        bullets.append(f"{hours_delta:+.0f}h contract hours vs roster targets")
    if below_n > 0:
        bullets.append(f"{below_n} night day(s) below 2/2")
    if below_e > 0:
        bullets.append(f"{below_e} evening day(s) below 2/2")
    if pending_mutations > 0:
        bullets.append(
            f"{pending_mutations} unpublished grid edit"
            f"{'' if pending_mutations == 1 else 's'}"
        )

    is_ready = (
        abs(hours_delta) <= FULLTIME_CONTRACT_HOUR_TOLERANCE
        and below_n == 0
        and below_e == 0
        and pending_mutations == 0
    )
    return SchedulePostingReadiness(
        is_ready=is_ready,
        attention_bullets=tuple(bullets),
        using_preview=False,
        last_persist_ok=True,
        has_failed_preview_available=False,
        hours_delta=hours_delta,
        below_evening_days=below_e,
        below_night_days=below_n,
        pending_mutations=pending_mutations,
    )


def _render_schedule_health_panel(
    *,
    period: TenantPeriod,
    snapshot: ScheduleHealthSnapshot,
    dates: List[date],
) -> None:
    """Live draft health summary above the manager schedule grid."""

    st.markdown(f"#### Schedule health · {html_lib.escape(period.name)}")

    metric_evenings, metric_nights, metric_compliance, metric_unsaved = st.columns(4)
    with metric_evenings:
        st.metric(
            "Evenings off",
            snapshot.evening_violation_days,
            help="Days where total evening shifts are not 2/2 on the clinical floor.",
        )
    with metric_nights:
        st.metric(
            "Nights off",
            snapshot.night_violation_days,
            help="Days where total night shifts are not 2/2 on the clinical floor.",
        )
    with metric_compliance:
        st.metric(
            "Compliance errors",
            snapshot.compliance_error_count,
            help="Union and labor rule violations on this draft.",
        )
    with metric_unsaved:
        unsaved_label = "OK" if snapshot.pending_mutations == 0 else str(snapshot.pending_mutations)
        st.metric(
            "Unsaved edits",
            unsaved_label,
            help="Staged grid changes not yet written to the database.",
        )

    floor_ok = snapshot.is_operational_floor_ok
    compliance_ok = snapshot.compliance_error_count == 0
    edits_ok = snapshot.pending_mutations == 0
    if floor_ok and compliance_ok and edits_ok:
        st.success("This draft passes operational floor, compliance, and save checks.")
    else:
        parts: List[str] = []
        if not floor_ok:
            parts.append(
                f"{len(snapshot.tally_issues)} evening/night day-band issue"
                f"{'' if len(snapshot.tally_issues) == 1 else 's'}"
            )
        if not compliance_ok:
            parts.append(
                f"{snapshot.compliance_error_count} compliance error"
                f"{'' if snapshot.compliance_error_count == 1 else 's'}"
            )
        if not edits_ok:
            parts.append(
                f"{snapshot.pending_mutations} unsaved edit"
                f"{'' if snapshot.pending_mutations == 1 else 's'}"
            )
        st.warning("Needs attention: " + "; ".join(parts) + ".")

    if snapshot.pending_mutations > 0:
        emphasis = (
            " Review before using Distribute or Fill alternate."
            if snapshot.pending_mutations >= 50
            else ""
        )
        st.warning(
            f"You have **{snapshot.pending_mutations}** unstaged change"
            f"{'' if snapshot.pending_mutations == 1 else 's'}. "
            "Save or **Clear schedule** before running auto-fill tools."
            f"{emphasis}"
        )

    top_issues = snapshot.tally_issues[:5]
    if top_issues:
        st.markdown("**Operational floor issues**")
        for index, issue in enumerate(top_issues):
            issue_col, action_col = st.columns([5, 1])
            issue_col.markdown(f"⚠ {format_tally_issue_message(issue)}")
            with action_col:
                if st.button(
                    "Go",
                    key=f"health_go_{period.id}_{issue.assignment_date.isoformat()}_{issue.band}_{index}",
                    help="Jump the grid to the week chunk containing this date.",
                    width="stretch",
                ):
                    chunk_index = chunk_index_for_date(dates, issue.assignment_date)
                    st.session_state[_schedule_view_chunk_key(period.id)] = chunk_index
                    st.session_state[_schedule_health_focus_date_key(period.id)] = (
                        issue.assignment_date.isoformat()
                    )
                    _invalidate_schedule_matrix_view_cache(period.id)
                    st.rerun()
        if len(snapshot.tally_issues) > 5:
            st.caption(
                f"Showing 5 of {len(snapshot.tally_issues)} day-band mismatches. "
                "Check footer reds in the grid for the full list."
            )

    equity_notes: List[str] = []
    if snapshot.equity_evening_mismatch_lines:
        equity_notes.append(
            "Evening targets: " + " · ".join(snapshot.equity_evening_mismatch_lines[:4])
        )
    if snapshot.equity_drift_lines:
        equity_notes.append(
            "Equity drift: " + ", ".join(snapshot.equity_drift_lines[:4])
        )
    if equity_notes:
        st.caption(" · ".join(equity_notes))

    if snapshot.de_evening_pattern_lines:
        st.markdown("**Evening rotation pattern**")
        for line in snapshot.de_evening_pattern_lines[:5]:
            st.caption(f"⚠ {html_lib.escape(line)}")


def _workspace_publish_state(
    *,
    period_id: str,
    audit_summary: ComplianceAuditSummary,
) -> Dict[str, object]:
    del period_id
    coverage = audit_summary.coverage
    return {
        "persist_ok": True,
        "required_filled": int(coverage.filled_slots),
        "required_total": int(coverage.total_shift_slots),
        "violation_codes": {},
        "saved_coverage_pct": float(coverage.coverage_pct),
        "saved_filled": int(coverage.filled_slots),
        "saved_total": int(coverage.total_shift_slots),
    }


def _render_manager_preview_status_card(
    *,
    period: TenantPeriod,
    publish_state: Mapping[str, object],
    policy_view: Optional[PolicyViewModel] = None,
    posting_readiness: Optional[SchedulePostingReadiness] = None,
) -> None:
    """Manager banner: saved schedule, staged edits, or export blockers."""

    saved_pct = float(publish_state.get("saved_coverage_pct") or 0.0)
    saved_filled = int(publish_state.get("saved_filled") or 0)
    saved_total = int(publish_state.get("saved_total") or 0)
    pending_count = len(policy_view.pending_mutations) if policy_view else 0

    if pending_count > 0:
        st.warning(
            f"**Unsaved edits ({pending_count})** — click **Save** "
            "to write the database and refresh the standard JSON backup."
        )
    elif posting_readiness is not None and posting_readiness.is_ready:
        st.success(
            f"**Saved schedule** for **{html_lib.escape(period.name)}** — "
            f"{saved_filled}/{saved_total} slots ({saved_pct:.0f}% filled). "
            "Breakroom export is ready."
        )
    elif posting_readiness is not None and posting_readiness.attention_bullets:
        bullet_lines = "\n".join(f"- {item}" for item in posting_readiness.attention_bullets)
        st.warning(
            f"**Schedule saved; breakroom export blocked**\n\n{bullet_lines}"
        )
    else:
        st.success(
            f"**Saved schedule** for **{html_lib.escape(period.name)}** — "
            f"{saved_filled}/{saved_total} slots in the database ({saved_pct:.0f}% filled)."
        )
    st.caption(
        "Row targets show **contract FTE hours** (e.g. 320h). "
        "Manual edits stay staged until **Save**; breakroom export requires tallies and contract checks."
    )


def _render_manager_print_tab(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
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
    schedule_archetype: str = "STANDARD",
    posting_readiness: Optional[SchedulePostingReadiness] = None,
) -> None:
    del conn, tenant_id
    render_manager_print_tab(
        period=period,
        facility_name=facility_name,
        export_employees=export_employees,
        dates=dates,
        assignments=assignments,
        templates=templates,
        blocked_map=blocked_map,
        gates=gates,
        rules=rules,
        qual_codes=qual_codes,
        qual_ids_by_employee=qual_ids_by_employee,
        contract_target_hours=contract_target_hours,
        publish_state=publish_state,
        schedule_archetype=schedule_archetype,
        posting_readiness=posting_readiness,
        build_breakroom_document=_build_breakroom_print_document,
        breakroom_posting_context=breakroom_posting_context_from_publish_state,
    )


def _render_manager_analytics_tab(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    assignments: List[Dict],
    compliance_report: ComplianceReport,
    gates: FeatureGates,
    target_hours: Mapping[str, float],
    display_target_hours: Mapping[str, float],
    policy_view: PolicyViewModel,
    display_draft: pd.DataFrame,
    draft: pd.DataFrame,
    dates: List[date],
    emp_quals: Mapping[str, Set[str]],
    qual_codes: Mapping[str, str],
    roster_by_id: Dict[str, Dict],
) -> None:
    del (
        target_hours,
        display_target_hours,
        templates,
        assignments,
        compliance_report,
        gates,
    )

    employee_stats_df = _build_employee_analytics_dataframe(
        draft_frame=draft,
        employees=employees,
        dates=dates,
        period=period,
        rules=rules,
        emp_quals=emp_quals,
        qual_codes=qual_codes,
        contract_rows=policy_view.contract_rows,
    )
    meta_view, meta_column_config, meta_columns = _build_roster_meta_view(display_draft)
    metadata_editor_key = f"schedule_metadata_editor_{period.id}"
    edited_meta = render_manager_analytics_tab(
        employee_stats_df=employee_stats_df,
        meta_view=meta_view,
        meta_column_config=meta_column_config,
        metadata_editor_key=metadata_editor_key,
    )
    baseline_meta = draft[
        [column for column in meta_columns if column in draft.columns]
    ].reset_index(drop=True)
    edited_meta = edited_meta.reset_index(drop=True)
    if not edited_meta.equals(baseline_meta):
        if _process_inline_metadata_edits(
            conn,
            tenant_id=tenant_id,
            period=period,
            rules=rules,
            roster_by_id=roster_by_id,
            baseline=draft,
            edited=edited_meta,
        ):
            st.session_state.pop(metadata_editor_key, None)
            st.session_state[f"schedule_sync_{period.id}"] = True
            st.rerun()


def _filter_assignments_through(
    end_inclusive: date,
    assignments: List[Dict],
) -> List[Dict]:
    return [
        assignment
        for assignment in assignments
        if assignment["assignment_date"] <= end_inclusive
    ]


def _friendly_period_label(period: TenantPeriod) -> str:
    return (
        f"{period.name} · {period.period_start.strftime('%b %d')} – "
        f"{period.period_end_inclusive.strftime('%b %d, %Y')}"
    )


def _prepare_workspace_scope(
    period: TenantPeriod,
    employees: List[Dict],
    assignments: List[Dict],
    gates: FeatureGates,
) -> Tuple[TenantPeriod, List[Dict], List[Dict]]:
    effective_period, effective_employees = _effective_period_and_employees(
        period,
        employees,
        gates,
    )
    if gates.is_premium:
        return period, employees, assignments
    scoped_assignments = _filter_assignments_through(
        effective_period.period_end_inclusive,
        assignments,
    )
    return effective_period, effective_employees, scoped_assignments


def _inject_global_ui_styles() -> None:
    st.markdown(
        """
        <style>
          .lab-badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            line-height: 1.4;
            white-space: nowrap;
          }
          .lab-badge-ok { background: #dcfce7; color: #166534; border: 1px solid #86efac; }
          .lab-badge-warn { background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; }
          .lab-badge-danger { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
          .lab-badge-info { background: #dbeafe; color: #1e40af; border: 1px solid #93c5fd; }
          .lab-micro-banner {
            padding: 10px 14px;
            border-radius: 8px;
            margin: 6px 0;
            font-size: 13px;
            line-height: 1.45;
            border-left: 4px solid transparent;
          }
          .lab-micro-error {
            background: #fef2f2;
            border-left-color: #dc2626;
            color: #7f1d1d;
          }
          .lab-micro-warn {
            background: #fffbeb;
            border-left-color: #d97706;
            color: #78350f;
          }
          .lab-micro-success {
            background: #f0fdf4;
            border-left-color: #16a34a;
            color: #14532d;
          }
          .lab-micro-info {
            background: #eff6ff;
            border-left-color: #2563eb;
            color: #1e3a8a;
          }
          .lab-micro-impossible {
            background: #fef2f2;
            border: 2px solid #dc2626;
            border-left: 4px solid #dc2626;
            color: #7f1d1d;
          }
          .lab-health-banner {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            margin: 12px 0 16px;
          }
          @media (max-width: 900px) {
            .lab-health-banner { grid-template-columns: 1fr; }
          }
          .lab-health-card {
            border-radius: 12px;
            padding: 14px 16px;
            border: 1px solid #e2e8f0;
            background: #f8fafc;
          }
          .lab-health-card-healthy {
            border-color: #86efac;
            background: #f0fdf4;
          }
          .lab-health-card-warn {
            border-color: #fcd34d;
            background: #fffbeb;
          }
          .lab-health-label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #64748b;
            font-weight: 700;
          }
          .lab-health-value {
            font-size: 26px;
            font-weight: 800;
            margin-top: 4px;
            line-height: 1.1;
          }
          .lab-health-sub {
            font-size: 12px;
            color: #475569;
            margin-top: 4px;
          }
          .lab-prioritize-banner {
            border: 2px solid #2563eb;
            background: #eff6ff;
            border-radius: 10px;
            padding: 10px 14px;
            margin: 8px 0 12px;
            color: #1e3a8a;
            font-size: 13px;
          }
          .lab-under-target-row {
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 10px 12px;
            margin-bottom: 8px;
            background: #fff;
          }
          .lab-schedule-wrap {
            overflow: auto;
            max-height: min(72vh, 820px);
            border: 1px solid var(--lab-grid-border, #e2e8f0);
            border-radius: 12px;
            background: var(--lab-grid-bg, #ffffff);
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.08);
            margin-bottom: 8px;
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
          }
          .lab-schedule-wrap {
            --lab-grid-bg: #ffffff;
            --lab-grid-header-bg: #0f172a;
            --lab-grid-header-fg: #f8fafc;
            --lab-grid-emp-bg: #f8fafc;
            --lab-grid-emp-fg: #0f172a;
            --lab-grid-border: #e2e8f0;
            --lab-grid-cell-bg: #ffffff;
            --lab-grid-weekend-bg: #f8fafc;
            --lab-pill-off-bg: #f1f5f9;
            --lab-pill-off-fg: #64748b;
          }
          @media (prefers-color-scheme: dark) {
            .lab-schedule-wrap {
              --lab-grid-bg: #0f172a;
              --lab-grid-header-bg: #1e293b;
              --lab-grid-header-fg: #f1f5f9;
              --lab-grid-emp-bg: #1e293b;
              --lab-grid-emp-fg: #e2e8f0;
              --lab-grid-border: #334155;
              --lab-grid-cell-bg: #0f172a;
              --lab-grid-weekend-bg: #111827;
              --lab-pill-off-bg: #334155;
              --lab-pill-off-fg: #94a3b8;
            }
          }
          .lab-schedule-grid {
            border-collapse: separate;
            border-spacing: 0;
            width: max-content;
            min-width: 100%;
            table-layout: fixed;
            font-size: 12px;
            background: var(--lab-grid-bg, #ffffff);
          }
          .lab-schedule-grid thead th {
            position: sticky;
            top: 0;
            z-index: 2;
            background: var(--lab-grid-header-bg, #0f172a);
            color: var(--lab-grid-header-fg, #f8fafc);
            font-weight: 700;
            text-align: center;
            padding: 8px 4px;
            border-bottom: 2px solid var(--lab-grid-border, #334155);
            border-right: 1px solid var(--lab-grid-border, #334155);
            white-space: nowrap;
            line-height: 1.25;
          }
          .lab-schedule-grid thead th.lab-emp-col {
            position: sticky;
            left: 0;
            z-index: 4;
            text-align: left;
            padding-left: 14px;
            width: 300px;
            min-width: 300px;
            background: var(--lab-grid-header-bg, #0f172a);
          }
          .lab-schedule-grid thead th.lab-day-col {
            width: 64px;
            min-width: 64px;
            max-width: 64px;
          }
          .lab-schedule-grid thead th.lab-weekend-col {
            background: #475569;
            color: #f8fafc;
          }
          .lab-schedule-grid tbody td {
            border-bottom: 1px solid var(--lab-grid-border, #e2e8f0);
            border-right: 1px solid var(--lab-grid-border, #e2e8f0);
            padding: 0;
            vertical-align: middle;
            background: var(--lab-grid-cell-bg, #ffffff);
          }
          .lab-schedule-grid tbody td.lab-emp-col {
            position: sticky;
            left: 0;
            z-index: 3;
            padding: 8px 12px;
            font-weight: 600;
            color: var(--lab-grid-emp-fg, #0f172a);
            background: var(--lab-grid-emp-bg, #f8fafc);
            width: 300px;
            min-width: 300px;
            white-space: normal;
            overflow: visible;
            text-overflow: clip;
            box-shadow: 2px 0 4px rgba(15, 23, 42, 0.06);
          }
          .lab-schedule-grid tbody tr:hover td.lab-emp-col {
            background: var(--lab-grid-emp-bg, #f1f5f9);
          }
          .lab-schedule-grid tbody td.lab-day-col {
            text-align: center;
            padding: 4px 2px;
          }
          .lab-schedule-grid tbody td.lab-weekend-col {
            background: var(--lab-grid-weekend-bg, #f8fafc);
          }
          .lab-schedule-grid .lab-week-start {
            border-left: 2px solid #3b82f6 !important;
          }
          .lab-schedule-grid .lab-health-focus-col {
            box-shadow: inset 0 0 0 2px #f59e0b;
            background: rgba(245, 158, 11, 0.12) !important;
          }
          .lab-schedule-grid thead th.lab-health-focus-col {
            background: #b45309 !important;
            color: #fffbeb !important;
          }
          .lab-shift-cell {
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 36px;
            width: 52px;
            font-weight: 800;
            letter-spacing: 0.06em;
            margin: 0 auto;
          }
          .lab-shift-pill {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 40px;
            width: 44px;
            height: 32px;
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: 8px;
            font-weight: 800;
            font-size: 12px;
            letter-spacing: 0.06em;
            line-height: 1;
            padding: 0;
            margin: 0 auto;
            background: var(--lab-pill-off-bg, #f1f5f9);
            color: var(--lab-pill-off-fg, #64748b);
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
          }
          .lab-shift-pill-off {
            background: var(--lab-pill-off-bg, #f1f5f9) !important;
            color: var(--lab-pill-off-fg, #64748b) !important;
            border-color: var(--lab-grid-border, #e2e8f0);
          }
          .lab-shift-pill-d {
            background-color: #dbeafe !important;
            color: #1e3a8a !important;
          }
          .lab-shift-pill-e {
            background-color: #fef3c7 !important;
            color: #78350f !important;
          }
          .lab-shift-pill-n {
            background-color: #1e293b !important;
            color: #f8fafc !important;
          }
          .lab-shift-pill-editable {
            cursor: pointer;
          }
          .lab-shift-pill-editable:hover {
            outline: 2px solid #2563eb;
            outline-offset: 1px;
          }
          .lab-shift-pill-readonly {
            cursor: default;
            pointer-events: none;
          }
          .lab-shift-popover {
            position: absolute;
            z-index: 1000;
            background: var(--lab-grid-bg, #ffffff);
            border: 1px solid var(--lab-grid-border, #e2e8f0);
            border-radius: 10px;
            padding: 8px;
            box-shadow: 0 8px 24px rgba(15, 23, 42, 0.18);
          }
          .lab-shift-popover select {
            min-width: 72px;
            min-height: 36px;
            border-radius: 8px;
            border: 1px solid var(--lab-grid-border, #e2e8f0);
            font-weight: 700;
            font-size: 12px;
            padding: 4px 8px;
            background: var(--lab-grid-bg, #ffffff);
            color: var(--lab-grid-emp-fg, #0f172a);
          }
          .lab-schedule-wrap.lab-edit-mode {
            outline: 1px dashed #2563eb;
            outline-offset: 2px;
          }
          .lab-shift-select {
            width: 100%;
            min-height: 36px;
            border: 1px solid rgba(15, 23, 42, 0.12);
            border-radius: 8px;
            font-weight: 800;
            font-size: 12px;
            letter-spacing: 0.06em;
            text-align: center;
            text-align-last: center;
            cursor: pointer;
            padding: 4px 2px;
            appearance: none;
            -webkit-appearance: none;
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
          }
          .lab-shift-select.lab-shift-token-d {
            background-color: #dbeafe !important;
            color: #1e3a8a !important;
          }
          .lab-shift-select.lab-shift-token-e {
            background-color: #fef3c7 !important;
            color: #78350f !important;
          }
          .lab-shift-select.lab-shift-token-n {
            background-color: #1e293b !important;
            color: #f8fafc !important;
          }
          .lab-shift-select:focus {
            outline: 2px solid #2563eb;
            outline-offset: 1px;
          }
          .lab-emp-sub {
            display: block;
            margin-top: 4px;
            font-size: 11px;
            font-weight: 500;
            color: var(--lab-pill-off-fg, #64748b);
            line-height: 1.35;
          }
          .lab-emp-stats {
            font-variant-numeric: tabular-nums;
          }
          .lab-emp-primary {
            font-size: 12px;
            font-weight: 700;
            color: var(--lab-grid-emp-fg, #0f172a);
            line-height: 1.3;
          }
          .lab-legend-chip {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-right: 14px;
            margin-bottom: 6px;
            font-size: 12px;
            color: #475569;
          }
          .lab-legend-swatch {
            width: 28px;
            height: 22px;
            border-radius: 6px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 11px;
            border: 1px solid rgba(15, 23, 42, 0.08);
          }
          .lab-finance-banner {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px;
            margin: 0 0 20px 0;
          }
          @media (max-width: 900px) {
            .lab-finance-banner { grid-template-columns: 1fr; }
          }
          .lab-finance-card {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 18px 20px;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
          }
          .lab-finance-label {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #64748b;
          }
          .lab-finance-value {
            font-size: 28px;
            font-weight: 800;
            color: #0f172a;
            margin: 6px 0 2px;
            line-height: 1.1;
          }
          .lab-finance-sub {
            font-size: 13px;
            color: #475569;
            line-height: 1.4;
          }
          .lab-login-wrap {
            max-width: 420px;
            margin: 48px auto 24px;
            padding: 28px 32px 24px;
            border: 1px solid #e2e8f0;
            border-radius: 16px;
            background: #ffffff;
            box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
          }
          .lab-login-title {
            font-size: 22px;
            font-weight: 800;
            color: #0f172a;
            margin: 0 0 6px;
          }
          .lab-login-sub {
            font-size: 13px;
            color: #64748b;
            margin: 0 0 20px;
            line-height: 1.45;
          }
          .lab-billing-panel {
            background: linear-gradient(165deg, #1e293b 0%, #0f172a 100%);
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 14px 16px;
            margin: 10px 0 6px;
            color: #e2e8f0;
          }
          .lab-billing-title {
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            color: #94a3b8;
            margin-bottom: 8px;
          }
          .lab-billing-plan {
            font-size: 15px;
            font-weight: 700;
            color: #f8fafc;
            margin-bottom: 4px;
          }
          .lab-billing-meta {
            font-size: 12px;
            color: #94a3b8;
            line-height: 1.45;
          }
          .lab-billing-upgrade-note {
            font-size: 12px;
            color: #cbd5e1;
            line-height: 1.5;
            margin: 10px 0 0;
            padding-top: 10px;
            border-top: 1px solid #334155;
          }
          .lab-conversion-panel {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 14px 16px;
            margin: 0 0 12px;
          }
          .lab-conversion-title {
            font-size: 13px;
            font-weight: 700;
            color: #0f172a;
            margin-bottom: 6px;
          }
          .lab-conversion-copy {
            font-size: 13px;
            color: #475569;
            margin: 0;
            line-height: 1.5;
          }
          .lab-billing-checkout {
            background: #0b1220;
            border: 1px dashed #475569;
            border-radius: 10px;
            padding: 12px 14px;
            margin-top: 10px;
            font-size: 12px;
            color: #cbd5e1;
            line-height: 1.5;
            word-break: break-all;
          }
          .lab-premium-lock {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 12px 14px;
            color: #e2e8f0;
            font-size: 13px;
            line-height: 1.45;
          }
          .lab-sickcall-card {
            background: linear-gradient(160deg, #450a0a 0%, #1e1b4b 100%);
            border: 1px solid #f87171;
            border-radius: 14px;
            padding: 18px 20px;
            margin: 16px 0;
            color: #fef2f2;
            box-shadow: 0 0 0 1px rgba(248, 113, 113, 0.15), 0 12px 28px rgba(15, 23, 42, 0.35);
          }
          .lab-sickcall-title {
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #fecaca;
            margin-bottom: 8px;
          }
          .lab-sickcall-headline {
            font-size: 18px;
            font-weight: 800;
            color: #fff7ed;
            margin-bottom: 6px;
          }
          .lab-sickcall-meta {
            font-size: 13px;
            color: #fecaca;
            margin-bottom: 14px;
            line-height: 1.45;
          }
          .lab-sickcall-candidate {
            display: block;
            background: rgba(15, 23, 42, 0.55);
            border: 1px solid rgba(254, 202, 202, 0.35);
            border-radius: 10px;
            padding: 10px 12px;
            margin: 8px 0;
            color: #fff7ed;
            font-size: 13px;
            line-height: 1.4;
          }
          .lab-swap-glow {
            box-shadow: 0 0 0 2px #22c55e, 0 0 14px rgba(34, 197, 94, 0.55);
            border-radius: 8px;
            animation: lab-swap-pulse 1.2s ease-in-out infinite;
          }
          @keyframes lab-swap-pulse {
            0%, 100% { box-shadow: 0 0 0 2px #22c55e, 0 0 10px rgba(34, 197, 94, 0.35); }
            50% { box-shadow: 0 0 0 3px #4ade80, 0 0 18px rgba(74, 222, 128, 0.65); }
          }
          .lab-swap-panel {
            background: linear-gradient(160deg, #0f172a 0%, #1e3a8a 100%);
            border: 1px solid #60a5fa;
            border-radius: 14px;
            padding: 18px 20px;
            margin: 12px 0 16px;
            color: #eff6ff;
            box-shadow: 0 0 0 1px rgba(96, 165, 250, 0.15), 0 12px 28px rgba(15, 23, 42, 0.35);
          }
          .lab-swap-panel-title {
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #93c5fd;
            margin-bottom: 8px;
          }
          .lab-swap-panel-headline {
            font-size: 18px;
            font-weight: 800;
            color: #f8fafc;
            margin-bottom: 6px;
          }
          .lab-swap-panel-meta {
            font-size: 14px;
            font-weight: 600;
            color: #e2e8f0;
            margin-bottom: 12px;
            line-height: 1.55;
          }
          .lab-swap-candidate {
            display: block;
            background: rgba(15, 23, 42, 0.55);
            border: 1px solid rgba(147, 197, 253, 0.35);
            border-radius: 10px;
            padding: 10px 12px;
            margin: 8px 0;
            color: #f8fafc;
            font-size: 13px;
            line-height: 1.4;
          }
          .lab-swap-candidate-blocked {
            opacity: 0.72;
            border-color: rgba(248, 113, 113, 0.45);
            color: #fecaca;
          }
          .lab-swap-confirm-row {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
            margin: 0 0 12px 0;
            font-size: 13px;
            color: #14532d;
          }
          .lab-swap-confirm-chip {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 12px;
            border-radius: 10px;
            background: #ecfdf5;
            border: 1px solid #86efac;
            font-weight: 700;
          }
          .lab-swap-cell-hit {
            min-height: 32px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            letter-spacing: 0.06em;
            border-radius: 8px;
            border: 1px solid #cbd5e1;
            background: #f8fafc;
            color: #334155;
          }
          .lab-swap-cell-hit.lab-swap-glow {
            background: #ecfdf5;
            border-color: #22c55e;
            color: #14532d;
          }
          .lab-schedule-tally-wrap {
            margin-top: 4px;
            margin-bottom: 12px;
            overflow: auto;
            max-height: 220px;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            background: #ffffff;
          }
          table.lab-schedule-tally-grid {
            width: max-content;
            min-width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            font-size: 13px;
          }
          table.lab-schedule-tally-grid th,
          table.lab-schedule-tally-grid td {
            border: 1px solid #cbd5e1;
            padding: 6px 8px;
            text-align: center;
          }
          table.lab-schedule-tally-grid th {
            position: sticky;
            top: 0;
            z-index: 2;
            background: #0f172a;
            color: #f8fafc;
            font-weight: 700;
            font-size: 11px;
          }
          table.lab-schedule-tally-grid th.lab-tally-label-col {
            position: sticky;
            left: 0;
            z-index: 3;
            text-align: left;
            min-width: 220px;
            background: #0f172a;
          }
          table.lab-schedule-tally-grid th.lab-weekend-col {
            background: #cbd5e1;
            color: #0f172a;
          }
          table.lab-schedule-tally-grid td.tally-label {
            position: sticky;
            left: 0;
            z-index: 1;
            text-align: left;
            font-weight: 800;
            background: #eff6ff;
            color: #1e3a8a;
            min-width: 220px;
            box-shadow: 2px 0 4px rgba(15, 23, 42, 0.06);
          }
          table.lab-schedule-grid tbody tr.tally-row td.tally-cell {
            font-weight: 800;
            background: #eff6ff;
            color: #1e3a8a;
            font-variant-numeric: tabular-nums;
            text-align: center;
          }
          table.lab-schedule-grid tbody tr.tally-row td.lab-tally-label {
            position: sticky;
            left: 0;
            z-index: 1;
            text-align: left;
            font-weight: 800;
            background: #eff6ff;
            color: #1e3a8a;
            min-width: 220px;
            box-shadow: 2px 0 4px rgba(15, 23, 42, 0.06);
          }
          table.lab-schedule-tally-grid td.tally-count {
            font-weight: 800;
            background: #ffffff;
            color: #334155;
            font-variant-numeric: tabular-nums;
            text-align: center;
          }
          table.lab-schedule-tally-grid td.tally-weekend-col {
            background: #f8fafc;
          }
          .lab-ops-ribbon {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: stretch;
            margin: 0 0 14px 0;
            padding: 12px 14px;
            border: 1px solid #334155;
            border-radius: 12px;
            background: linear-gradient(180deg, #0f172a 0%, #1e293b 100%);
          }
          .lab-ops-metric {
            flex: 1 1 140px;
            min-width: 120px;
            padding: 8px 10px;
            border-radius: 10px;
            background: rgba(15, 23, 42, 0.65);
            border: 1px solid #475569;
          }
          .lab-ops-metric-label {
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: #94a3b8;
          }
          .lab-ops-metric-value {
            font-size: 20px;
            font-weight: 800;
            color: #f8fafc;
            line-height: 1.2;
          }
          .lab-ops-metric-ok {
            color: #86efac;
          }
          .lab-ops-metric-warn {
            color: #fca5a5;
            text-shadow: 0 0 12px rgba(248, 113, 113, 0.35);
          }
          .lab-ops-actions {
            flex: 1 1 280px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            justify-content: flex-end;
          }
          .lab-drawer-panel {
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 14px 16px;
            background: #0b1220;
            min-height: 420px;
          }
          .lab-drawer-title {
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #93c5fd;
            margin-bottom: 10px;
          }
          .lab-hour-balance {
            font-size: 28px;
            font-weight: 800;
            color: #f8fafc;
            margin: 8px 0 4px;
          }
          .lab-hour-balance-sub {
            font-size: 13px;
            color: #94a3b8;
            line-height: 1.45;
          }
          .contract-union-risk {
            color: #dc2626;
          }
          .contract-overtime-warn {
            color: #ea580c;
          }
          .contract-overtime-risk {
            color: #dc2626;
          }
          .contract-ok {
            color: #16a34a;
          }
          .lab-draft-badge {
            font-size: 12px;
            font-weight: 700;
            color: #fbbf24;
            margin-left: 8px;
          }
          .lab-lifecycle-modal {
            background: linear-gradient(160deg, #1e1b4b 0%, #312e81 55%, #0f172a 100%);
            border: 1px solid #818cf8;
            border-radius: 14px;
            padding: 18px 20px;
            margin: 14px 0 18px;
            color: #eef2ff;
            box-shadow: 0 0 0 1px rgba(129, 140, 248, 0.18), 0 14px 32px rgba(15, 23, 42, 0.38);
          }
          .lab-lifecycle-modal-title {
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #c7d2fe;
            margin-bottom: 8px;
          }
          .lab-lifecycle-modal-headline {
            font-size: 20px;
            font-weight: 800;
            color: #f8fafc;
            margin-bottom: 8px;
          }
          .lab-lifecycle-modal-copy {
            font-size: 13px;
            color: #c7d2fe;
            line-height: 1.5;
            margin-bottom: 12px;
          }
          .lab-lifecycle-vacancy {
            display: block;
            background: rgba(15, 23, 42, 0.55);
            border: 1px solid rgba(199, 210, 254, 0.35);
            border-radius: 10px;
            padding: 10px 12px;
            margin: 8px 0;
            color: #f8fafc;
            font-size: 13px;
            line-height: 1.4;
          }
          .lab-lifecycle-danger {
            background: #450a0a;
            border: 1px solid #f87171;
            border-radius: 12px;
            padding: 14px 16px;
            margin: 10px 0 14px;
            color: #fecaca;
          }
          .lab-sentry-panel {
            background: linear-gradient(160deg, #1e293b 0%, #0f172a 100%);
            border: 1px solid #475569;
            border-radius: 14px;
            padding: 18px 20px;
            margin: 0 0 18px 0;
            color: #e2e8f0;
          }
          .lab-sentry-title {
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #94a3b8;
            margin-bottom: 8px;
          }
          .lab-sentry-headline {
            font-size: 20px;
            font-weight: 800;
            color: #f8fafc;
            margin-bottom: 6px;
          }
          .lab-sentry-copy {
            font-size: 13px;
            color: #cbd5e1;
            line-height: 1.5;
          }
          .lab-breakroom-toolbar {
            margin: 0 0 12px 0;
          }
          .lab-breakroom-print-host {
            display: none;
          }
          .lab-breakroom-print-host table {
            width: 100%;
            border-collapse: collapse;
          }
          .lab-breakroom-print-host th,
          .lab-breakroom-print-host td {
            border: 1px solid #000000;
            padding: 4px 3px;
            text-align: center;
            color: #000000;
          }
          .lab-breakroom-print-host th:first-child,
          .lab-breakroom-print-host td:first-child {
            text-align: left;
            min-width: 200px;
            width: 200px;
            white-space: normal;
            overflow: visible;
            text-overflow: clip;
          }
          .lab-breakroom-print-host tbody tr:nth-child(even) {
            background-color: #f2f2f2;
          }
          .lab-breakroom-print-host tbody tr.tally-row td {
            background-color: #dbeafe;
            font-weight: 800;
          }
          .lab-print-token {
            display: inline-block;
            min-width: 18px;
            padding: 1px 4px;
            border: 2px solid #000000;
            font-weight: 900;
            letter-spacing: 0.08em;
            line-height: 1.1;
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
          }
          .lab-print-token-d {
            background-color: #dbeafe !important;
            color: #1e3a8a !important;
          }
          .lab-print-token-e {
            background-color: #fef3c7 !important;
            color: #78350f !important;
          }
          .lab-print-token-n {
            background-color: #1e293b !important;
            color: #f8fafc !important;
          }
          .lab-breakroom-print-footer,
          .lab-breakroom-compliance-badge {
            margin-top: 10px;
            font-size: 11px;
            font-weight: 700;
            color: #000000;
          }
          .lab-breakroom-compliance-badge {
            border-top: 1px solid #000000;
            padding-top: 8px;
          }
          @media print {
            * {
              -webkit-print-color-adjust: exact !important;
              print-color-adjust: exact !important;
            }
            @page {
              size: legal landscape;
              margin: 0.35in;
            }
            html, body, [data-testid="stAppViewContainer"], .main {
              width: auto !important;
              overflow: visible !important;
            }
            [data-testid="stSidebar"],
            [data-testid="stHeader"],
            [data-testid="stToolbar"],
            [data-testid="stDecoration"],
            [data-testid="stStatusWidget"],
            .lab-no-print,
            .lab-breakroom-toolbar,
            button,
            .stButton,
            .stDownloadButton,
            .stExpander,
            [data-testid="stExpander"],
            iframe {
              display: none !important;
            }
            .lab-breakroom-print-host {
              display: block !important;
            }
            .lab-breakroom-print-host table {
              border-collapse: collapse !important;
              width: 100% !important;
              color: #000000 !important;
            }
            .lab-breakroom-print-host tbody tr:nth-child(even) {
              background-color: #f2f2f2 !important;
            }
            .lab-breakroom-print-host th,
            .lab-breakroom-print-host td {
              border: 1px solid #000000 !important;
              color: #000000 !important;
              background: #ffffff !important;
              font-weight: 800 !important;
            }
            .lab-breakroom-print-host th:first-child,
            .lab-breakroom-print-host td:first-child {
              min-width: 200px !important;
              width: 200px !important;
              white-space: normal !important;
              overflow: visible !important;
              text-overflow: clip !important;
            }
            .lab-breakroom-print-host tbody tr:nth-child(even) td {
              background: #f2f2f2 !important;
            }
            .lab-print-token {
              border: 2px solid #000000 !important;
            }
            .lab-print-token-d {
              background-color: #dbeafe !important;
              color: #1e3a8a !important;
            }
            .lab-print-token-e {
              background-color: #fef3c7 !important;
              color: #78350f !important;
            }
            .lab-print-token-n {
              background-color: #1e293b !important;
              color: #f8fafc !important;
            }
            .lab-shift-select.lab-shift-token-d {
              background-color: #dbeafe !important;
              color: #1e3a8a !important;
            }
            .lab-shift-select.lab-shift-token-e {
              background-color: #fef3c7 !important;
              color: #78350f !important;
            }
            .lab-shift-select.lab-shift-token-n {
              background-color: #1e293b !important;
              color: #f8fafc !important;
            }
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _html_badge(text: str, variant: str = "ok") -> str:
    safe = html_lib.escape(text)
    return f'<span class="lab-badge lab-badge-{variant}">{safe}</span>'


def _html_micro_banner(
    message: str,
    variant: str = "info",
    *,
    title: Optional[str] = None,
    escape_message: bool = True,
) -> str:
    if escape_message:
        body = html_lib.escape(message)
    else:
        body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", message)
    title_html = (
        f'<div style="font-weight:700;margin-bottom:4px;">{html_lib.escape(title)}</div>' if title else ""
    )
    return f'<div class="lab-micro-banner lab-micro-{variant}">{title_html}{body}</div>'


def _shift_cell_html(short: str) -> str:
    style = SHIFT_CELL_STYLES.get(short, SHIFT_CELL_STYLES["."])
    label = "—" if short == "." else html_lib.escape(short)
    return (
        f'<div class="lab-shift-cell" style="background:{style["bg"]};color:{style["fg"]};">'
        f"{label}</div>"
    )


def _is_weekend_schedule_day(day: date) -> bool:
    return day.weekday() >= 5


def _schedule_week_groups(
    view_dates: List[date],
    *,
    period_start: Optional[date] = None,
) -> List[Tuple[int, List[date]]]:
    if not view_dates:
        return []
    groups: List[List[date]] = []
    for day in view_dates:
        if day.weekday() == 0 and groups and groups[-1]:
            groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append(day)
    if period_start is not None:
        return [
            ((days[0] - period_start).days // 7 + 1, days)
            for days in groups
            if days
        ]
    return [(index + 1, chunk) for index, chunk in enumerate(groups)]


def _schedule_grid_thead_html(
    view_dates: List[date],
    *,
    period_start: Optional[date] = None,
    focus_date: Optional[date] = None,
) -> str:
    week_groups = _schedule_week_groups(view_dates, period_start=period_start)
    week_cells: List[str] = []
    day_cells: List[str] = []
    for week_num, days in week_groups:
        week_cells.append(
            f"<th class='lab-week-band' colspan='{len(days)}'>W{week_num}</th>"
        )
        for day in days:
            day_cells.append(
                "<th class='"
                + _schedule_day_css_classes(day, focus_date=focus_date)
                + "'>"
                + html_lib.escape(f"{day.strftime('%a')} {day.month}/{day.day}")
                + "</th>"
            )
    return (
        "<thead>"
        "<tr class='lab-week-band-row'>"
        "<th class='lab-emp-col lab-week-band-label' rowspan='2'>Employee</th>"
        + "".join(week_cells)
        + "</tr>"
        "<tr class='lab-day-header-row'>"
        + "".join(day_cells)
        + "</tr>"
        "</thead>"
    )


def _schedule_day_css_classes(day: date, *, focus_date: Optional[date] = None) -> str:
    classes = ["lab-day-col"]
    if _is_weekend_schedule_day(day):
        classes.append("lab-weekend-col")
    if day.weekday() == 0:
        classes.append("lab-week-start")
    if focus_date is not None and day == focus_date:
        classes.append("lab-health-focus-col")
    return " ".join(classes)


def _shift_style_for_value(value: object) -> Dict[str, str]:
    token = _normalize_shift_cell(value)
    if not token:
        return SHIFT_CELL_STYLES["."]
    return SHIFT_CELL_STYLES.get(token, SHIFT_CELL_STYLES["."])


def _shift_token_css_class(value: object) -> str:
    token = _normalize_shift_cell(value)
    if token in ("D", "M"):
        return "lab-shift-token-d"
    if token == "E":
        return "lab-shift-token-e"
    if token == "N":
        return "lab-shift-token-n"
    return ""


def _print_shift_token_class(token: str) -> str:
    normalized = _normalize_shift_cell(token)
    if normalized in ("D", "M"):
        return "lab-print-token-d"
    if normalized == "E":
        return "lab-print-token-e"
    if normalized in ("N", "I"):
        return "lab-print-token-n"
    return ""


def _shift_style_lookup_json() -> str:
    import json

    lookup = {".": SHIFT_CELL_STYLES["."]}
    for code in ("D", "M", "E", "N", OFF_CODE_VACATION, OFF_CODE_SICK):
        lookup[code] = SHIFT_CELL_STYLES.get(code, SHIFT_CELL_STYLES["."])
    lookup[EMPTY_SHIFT_DISPLAY] = SHIFT_CELL_STYLES["."]
    return json.dumps(lookup)


def _shift_select_options_html(selected_display: str) -> str:
    options: List[str] = []
    for option in SHIFT_EDITOR_OPTIONS:
        selected_attr = " selected" if option == selected_display else ""
        options.append(
            f'<option value="{html_lib.escape(option, quote=True)}"{selected_attr}>'
            f"{html_lib.escape(option)}</option>"
        )
    return "".join(options)


def _shift_editor_options_json() -> str:
    import json

    return json.dumps(list(SHIFT_EDITOR_OPTIONS))


def _schedule_edit_mode_key(period_id: str) -> str:
    return f"schedule_edit_mode_{period_id}"


def _schedule_focus_key(period_id: str) -> str:
    return f"schedule_focus_active_{period_id}"


def _schedule_focus_key_legacy(period_id: str) -> str:
    return f"epic_mode_active_{period_id}"


def _schedule_clear_pending_key(period_id: str) -> str:
    return f"schedule_clear_pending_{period_id}"


def _schedule_distribute_alt_pending_key(period_id: str) -> str:
    return f"schedule_distribute_alt_pending_{period_id}"


def _schedule_alternate_fill_pending_key(period_id: str) -> str:
    return f"schedule_alternate_fill_pending_{period_id}"


def _schedule_intentional_clear_save_key(period_id: str) -> str:
    return f"schedule_intentional_clear_save_{period_id}"


def _schedule_health_focus_date(period_id: str) -> Optional[date]:
    raw = st.session_state.get(_schedule_health_focus_date_key(period_id))
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _schedule_health_focus_date_key(period_id: str) -> str:
    return f"schedule_health_focus_date_{period_id}"


def _is_intentional_clear_save(
    period_id: str,
    *,
    draft_shift_count: int,
    db_shift_count: int,
    cache_shift_count: int = 0,
) -> bool:
    """True when staged edits represent clearing a populated schedule to empty."""

    if draft_shift_count != 0 or cache_shift_count > 0 or db_shift_count <= 0:
        return False
    pending = _load_pending_mutations(period_id)
    if not pending:
        return False
    for mutation in pending:
        if _normalize_shift_cell(mutation.new_token) in ALLOWED_SHIFT_CODES:
            return False
    return True


def _clear_intentional_clear_save_flag(period_id: str) -> None:
    st.session_state.pop(_schedule_intentional_clear_save_key(period_id), None)


def _schedule_focus_active(period_id: str) -> bool:
    focus_key = _schedule_focus_key(period_id)
    if focus_key in st.session_state:
        return bool(st.session_state[focus_key])
    legacy_key = _schedule_focus_key_legacy(period_id)
    if legacy_key in st.session_state:
        st.session_state[focus_key] = bool(st.session_state.pop(legacy_key, False))
        return bool(st.session_state[focus_key])
    st.session_state[focus_key] = False
    return False


def _any_schedule_focus_active() -> bool:
    """True when any schedule period is in focus mode."""

    prefix = "schedule_focus_active_"
    legacy_prefix = "epic_mode_active_"
    for key, value in st.session_state.items():
        key_text = str(key)
        if key_text.startswith(prefix) or key_text.startswith(legacy_prefix):
            if bool(value):
                return True
    return False


def _shift_band_token(value: object) -> Optional[str]:
    token = _normalize_shift_cell(value)
    if token in ("D", "M"):
        return "D"
    if token == "E":
        return "E"
    if token == "N":
        return "N"
    return None


def _shift_mix_from_matrix_row(
    row: Mapping[str, object],
    date_keys: Sequence[str],
) -> Dict[str, int]:
    counts = {"D": 0, "E": 0, "N": 0, "weekend": 0}
    for key in date_keys:
        band = _shift_band_token(row.get(key))
        if band is None:
            continue
        counts[band] += 1
        try:
            day = date.fromisoformat(key)
        except ValueError:
            continue
        if day.weekday() >= 5:
            counts["weekend"] += 1
    return counts


def _off_view_shift_mix(
    row: Mapping[str, object],
    view_dates: Sequence[date],
    date_keys: Sequence[str],
) -> Dict[str, int]:
    view_keys = {day.isoformat() for day in view_dates}
    counts = {"D": 0, "E": 0, "N": 0, "weekend": 0}
    for key in date_keys:
        if key in view_keys:
            continue
        band = _shift_band_token(row.get(key))
        if band is None:
            continue
        counts[band] += 1
        try:
            day = date.fromisoformat(key)
        except ValueError:
            continue
        if day.weekday() >= 5:
            counts["weekend"] += 1
    return counts


def _employee_grid_shift_stats(
    row: Mapping[str, object],
    date_keys: Sequence[str],
    contract_line_type: object,
) -> Optional[Dict[str, object]]:
    counts = _shift_mix_from_matrix_row(row, date_keys)
    contract = str(contract_line_type or "D/E").strip().upper()
    if contract == "D/N":
        total = counts["D"] + counts["N"]
        alternate = counts["N"]
        alternate_band = "N"
        alternate_label = "night"
        day_label = "D"
        day_count = counts["D"]
    elif contract == "D/E":
        total = counts["D"] + counts["E"]
        alternate = counts["E"]
        alternate_band = "E"
        alternate_label = "evening"
        day_label = "D"
        day_count = counts["D"]
    else:
        total = counts["D"] + counts["E"] + counts["N"]
        alternate = counts["E"] + counts["N"]
        alternate_band = "E+N"
        alternate_label = "alt"
        day_label = "D"
        day_count = counts["D"]
    if total <= 0:
        return None
    return {
        "alternate_shift_pct": round(100.0 * alternate / total, 1),
        "day_shift_pct": round(100.0 * day_count / total, 1),
        "alternate_band": alternate_band,
        "alternate_label": alternate_label,
        "alternate_shifts": alternate,
        "day_shifts": day_count,
        "day_label": day_label,
        "total_shifts": total,
        "weekend_shifts": counts["weekend"],
    }


def _build_roster_meta_view(
    frame: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict[str, object], List[str]]:
    meta_columns = ["Employee", "employee_id", "fte", "contract_line_type"]
    meta_view = frame[
        [column for column in meta_columns if column in frame.columns]
    ].copy()
    meta_view = meta_view.reset_index(drop=True)
    meta_view.insert(0, "#", range(1, len(meta_view) + 1))
    meta_column_config: Dict[str, object] = {
        "#": st.column_config.NumberColumn("#", disabled=True, width="small", format="%d"),
        "Employee": st.column_config.TextColumn("Employee", disabled=True, width="medium"),
        "employee_id": None,
        "fte": st.column_config.NumberColumn(
            "FTE",
            min_value=0.1,
            max_value=1.0,
            step=0.1,
            format="%.2f",
            width="small",
        ),
        "contract_line_type": st.column_config.SelectboxColumn(
            "Contract",
            options=list(CONTRACT_LINE_TYPES),
            width="small",
            required=True,
        ),
    }
    meta_column_config = {
        key: value for key, value in meta_column_config.items() if value is not None
    }
    return meta_view, meta_column_config, meta_columns


def _build_employee_analytics_dataframe(
    *,
    draft_frame: pd.DataFrame,
    employees: List[Dict],
    dates: List[date],
    period: TenantPeriod,
    rules: JurisdictionRules,
    emp_quals: Mapping[str, Set[str]],
    qual_codes: Mapping[str, str],
    contract_rows: Mapping[str, object],
) -> pd.DataFrame:
    from lab_scheduler.engine.constraints import portage_employee_target_hours
    from lab_scheduler.scheduling.contract_payroll import apply_catalog_targets_for_vacant_master_lines
    from lab_scheduler.scheduling.portage_equity_drift import PortageEquityDrift
    from lab_scheduler.scheduling.profiles import EmployeeProfile

    date_keys = [day.isoformat() for day in dates]
    matrix = _employee_schedule_matrix(draft_frame)
    matrix_by_id = {
        str(row.get("employee_id", "") or ""): row
        for _, row in matrix.iterrows()
        if str(row.get("employee_id", "") or "")
    }

    grid_profiles = [
        EmployeeProfile(
            id=str(employee["id"]),
            full_name=str(employee.get("full_name") or ""),
            fte=float(employee.get("fte") or 1.0),
            qualification_ids=emp_quals.get(employee["id"], set()),
            seniority_hours=float(employee.get("seniority_hours") or 0.0),
            base_hourly_rate=float(employee.get("base_hourly_rate") or 40.0),
            contract_line_type=employee.get("contract_line_type"),
        )
        for employee in employees
    ]
    grid_base_targets = portage_employee_target_hours(
        grid_profiles,
        weeks_in_period=period.week_count,
        rules=rules,
    )
    grid_catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        grid_profiles,
        grid_base_targets,
        rules=rules,
        weeks_in_period=period.week_count,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
    )
    equity_drift_by_employee = _build_portage_equity_drift_map_for_grid(
        employees,
        matrix,
        date_keys,
        period=period,
        catalog_targets=grid_catalog_targets,
        qual_codes=qual_codes,
    )

    rows: List[Dict[str, object]] = []
    for employee in employees:
        employee_id = str(employee["id"])
        if is_daily_tally_employee_id(employee_id):
            continue
        matrix_row = matrix_by_id.get(employee_id, {})
        contract = str(
            matrix_row.get("contract_line_type")
            or employee.get("contract_line_type")
            or "D/E"
        ).strip().upper()
        mix = _shift_mix_from_matrix_row(matrix_row, date_keys)
        stats = _employee_grid_shift_stats(matrix_row, date_keys, contract)
        tracking = contract_rows.get(employee_id)
        drift = equity_drift_by_employee.get(employee_id)

        alt_pct = f"{stats['alternate_shift_pct']:.0f}%" if stats else "—"
        alt_target = "—"
        wknd_target = "—"
        if isinstance(drift, PortageEquityDrift):
            alt_target = f"{drift.alt_actual}/{drift.alt_target}"
            wknd_target = f"{drift.weekend_actual}/{drift.weekend_target}"
            if drift.alt_status == "low":
                alt_target += " ↓"
            elif drift.alt_status == "high":
                alt_target += " ↑"
            if drift.weekend_status == "low":
                wknd_target += " ↓"
            elif drift.weekend_status == "high":
                wknd_target += " ↑"

        rows.append(
            {
                "#": len(rows) + 1,
                "Line": employee.get("full_name", employee_id),
                "Contract": contract,
                "FTE": round(float(employee.get("fte") or 1.0), 2),
                "Day": mix.get("D", 0),
                "Evening": mix.get("E", 0),
                "Night": mix.get("N", 0),
                "Weekend": int(stats["weekend_shifts"]) if stats else mix.get("weekend", 0),
                "Alt %": alt_pct,
                "Scheduled h": round(float(tracking.actual_hours), 1) if tracking else 0.0,
                "Target h": round(float(tracking.target_hours), 1) if tracking else 0.0,
                "Variance h": round(float(tracking.variance_hours), 1) if tracking else 0.0,
                "Alt vs target": alt_target,
                "Wknd vs target": wknd_target,
            }
        )

    return pd.DataFrame(rows)


def _format_portage_equity_drift_html(drift: object) -> str:
    from lab_scheduler.scheduling.portage_equity_drift import PortageEquityDrift

    if not isinstance(drift, PortageEquityDrift):
        return ""
    alt_warn = drift.alt_status not in ("ok", "na")
    wknd_warn = drift.weekend_status not in ("ok", "na")
    if alt_warn and wknd_warn:
        css = "lab-emp-equity-both-warn"
    elif alt_warn:
        css = "lab-emp-equity-alt-warn"
    elif wknd_warn:
        css = "lab-emp-equity-wknd-warn"
    else:
        css = "lab-emp-equity-ok"

    alt_note = (
        f"Alt {drift.alt_actual}/{drift.alt_target} "
        f"({drift.alt_density_pct:.0f}% vs {drift.alt_target_density_pct:.0f}% {drift.role_label})"
    )
    wknd_note = (
        f"Wknd {drift.weekend_actual}/{drift.weekend_target} "
        f"({drift.active_weekend_target} active pairs)"
    )
    if alt_warn:
        if drift.alt_status == "low":
            alt_note += " · below alt target"
        else:
            alt_note += " · above alt target"
    if wknd_warn:
        if drift.weekend_status == "low":
            wknd_note += " · below weekend target"
        else:
            wknd_note += " · above weekend target"

    title = html_lib.escape(f"{alt_note} · {wknd_note}")
    return (
        f"<span class='lab-emp-sub lab-emp-equity {css}' data-role='equity-drift' "
        f"title='{title}'>"
        f"{html_lib.escape(alt_note)} · {html_lib.escape(wknd_note)}"
        "</span>"
    )


def _build_portage_equity_drift_map_for_grid(
    employees: Sequence[Mapping[str, object]],
    full_matrix: pd.DataFrame,
    date_keys: Sequence[str],
    *,
    period: TenantPeriod,
    catalog_targets: Mapping[str, float],
    qual_codes: Mapping[str, str],
) -> Dict[str, object]:
    from lab_scheduler.scheduling.portage_equity_drift import build_portage_equity_drift_map
    from lab_scheduler.scheduling.profiles import EmployeeProfile

    profiles = [
        EmployeeProfile(
            id=str(row["id"]),
            full_name=str(row.get("full_name") or row.get("Employee") or ""),
            fte=float(row.get("fte") or 1.0),
            qualification_ids=set(),
            contract_line_type=row.get("contract_line_type"),
        )
        for row in employees
    ]
    profile_by_id = {profile.id: profile for profile in profiles}
    alt_by_id: Dict[str, int] = {}
    total_by_id: Dict[str, int] = {}
    wknd_by_id: Dict[str, int] = {}
    for _, matrix_row in full_matrix.iterrows():
        employee_id = str(matrix_row.get("employee_id", "") or "")
        if not employee_id or is_daily_tally_employee_id(employee_id):
            continue
        stats = _employee_grid_shift_stats(
            matrix_row,
            date_keys,
            matrix_row.get("contract_line_type", "D/E"),
        )
        if stats is None:
            continue
        alt_by_id[employee_id] = int(stats["alternate_shifts"])
        total_by_id[employee_id] = int(stats["total_shifts"])
        wknd_by_id[employee_id] = int(stats["weekend_shifts"])

    return build_portage_equity_drift_map(
        [profile_by_id[eid] for eid in alt_by_id if eid in profile_by_id],
        catalog_targets,
        alternate_shifts_by_employee=alt_by_id,
        total_shifts_by_employee=total_by_id,
        weekend_shifts_by_employee=wknd_by_id,
        qual_codes=qual_codes,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
    )


def _format_portage_equity_drift_compact_html(drift: object) -> str:
    from lab_scheduler.scheduling.portage_equity_drift import PortageEquityDrift

    if not isinstance(drift, PortageEquityDrift):
        return ""
    alt_warn = drift.alt_status not in ("ok", "na")
    wknd_warn = drift.weekend_status not in ("ok", "na")
    if alt_warn and wknd_warn:
        css = "lab-emp-equity-both-warn"
    elif alt_warn:
        css = "lab-emp-equity-alt-warn"
    elif wknd_warn:
        css = "lab-emp-equity-wknd-warn"
    else:
        css = "lab-emp-equity-ok"
    text = (
        f"Eq Alt {drift.alt_actual}/{drift.alt_target} "
        f"({drift.alt_density_pct:.0f}%/{drift.alt_target_density_pct:.0f}%) · "
        f"Wk {drift.weekend_actual}/{drift.weekend_target}"
    )
    return (
        f"<span class='lab-emp-sub lab-emp-equity {css}' data-role='equity-drift'>"
        f"{html_lib.escape(text)}</span>"
    )


def _format_grid_employee_cell(
    employee_label: str,
    *,
    actual_hours: Optional[float] = None,
    target_hours: Optional[float] = None,
    shift_stats: Optional[Mapping[str, object]] = None,
    equity_drift: Optional[object] = None,
    compact: bool = True,
) -> str:
    import re

    from lab_scheduler.scheduling.portage_template import (
        FULLTIME_FTE_THRESHOLD,
        portage_master_line_spec,
    )
    from lab_scheduler.scheduling.profiles import EmployeeProfile

    text = str(employee_label).strip()
    hours_match = re.search(r"\(([^)]+h)\)", text)
    hours_note = hours_match.group(1) if hours_match else ""
    core = re.sub(r"\s*\([^)]+h\)\s*", "", text).strip()
    line_match = re.search(r"Line\s+(\d+)", core, re.I)
    if line_match:
        prefix = core[: line_match.start()].strip(" -")
        if prefix.lower().startswith("vacant "):
            prefix = prefix[7:].strip()
        primary_text = f"Line {line_match.group(1)} · {prefix}" if prefix else f"Line {line_match.group(1)}"
    else:
        primary_text = core
    primary = html_lib.escape(primary_text)
    rotation_note: Optional[str] = None
    if "vacant" in core.lower():
        hint_profile = EmployeeProfile(
            id="grid-hint",
            full_name=core,
            fte=1.0,
            qualification_ids=set(),
            contract_line_type="D/E",
        )
        master_spec = portage_master_line_spec(hint_profile)
        if (
            master_spec is not None
            and master_spec.target_fte >= FULLTIME_FTE_THRESHOLD
        ):
            rotation_note = f"Wk offset {master_spec.week_offset}/8"
    meta_parts: List[str] = []
    if actual_hours is not None and target_hours is not None and target_hours > 0:
        hours_class = _contract_hour_row_class(
            actual_hours,
            target_hours,
        ).replace("lab-emp-row-", "lab-emp-hours-")
        meta_parts.append(
            f"<span class='lab-emp-sub lab-emp-hours {hours_class}' data-role='hours-summary'>"
            f"{actual_hours:.0f}/{target_hours:.0f}h</span>"
        )
    elif hours_note:
        meta_parts.append(
            f"<span class='lab-emp-sub'>{html_lib.escape(hours_note)}</span>"
        )
    if shift_stats is not None:
        alt_band = html_lib.escape(str(shift_stats["alternate_band"]))
        day_band = html_lib.escape(str(shift_stats["day_label"])[0:1])
        meta_parts.append(
            "<span class='lab-emp-sub lab-emp-stats' data-role='shift-stats'>"
            f"Alt {float(shift_stats['alternate_shift_pct']):.0f}% "
            f"{int(shift_stats['alternate_shifts'])}{alt_band}/{int(shift_stats['total_shifts'])} · "
            f"{float(shift_stats['day_shift_pct']):.0f}% {day_band} · "
            f"Wk {int(shift_stats['weekend_shifts'])}"
            "</span>"
        )
    else:
        meta_parts.append(
            "<span class='lab-emp-sub lab-emp-stats' data-role='shift-stats'>"
            "Alt 0% 0/0 · 0% D · Wk 0"
            "</span>"
        )
    equity_html = _format_portage_equity_drift_compact_html(equity_drift)
    if equity_html:
        meta_parts.append(equity_html)
    if rotation_note:
        meta_parts.append(
            "<span class='lab-emp-sub lab-emp-rotation' data-role='rotation-offset'>"
            f"{html_lib.escape(rotation_note)}</span>"
        )
    meta_row = "".join(meta_parts)
    return (
        f"<div class='lab-emp-cell-compact'>"
        f"<div class='lab-emp-primary'>{primary}</div>"
        f"<div class='lab-emp-meta-row'>{meta_row}</div>"
        f"</div>"
    )


def _is_worked_shift_cell(value: object) -> bool:
    token = _normalize_shift_cell(value)
    if not token:
        return False
    return token in WORKED_SHIFT_TOKENS or token in {"D", "E", "N", "M"}


def _off_view_worked_shift_count(
    row: Mapping[str, object],
    view_dates: Sequence[date],
    date_keys: Sequence[str],
) -> int:
    view_keys = {day.isoformat() for day in view_dates}
    return sum(
        1
        for key in date_keys
        if key not in view_keys and _is_worked_shift_cell(row.get(key))
    )


def _contract_hour_row_class(
    actual_hours: float,
    target_hours: float,
    *,
    hours_per_shift: float = HOURS_PER_SHIFT,
    tolerance: float = FULLTIME_CONTRACT_HOUR_TOLERANCE,
) -> str:
    """Row band: orange when over by one shift; red when over by more than one."""

    del tolerance
    if target_hours <= 0.0:
        return ""
    if actual_hours < target_hours - 0.25:
        return "lab-emp-row-short"
    one_shift = float(hours_per_shift)
    if actual_hours > target_hours + one_shift + 0.25:
        return "lab-emp-row-over"
    if actual_hours > target_hours + 0.25:
        return "lab-emp-row-over-warn"
    return "lab-emp-row-ok"


def _contract_row_config_json(
    *,
    contract_rows: Mapping[str, ContractTrackingRow],
    full_matrix: pd.DataFrame,
    view_dates: Sequence[date],
    date_keys: Sequence[str],
    hours_per_shift: float,
    tolerance: float,
) -> str:
    import json

    rows_by_id = {
        str(row.get("employee_id", "")): row
        for _, row in full_matrix.iterrows()
        if not is_daily_tally_employee_id(row.get("employee_id"))
    }
    payload: Dict[str, Dict[str, float]] = {}
    for employee_id, tracking in contract_rows.items():
        row = rows_by_id.get(employee_id)
        payload[employee_id] = {
            "target": float(tracking.target_hours),
            "actual": float(tracking.actual_hours),
            "offViewShifts": float(
                _off_view_worked_shift_count(row, view_dates, date_keys) if row is not None else 0.0
            ),
        }
    return json.dumps(
        {
            "rows": payload,
            "hoursPerShift": hours_per_shift,
            "tolerance": tolerance,
        }
    )


def _shift_pill_token_class(display_value: str) -> str:
    token = _normalize_shift_cell(display_value)
    if token in ("D", "M"):
        return "lab-shift-pill-d"
    if token == "E":
        return "lab-shift-pill-e"
    if token == "N":
        return "lab-shift-pill-n"
    return "lab-shift-pill-off"


def _shift_pill_label(display_value: str) -> str:
    token = _normalize_shift_cell(display_value)
    if not token:
        return "—"
    return html_lib.escape(token)


def _shift_select_options_for_cell(
    selected_display: str,
    *,
    contract_line: object,
) -> str:
    options: List[str] = []
    for option in _shift_editor_options_for_contract_line(contract_line):
        selected_attr = " selected" if option == selected_display else ""
        options.append(
            f'<option value="{html_lib.escape(option, quote=True)}"{selected_attr}>'
            f"{html_lib.escape(option)}</option>"
        )
    return "".join(options)


def _shift_pill_html(
    *,
    employee_id: str,
    day_key: str,
    display_value: str,
    edit_mode: bool,
    contract_line: object = "D/E",
    is_locked: bool = False,
) -> str:
    worked = _is_worked_shift_cell(display_value)
    lock_class = ""
    if is_locked:
        lock_class = " lab-shift-locked" if worked else " lab-shift-locked-empty"
    lock_title = ""
    if is_locked and worked:
        lock_title = " title='Week locked — right-click any day this week to unlock'"
    elif is_locked:
        lock_title = " title='Week locked (empty) — right-click to unlock'"
    disabled_attr = ""
    if edit_mode and is_locked and worked:
        disabled_attr = " disabled"
    if edit_mode:
        style = _shift_style_for_value(display_value)
        pill_class = _shift_pill_token_class(display_value)
        return (
            f"<span class='lab-shift-cell-wrap{lock_class}'{lock_title}>"
            f"<select class='lab-shift-inline-select {pill_class}' "
            f"data-employee-id='{html_lib.escape(employee_id, quote=True)}' "
            f"data-date='{html_lib.escape(day_key, quote=True)}' "
            f"data-display-token='{html_lib.escape(display_value, quote=True)}' "
            f"data-locked='{'true' if is_locked else 'false'}'"
            f"{disabled_attr} "
            f"style='background:{style['bg']};color:{style['fg']};'>"
            f"{_shift_select_options_for_cell(display_value, contract_line=contract_line)}"
            f"</select></span>"
        )
    pill_class = _shift_pill_token_class(display_value)
    return (
        f"<span class='lab-shift-cell-wrap{lock_class}'{lock_title}>"
        f"<button type='button' class='lab-shift-pill {pill_class} lab-shift-pill-readonly' "
        f"data-employee-id='{html_lib.escape(employee_id, quote=True)}' "
        f"data-date='{html_lib.escape(day_key, quote=True)}' "
        f"data-display-token='{html_lib.escape(display_value, quote=True)}' "
        f"data-locked='{'true' if is_locked else 'false'}'>"
        f"{_shift_pill_label(display_value)}</button></span>"
    )


def _master_schedule_grid_stylesheet(*, fullscreen: bool = False) -> str:
    """Self-contained styles for the components.html shift grid (iframe has no parent CSS)."""

    del fullscreen  # wrap fills iframe; fullscreen styling is on parent page + modifier class
    css = """
<style>
  html, body {
    margin: 0;
    padding: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: #ffffff;
    color: #0f172a;
  }
  .lab-schedule-wrap {
    box-sizing: border-box;
    width: 100%;
    height: 100%;
    overflow: auto;
    overflow-x: auto;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    border: 1px solid #cbd5e1;
    border-radius: 12px;
    background: #ffffff;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.08);
    --lab-grid-bg: #ffffff;
    --lab-grid-header-bg: #0f172a;
    --lab-grid-header-fg: #f8fafc;
    --lab-grid-emp-bg: #f1f5f9;
    --lab-grid-emp-fg: #0f172a;
    --lab-grid-border: #cbd5e1;
    --lab-grid-cell-bg: #ffffff;
    --lab-grid-weekend-bg: #ecfdf5;
    --lab-pill-off-bg: #f1f5f9;
    --lab-pill-off-fg: #475569;
    --lab-tally-label-bg: #dbeafe;
    --lab-tally-label-fg: #1e3a8a;
    --lab-tally-cell-bg: #f8fafc;
    --lab-tally-cell-fg: #0f172a;
  }
  .lab-schedule-grid {
    border-collapse: separate;
    border-spacing: 0;
    width: max-content;
    min-width: 100%;
    table-layout: fixed;
    font-size: 12px;
    background: var(--lab-grid-bg);
  }
  .lab-schedule-grid thead th {
    position: sticky;
    top: 0;
    z-index: 2;
    background: var(--lab-grid-header-bg);
    color: var(--lab-grid-header-fg);
    font-weight: 700;
    text-align: center;
    padding: 8px 4px;
    border-bottom: 2px solid var(--lab-grid-border);
    border-right: 1px solid var(--lab-grid-border);
    white-space: nowrap;
    line-height: 1.25;
  }
  .lab-schedule-grid thead th.lab-emp-col {
    position: sticky;
    left: 0;
    z-index: 4;
    text-align: left;
    padding-left: 10px;
    width: 220px;
    min-width: 220px;
    background: var(--lab-grid-header-bg);
  }
  .lab-schedule-grid thead tr.lab-week-band-row th.lab-week-band {
    background: #1e40af;
    color: #dbeafe;
    font-size: 10px;
    font-weight: 700;
    text-align: center;
    padding: 4px 2px;
    border-bottom: 1px solid #334155;
    border-right: 1px solid #334155;
    top: 0;
    z-index: 2;
  }
  .lab-schedule-grid thead tr.lab-week-band-row th.lab-week-band-label {
    vertical-align: bottom;
    z-index: 5;
  }
  .lab-schedule-grid thead tr.lab-day-header-row th {
    top: 26px;
  }
  .lab-schedule-grid thead th.lab-week-start,
  .lab-schedule-grid tbody td.lab-week-start,
  .lab-schedule-grid tfoot td.lab-week-start {
    border-left: 2px solid #2563eb;
  }
  .lab-shift-cell-wrap {
    position: relative;
    display: inline-block;
  }
  .lab-emp-cell-compact {
    display: flex;
    flex-direction: column;
    gap: 2px;
    line-height: 1.2;
  }
  .lab-emp-cell-compact .lab-emp-primary {
    font-weight: 600;
    line-height: 1.2;
  }
  .lab-emp-cell-compact .lab-emp-meta-row {
    display: block;
    font-size: 10px;
    line-height: 1.35;
    color: #475569;
    font-weight: 400;
    white-space: normal;
  }
  .lab-emp-cell-compact .lab-emp-sub {
    display: inline;
    margin: 0;
    font-size: inherit;
    line-height: inherit;
  }
  .lab-emp-cell-compact .lab-emp-sub + .lab-emp-sub::before {
    content: " · ";
    color: #94a3b8;
  }
  .lab-emp-cell-compact .lab-emp-equity {
    display: inline;
    margin: 0;
    padding: 0;
    border-left: none;
    font-size: inherit;
  }
  .lab-schedule-grid thead th.lab-day-col {
    width: 64px;
    min-width: 64px;
    max-width: 64px;
  }
  .lab-schedule-grid thead th.lab-weekend-col {
    background: #047857;
    color: #ecfdf5;
  }
  .lab-schedule-grid tbody td {
    border-bottom: 1px solid var(--lab-grid-border);
    border-right: 1px solid var(--lab-grid-border);
    padding: 0;
    vertical-align: middle;
    background: var(--lab-grid-cell-bg);
  }
  .lab-schedule-grid tbody td.lab-emp-col {
    position: sticky;
    left: 0;
    z-index: 3;
    padding: 6px 10px;
    font-weight: 600;
    color: var(--lab-grid-emp-fg);
    background: var(--lab-grid-emp-bg);
    width: 220px;
    min-width: 220px;
    white-space: normal;
    vertical-align: top;
    box-shadow: 2px 0 4px rgba(15, 23, 42, 0.08);
  }
  tr.lab-emp-row-short td.lab-emp-col {
    background: #fef9c3 !important;
    color: #854d0e !important;
  }
  tr.lab-emp-row-short td.lab-day-col,
  tr.lab-emp-row-short td.lab-weekend-col {
    background: #fffbeb !important;
  }
  tr.lab-emp-row-over td.lab-emp-col {
    background: #fee2e2 !important;
    color: #991b1b !important;
  }
  tr.lab-emp-row-over-warn td.lab-emp-col {
    background: #ffedd5 !important;
    color: #9a3412 !important;
  }
  tr.lab-emp-row-over td.lab-day-col,
  tr.lab-emp-row-over td.lab-weekend-col {
    background: #fef2f2 !important;
  }
  tr.lab-emp-row-over-warn td.lab-day-col,
  tr.lab-emp-row-over-warn td.lab-weekend-col {
    background: #fff7ed !important;
  }
  tr.lab-emp-row-ok td.lab-emp-col {
    background: #ecfdf5 !important;
    color: #166534 !important;
  }
  .lab-emp-hours.lab-emp-hours-short { color: #854d0e !important; font-weight: 800; }
  .lab-emp-hours.lab-emp-hours-over-warn { color: #9a3412 !important; font-weight: 800; }
  .lab-emp-hours.lab-emp-hours-over { color: #991b1b !important; font-weight: 800; }
  .lab-emp-hours.lab-emp-hours-ok { color: #166534 !important; font-weight: 800; }
  .lab-schedule-grid tbody td.lab-day-col { text-align: center; padding: 4px 2px; }
  .lab-schedule-grid tbody td.lab-weekend-col { background: var(--lab-grid-weekend-bg); }
  .lab-schedule-grid .lab-week-start { border-left: 2px solid #2563eb !important; }
  .lab-emp-primary { font-size: 12px; font-weight: 700; color: #0f172a; line-height: 1.3; }
  .lab-emp-sub { display: block; margin-top: 4px; font-size: 11px; font-weight: 500; color: #475569; line-height: 1.35; }
  .lab-emp-stats { font-variant-numeric: tabular-nums; }
  .lab-shift-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 40px;
    width: 44px;
    height: 32px;
    border: 1px solid rgba(15, 23, 42, 0.12);
    border-radius: 8px;
    font-weight: 800;
    font-size: 12px;
    letter-spacing: 0.06em;
    line-height: 1;
    padding: 0;
    margin: 0 auto;
    background: var(--lab-pill-off-bg);
    color: var(--lab-pill-off-fg);
    cursor: default;
  }
  .lab-shift-pill-d { background-color: #dbeafe !important; color: #1e3a8a !important; }
  .lab-shift-pill-e { background-color: #fef3c7 !important; color: #78350f !important; }
  .lab-shift-pill-n { background-color: #1e293b !important; color: #f8fafc !important; }
  .lab-shift-pill-off { background-color: #f1f5f9 !important; color: #64748b !important; }
  .lab-shift-pill-editable { cursor: pointer; }
  .lab-shift-pill-editable:hover { outline: 2px solid #2563eb; outline-offset: 1px; }
  .lab-shift-inline-select {
    display: block;
    min-width: 44px;
    width: 44px;
    height: 32px;
    border: 1px solid rgba(15, 23, 42, 0.12);
    border-radius: 8px;
    font-weight: 800;
    font-size: 12px;
    letter-spacing: 0.06em;
    line-height: 1;
    padding: 0 2px;
    margin: 0 auto;
    text-align: center;
    text-align-last: center;
    cursor: pointer;
    appearance: auto;
    -webkit-appearance: menulist;
  }
  .lab-shift-inline-select:hover,
  .lab-shift-inline-select:focus {
    outline: 2px solid #2563eb;
    outline-offset: 1px;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-shift-inline-select {
    appearance: none;
    -webkit-appearance: none;
    -moz-appearance: none;
    background-image: none !important;
    padding: 0;
    text-align: center;
    text-align-last: center;
  }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit .lab-shift-inline-select::-ms-expand {
    display: none;
  }
  .lab-shift-cell-wrap.lab-shift-locked .lab-shift-inline-select,
  .lab-shift-cell-wrap.lab-shift-locked .lab-shift-pill-readonly {
    box-shadow: 0 0 0 2px #16a34a;
  }
  .lab-shift-cell-wrap.lab-shift-locked-empty .lab-shift-inline-select,
  .lab-shift-cell-wrap.lab-shift-locked-empty .lab-shift-pill-readonly {
    box-shadow: 0 0 0 2px #16a34a;
    border-style: dashed;
  }
  .lab-shift-inline-select:disabled {
    opacity: 0.88;
    cursor: not-allowed;
  }
  .lab-shift-cell-wrap.lab-drag-selected {
    outline: 2px solid #2563eb;
    outline-offset: -1px;
    box-shadow: inset 0 0 0 1px #93c5fd;
    background: rgba(37, 99, 235, 0.08);
  }
  .lab-marquee-select {
    position: fixed;
    z-index: 19999;
    pointer-events: none;
    border: 1px solid #2563eb;
    background: rgba(37, 99, 235, 0.14);
    box-shadow: 0 0 0 1px rgba(147, 197, 253, 0.55);
    border-radius: 1px;
  }
  body.lab-marquee-dragging {
    user-select: none;
    cursor: crosshair;
  }
  body.lab-marquee-dragging .lab-shift-inline-select {
    pointer-events: none;
  }
  .lab-drag-fill-palette {
    position: fixed;
    z-index: 20000;
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid #cbd5e1;
    background: #ffffff;
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.18);
    font: 600 12px/1.2 system-ui, sans-serif;
    color: #0f172a;
  }
  .lab-drag-fill-label {
    margin-right: 4px;
    color: #475569;
    font-weight: 600;
  }
  .lab-drag-fill-skipped {
    color: #64748b;
    font-weight: 500;
    font-size: 11px;
  }
  .lab-drag-fill-btn {
    min-width: 30px;
    min-height: 28px;
    padding: 0 8px;
    border-radius: 6px;
    border: 1px solid #94a3b8;
    background: #f8fafc;
    color: #0f172a;
    font: 700 13px/1 system-ui, sans-serif;
    cursor: pointer;
  }
  .lab-drag-fill-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  .lab-drag-fill-btn:hover:not(:disabled) {
    background: #e2e8f0;
  }
  .lab-shift-popover {
    position: absolute;
    z-index: 1000;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 10px;
    padding: 8px;
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.18);
  }
  .lab-shift-popover select {
    min-width: 72px;
    min-height: 36px;
    border-radius: 8px;
    border: 1px solid #cbd5e1;
    font-weight: 700;
    font-size: 12px;
    padding: 4px 8px;
    background: #ffffff;
    color: #0f172a;
  }
  .lab-schedule-wrap.lab-edit-mode { outline: 2px dashed #2563eb; outline-offset: 2px; }
  .lab-schedule-wrap.lab-schedule-wrap--focus-fit.lab-edit-mode {
    outline: none;
  }
  .lab-schedule-wrap--fullscreen .lab-tally-legend {
    font-size: 10px;
    padding: 6px 10px;
    margin-bottom: 6px;
  }
  table.lab-schedule-grid tbody tr.tally-row td.tally-cell {
    font-weight: 800;
    background: var(--lab-tally-cell-bg);
    color: var(--lab-tally-cell-fg);
    font-variant-numeric: tabular-nums;
    text-align: center;
  }
  table.lab-schedule-grid tbody tr.tally-row td.lab-tally-label {
    position: sticky;
    left: 0;
    z-index: 2;
    text-align: left;
    font-weight: 800;
    background: var(--lab-tally-label-bg);
    color: var(--lab-tally-label-fg);
    min-width: 220px;
    box-shadow: 2px 0 4px rgba(15, 23, 42, 0.08);
  }
  table.lab-schedule-grid tbody tr.tally-row td.lab-weekend-col {
    background: #d1fae5;
    color: #064e3b;
  }
  table.lab-schedule-grid tfoot tr.tally-row td {
    position: sticky;
    z-index: 4;
    font-weight: 800;
    background: var(--lab-tally-cell-bg);
    color: var(--lab-tally-cell-fg);
    font-variant-numeric: tabular-nums;
    text-align: center;
    box-shadow: 0 -2px 6px rgba(15, 23, 42, 0.08);
  }
  table.lab-schedule-grid tfoot tr.tally-row:nth-child(1) td { bottom: 72px; }
  table.lab-schedule-grid tfoot tr.tally-row:nth-child(2) td { bottom: 36px; }
  table.lab-schedule-grid tfoot tr.tally-row:nth-child(3) td { bottom: 0; }
  table.lab-schedule-grid tfoot tr.tally-row td.lab-tally-label {
    position: sticky;
    left: 0;
    z-index: 5;
    text-align: left;
    background: var(--lab-tally-label-bg);
    color: var(--lab-tally-label-fg);
    min-width: 220px;
    width: 220px;
    box-shadow: 2px 0 4px rgba(15, 23, 42, 0.08);
  }
  table.lab-schedule-grid tfoot tr.tally-row td.tally-ok {
    background: #dcfce7 !important;
    color: #166534 !important;
  }
  table.lab-schedule-grid tfoot tr.tally-row td.tally-short {
    background: #fef9c3 !important;
    color: #854d0e !important;
    cursor: pointer;
  }
  table.lab-schedule-grid tfoot tr.tally-row td.tally-over {
    background: #fee2e2 !important;
    color: #991b1b !important;
    cursor: pointer;
  }
  table.lab-schedule-grid tfoot tr.tally-row td.lab-weekend-col.tally-ok {
    background: #bbf7d0 !important;
    color: #14532d !important;
  }
  .lab-tally-legend {
    margin: 0 0 8px;
    padding: 8px 12px;
    border-radius: 8px;
    background: #eff6ff;
    color: #1e3a8a;
    font-size: 11px;
    line-height: 1.45;
  }
  .lab-emp-equity {
    display: block;
    margin-top: 2px;
    padding: 2px 0 2px 8px;
    border-left: 3px solid transparent;
    font-size: 10px;
    line-height: 1.35;
    color: #64748b;
  }
  .lab-emp-equity-ok {
    border-left-color: #86efac;
    color: #166534;
  }
  .lab-emp-equity-alt-warn {
    border-left-color: #a855f7;
    color: #6b21a8;
    font-weight: 700;
  }
  .lab-emp-equity-wknd-warn {
    border-left-color: #14b8a6;
    color: #115e59;
    font-weight: 700;
  }
  .lab-emp-equity-both-warn {
    border-left-color: #f59e0b;
    color: #92400e;
    font-weight: 700;
  }
</style>
"""
    return css


def _load_live_schedule_matrix(
    period_id: str,
    fallback: pd.DataFrame,
    view_dates: List[date],
) -> pd.DataFrame:
    """Return the employee matrix slice for the active week block."""

    merged = _merge_matrix_cache_into_draft(period_id, _employee_schedule_matrix(fallback))
    return _slice_schedule_frame_for_view(merged, view_dates)


def _invalidate_schedule_matrix_view_cache(period_id: str) -> None:
    st.session_state.pop(_schedule_matrix_key(period_id), None)


def _schedule_grid_staging_revision_key(period_id: str) -> str:
    return f"schedule_grid_staging_revision_{period_id}"


def _schedule_grid_staging_revision(period_id: str) -> int:
    return int(st.session_state.get(_schedule_grid_staging_revision_key(period_id), 0) or 0)


def _bump_schedule_grid_staging_revision(period_id: str) -> int:
    revision = _schedule_grid_staging_revision(period_id) + 1
    st.session_state[_schedule_grid_staging_revision_key(period_id)] = revision
    return revision


def _grid_session_storage_key(period_id: str) -> str:
    return f"lab_grid_pending_{period_id}"


def _merge_grid_cell_changes(
    *change_lists: Sequence[Mapping[str, str]],
) -> List[Dict[str, str]]:
    merged: Dict[Tuple[str, str], Dict[str, str]] = {}
    for changes in change_lists:
        for change in changes:
            employee_id = str(change.get("employee_id", "") or "")
            date_key = str(change.get("date", "") or "")
            if employee_id and date_key:
                merged[(employee_id, date_key)] = {
                    "employee_id": employee_id,
                    "date": date_key,
                    "token": str(change.get("token", "") or ""),
                }
    return list(merged.values())


def _grid_bridge_run_cache_key(period_id: str) -> str:
    return f"schedule_grid_bridge_run_cache_{period_id}"


def _reset_grid_bridge_run_cache(period_id: str) -> None:
    st.session_state.pop(_grid_bridge_run_cache_key(period_id), None)


def _drain_grid_session_storage_bridge(
    period_id: str,
    *,
    clear: bool = False,
    read_pending: bool = False,
) -> Optional[Mapping[str, object]]:
    """Read grid edits persisted in browser storage via Components v2 bridge."""

    cache_key = _grid_bridge_run_cache_key(period_id)
    if clear:
        _reset_grid_bridge_run_cache(period_id)
        payload = _mount_lab_grid_storage_bridge(
            period_id,
            clear=True,
            key_suffix="_clear",
        )
    elif read_pending:
        _reset_grid_bridge_run_cache(period_id)
        payload = _mount_lab_grid_storage_bridge(
            period_id,
            clear=False,
            read_pending=True,
            key_suffix="_save_read",
        )
        if isinstance(payload, Mapping):
            st.session_state[cache_key] = payload
    else:
        # Edits stay in browser sessionStorage until Save. Mounting the listen
        # bridge here triggers a full Streamlit rerun on every cell change.
        payload = None
    change_count = 0
    if isinstance(payload, Mapping):
        changes = payload.get("changes")
        if isinstance(changes, list):
            change_count = len(changes)
    if isinstance(payload, Mapping):
        return payload
    return None


def _clear_grid_session_storage_bridge(period_id: str) -> None:
    """Drop browser-stored grid edits after they have been applied to the draft."""

    _reset_grid_bridge_run_cache(period_id)
    _mount_lab_grid_storage_bridge(period_id, clear=True, key_suffix="_clear")


def _grid_changes_accumulator_key(period_id: str) -> str:
    return f"schedule_grid_pending_accum_{period_id}"


def _accumulate_grid_cell_changes(
    period_id: str,
    changes: Sequence[Mapping[str, str]],
) -> None:
    if not changes:
        return
    key = _grid_changes_accumulator_key(period_id)
    st.session_state[key] = _merge_grid_cell_changes(
        st.session_state.get(key, []),
        changes,
    )


def _load_accumulated_grid_changes(period_id: str) -> List[Dict[str, str]]:
    stored = st.session_state.get(_grid_changes_accumulator_key(period_id), [])
    return list(stored) if stored else []


def _clear_accumulated_grid_changes(period_id: str) -> None:
    st.session_state.pop(_grid_changes_accumulator_key(period_id), None)


def _collect_pending_grid_changes_for_save(
    period_id: str,
    *,
    draft: pd.DataFrame,
    employees: Sequence[Mapping[str, object]],
) -> List[Dict[str, str]]:
    """Merge live user edits, cached iframe echo, and session accumulator for save."""

    bridge_payload = _drain_grid_session_storage_bridge(
        period_id,
        clear=False,
        read_pending=True,
    )
    bridge_changes = _bridge_changes_for_draft_apply(bridge_payload, save_pending=True)
    echo_payload = _capture_grid_component_echo(period_id, None)
    raw_changes = _merge_grid_cell_changes(
        _load_accumulated_grid_changes(period_id),
        bridge_changes,
        _grid_component_cell_changes(echo_payload),
    )
    return _filter_grid_changes_against_draft(draft, employees, raw_changes)


def _schedule_grid_echo_key(period_id: str) -> str:
    return f"schedule_grid_last_echo_{period_id}"


def _capture_grid_component_echo(
    period_id: str,
    cell_change: Optional[Mapping[str, object]],
) -> Optional[Mapping[str, object]]:
    """Keep the latest iframe payload across component remounts."""

    echo_key = _schedule_grid_echo_key(period_id)
    if cell_change is not None:
        st.session_state[echo_key] = cell_change
        return cell_change
    cached = st.session_state.get(echo_key)
    if isinstance(cached, Mapping):
        return cached
    return None


def _clear_grid_component_echo(period_id: str) -> None:
    st.session_state.pop(_schedule_grid_echo_key(period_id), None)


def _apply_pending_mutations_to_draft(
    draft: pd.DataFrame,
    *,
    period_id: str,
    dates: Sequence[date],
) -> pd.DataFrame:
    """Ensure staged cell mutations are reflected in the draft before DB publish."""

    pending = _load_pending_mutations(period_id)
    if not pending or draft.empty or "employee_id" not in draft.columns:
        return draft
    updated = draft.copy()
    row_index = schedule_frame_row_index_by_employee_id(updated)
    for mutation in pending:
        row_idx = row_index.get(mutation.employee_id)
        if row_idx is None:
            continue
        day_key = mutation.assignment_date.isoformat()
        if day_key not in updated.columns:
            continue
        updated.at[row_idx, day_key] = _display_shift_cell(mutation.new_token)
    return updated


def _filter_grid_changes_against_draft(
    draft: pd.DataFrame,
    employees: Sequence[Mapping[str, object]],
    changes: Sequence[Mapping[str, str]],
) -> List[Dict[str, str]]:
    """Drop stale iframe queue entries that already match the draft (prevents regressions on rerun)."""

    if draft.empty or "employee_id" not in draft.columns:
        return list(changes)
    employee_index = schedule_frame_row_index_by_employee_id(draft)
    filtered: List[Dict[str, str]] = []
    for change in changes:
        employee_id = str(change.get("employee_id", "") or "")
        date_key = str(change.get("date", "") or "")
        token = _normalize_shift_cell(change.get("token", ""))
        row_idx = employee_index.get(employee_id)
        if row_idx is None or date_key not in draft.columns:
            continue
        current = _normalize_shift_cell(draft.at[row_idx, date_key])
        if current == token:
            continue
        filtered.append(
            {
                "employee_id": employee_id,
                "date": date_key,
                "token": token,
            }
        )
    return filtered


def _bridge_changes_for_draft_apply(
    bridge_payload: Optional[Mapping[str, object]],
    *,
    save_pending: bool,
) -> List[Dict[str, str]]:
    """Apply only live user edits — never bootstrap storage dumps."""

    del save_pending
    if not isinstance(bridge_payload, Mapping):
        return []
    source = str(bridge_payload.get("source") or "")
    if source not in {"message", "save-drain"}:
        return []
    return _grid_component_cell_changes(bridge_payload)


def _grid_component_cell_changes(
    change: Optional[Mapping[str, object]],
) -> List[Dict[str, str]]:
    """Normalize single or batched iframe edit payloads from the master grid."""

    if change is None:
        return []
    if not isinstance(change, Mapping):
        return []
    batched = change.get("changes")
    if isinstance(batched, list):
        normalized: List[Dict[str, str]] = []
        for item in batched:
            if not isinstance(item, Mapping):
                continue
            employee_id = str(item.get("employee_id", "") or "")
            date_key = str(item.get("date", "") or "")
            if employee_id and date_key:
                normalized.append(
                    {
                        "employee_id": employee_id,
                        "date": date_key,
                        "token": str(item.get("token", "") or ""),
                    }
                )
        return normalized
    employee_id = str(change.get("employee_id", "") or "")
    date_key = str(change.get("date", "") or "")
    if employee_id and date_key:
        return [
            {
                "employee_id": employee_id,
                "date": date_key,
                "token": str(change.get("token", "") or ""),
            }
        ]
    return []


def _apply_grid_cell_change_list_to_draft(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    draft_key: str,
    baseline_key: str,
    dates: List[date],
    blocked_map: Dict[str, Dict[date, str]],
    target_hours: Mapping[str, float],
    blocked_sets: Dict[str, Set[date]],
    shift_cell_locks: Set[Tuple[str, date]],
    change_list: Sequence[Mapping[str, str]],
) -> bool:
    if not change_list:
        return False
    draft = _sanitize_schedule_dataframe(st.session_state[draft_key].copy(), dates)
    merged = draft.copy()
    draft_row_index = schedule_frame_row_index_by_employee_id(merged)
    for change_item in change_list:
        row_idx = draft_row_index.get(change_item["employee_id"])
        if row_idx is not None:
            merged.at[row_idx, change_item["date"]] = _display_shift_cell(change_item["token"])
    policy_view, any_applied = _process_staged_grid_edits(
        conn,
        tenant_id=tenant_id,
        period=period,
        rules=rules,
        employees=employees,
        templates=templates,
        draft_frame=draft,
        edited=merged,
        dates=dates,
        blocked_map=blocked_map,
        employee_target_hours=dict(target_hours),
        availability_blocked=blocked_sets,
        locked_cells=shift_cell_locks,
    )
    if any_applied:
        st.session_state[draft_key] = _sanitize_schedule_dataframe(
            policy_view.draft_frame,
            dates,
        )
        st.session_state[baseline_key] = st.session_state[draft_key]
        if not schedule_sess.peek_save_requested(st.session_state, period.id):
            _clear_grid_component_echo(period.id)
            _clear_grid_session_storage_bridge(period.id)
    return any_applied


def _flush_session_storage_grid_edits_before_save(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    draft_key: str,
    baseline_key: str,
    dates: List[date],
    blocked_map: Dict[str, Dict[date, str]],
    target_hours: Mapping[str, float],
    blocked_sets: Dict[str, Set[date]],
) -> None:
    """Last-chance drain of browser-stored grid edits immediately before save."""

    if not schedule_sess.peek_save_requested(st.session_state, period.id):
        return
    draft = _merge_matrix_cache_into_draft(
        period.id,
        _sanitize_schedule_dataframe(st.session_state[draft_key].copy(), dates),
    )
    raw_late_changes = _merge_grid_cell_changes(
        _collect_pending_grid_changes_for_save(
            period.id,
            draft=draft,
            employees=employees,
        ),
        [
            {
                "employee_id": mutation.employee_id,
                "date": mutation.assignment_date.isoformat(),
                "token": mutation.new_token,
            }
            for mutation in _load_pending_mutations(period.id)
        ],
    )
    late_changes = _filter_grid_changes_against_draft(
        draft,
        employees,
        raw_late_changes,
    )
    shift_cell_locks = fetch_shift_cell_locks(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=period.id,
    )
    if _apply_grid_cell_change_list_to_draft(
        conn,
        tenant_id=tenant_id,
        period=period,
        rules=rules,
        employees=employees,
        templates=templates,
        draft_key=draft_key,
        baseline_key=baseline_key,
        dates=dates,
        blocked_map=blocked_map,
        target_hours=target_hours,
        blocked_sets=blocked_sets,
        shift_cell_locks=shift_cell_locks,
        change_list=late_changes,
    ):
        _clear_grid_session_storage_bridge(period.id)


def _resync_empty_draft_from_assignments(
    *,
    period_id: str,
    draft_key: str,
    baseline_key: str,
    draft: pd.DataFrame,
    baseline_from_db: pd.DataFrame,
    dates: List[date],
    employees: List[Dict],
    templates: Dict[str, Dict],
    assignments: Sequence[Dict],
) -> pd.DataFrame:
    """Restore a stale empty session draft from database assignments."""

    if not assignments:
        return draft
    draft_shifts = _count_worked_shifts_in_frame(
        draft,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    raw_cells = _count_raw_grid_shift_cells_in_frame(draft, dates=dates)
    baseline_shifts = _count_worked_shifts_in_frame(
        baseline_from_db,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    if draft_shifts > 0 and raw_cells > 0:
        return draft
    if baseline_shifts <= 0:
        return draft
    pending_mutations = _load_pending_mutations(period_id)
    if pending_mutations and draft_shifts == 0 and raw_cells == 0:
        return draft
    sanitized = _sanitize_schedule_dataframe(baseline_from_db.copy(), dates)
    st.session_state[draft_key] = sanitized
    st.session_state[baseline_key] = sanitized
    _bump_schedule_grid_staging_revision(period_id)
    return sanitized


def _grid_component_lock_toggles(
    change: Optional[Mapping[str, object]],
) -> List[Dict[str, object]]:
    """Normalize lock toggle payloads from the master grid iframe."""

    if change is None or not isinstance(change, Mapping):
        return []
    batched = change.get("lock_toggles")
    if not isinstance(batched, list):
        return []
    normalized: List[Dict[str, object]] = []
    for item in batched:
        if not isinstance(item, Mapping):
            continue
        employee_id = str(item.get("employee_id", "") or "")
        date_key = str(item.get("date", "") or "")
        if employee_id and date_key:
            normalized.append(
                {
                    "employee_id": employee_id,
                    "date": date_key,
                    "locked": bool(item.get("locked", True)),
                    "scope": str(item.get("scope", "week") or "week"),
                    "lock_band": str(item.get("lock_band", "ANY") or "ANY").upper(),
                }
            )
    return normalized


def _grid_component_tally_select(
    change: Optional[Mapping[str, object]],
) -> Optional[Dict[str, str]]:
    if change is None or not isinstance(change, Mapping):
        return None
    tally = change.get("tally_select")
    if not isinstance(tally, Mapping):
        return None
    band = str(tally.get("band", "") or "").upper()
    date_key = str(tally.get("date", "") or "")
    if band in {"D", "E", "N"} and date_key:
        return {"band": band, "date": date_key}
    return None




def _grid_component_messages(
    change: Optional[Mapping[str, object]],
) -> Dict[str, object]:
    return {
        "changes": _grid_component_cell_changes(change),
        "lock_toggles": _grid_component_lock_toggles(change),
    }


def _shift_cell_locks_json(locked_cells: Set[Tuple[str, date]]) -> str:
    payload = [[employee_id, assignment_date.isoformat()] for employee_id, assignment_date in sorted(locked_cells)]
    return json.dumps(payload)


def _apply_shift_cell_lock_toggles(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period_id: str,
    toggles: Sequence[Mapping[str, object]],
    actor: str,
    period_start: date,
    period_end: date,
) -> int:
    if not toggles:
        return 0
    changed = apply_shift_cell_lock_toggles(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=period_id,
        toggles=toggles,
        actor=actor,
        period_start=period_start,
        period_end=period_end,
    )
    if changed:
        for toggle in toggles:
            employee_id = str(toggle.get("employee_id", "") or "")
            date_key = str(toggle.get("date", "") or "")
            locked = bool(toggle.get("locked", True))
            scope = str(toggle.get("scope", "week") or "week")
            action = "Locked week" if locked else "Unlocked week"
            if scope == "day":
                action = "Locked" if locked else "Unlocked"
            st.toast(f"{action} · {employee_id} · {date_key}", icon="🔒")
    return changed


def _count_shift_band_in_matrix_column(
    matrix: pd.DataFrame,
    *,
    date_key: str,
    band: str,
) -> int:
    return count_shift_band_in_column(
        _employee_schedule_matrix(matrix),
        date_key=date_key,
        band=band,
    )


def _tally_counts_from_matrix(
    matrix: pd.DataFrame,
    view_dates: List[date],
) -> Dict[str, Dict[str, int]]:
    """Live D/E/N counts per date column; ignores embedded tally rows and baseline targets."""

    counts: Dict[str, Dict[str, int]] = {}
    for label, band in (
        ("Total Days", "D"),
        ("Total Evenings", "E"),
        ("Total Nights", "N"),
    ):
        day_counts: Dict[str, int] = {}
        for day in view_dates:
            day_counts[day.isoformat()] = _count_shift_band_in_matrix_column(
                matrix,
                date_key=day.isoformat(),
                band=band,
            )
        counts[label] = day_counts
    return counts


def _tally_targets_for_view_dates(view_dates: List[date]) -> Dict[str, Dict[str, int]]:
    targets: Dict[str, Dict[str, int]] = {}
    for day in view_dates:
        date_key = day.isoformat()
        targets[date_key] = {
            band: shift_target_for_date(day, band)
            for band in ("D", "E", "N")
        }
    return targets


def _tally_targets_json(view_dates: List[date]) -> str:
    import json

    weekday_keys = [day.isoformat() for day in view_dates if day.weekday() < 5]
    return json.dumps(
        {
            "targets": _tally_targets_for_view_dates(view_dates),
            "weekdayDateKeys": weekday_keys,
            "weekdayDayBalanceTolerance": 1,
        }
    )


def _tally_cell_status_class(count: int, target: int) -> str:
    if count == target:
        return "tally-ok"
    if count < target:
        return "tally-short"
    return "tally-over"


def _tally_cell_markup(
    day: date,
    band: str,
    count: int,
    *,
    weekday_day_counts: Optional[Sequence[int]] = None,
) -> str:
    weekend_class = " lab-weekend-col" if _is_weekend_schedule_day(day) else ""
    if band == "D" and day.weekday() < 5:
        status = weekday_day_tally_status(count, weekday_day_counts or ())
        display = str(count)
        tally_mode = "balance"
    else:
        target = shift_target_for_date(day, band)
        status = _tally_cell_status_class(count, target)
        display = f"{count}/{target}"
        tally_mode = "fixed"
    return (
        "<td class='"
        + _schedule_day_css_classes(day)
        + " tally-cell "
        + status
        + weekend_class
        + "' data-tally-band='"
        + html_lib.escape(band, quote=True)
        + "' data-tally-date='"
        + html_lib.escape(day.isoformat(), quote=True)
        + "' data-tally-mode='"
        + tally_mode
        + "'>"
        + display
        + "</td>"
    )


def _tally_footer_rows_html_from_matrix(
    matrix: pd.DataFrame,
    view_dates: List[date],
    *,
    compact: bool = False,
) -> str:
    tally_counts = _tally_counts_from_matrix(matrix, view_dates)
    weekday_day_counts = [
        tally_counts["Total Days"][day.isoformat()]
        for day in view_dates
        if day.weekday() < 5
    ]
    footer_rows: List[str] = []
    if compact:
        row_labels = (
            ("Total Days", "Days", "D", "wd ±1"),
            ("Total Evenings", "Evenings", "E", "need 2"),
            ("Total Nights", "Nights", "N", "need 2"),
        )
    else:
        row_labels = (
            ("Total Days", "Total Days", "D", "wd ±1 balance · we need 2"),
            ("Total Evenings", "Total Evenings", "E", "need 2 daily"),
            ("Total Nights", "Total Nights", "N", "need 2 daily"),
        )
    for tally_key, label, band, need_note in row_labels:
        cells: List[str] = []
        for day in view_dates:
            date_key = day.isoformat()
            count = tally_counts[tally_key][date_key]
            cells.append(
                _tally_cell_markup(
                    day,
                    band,
                    count,
                    weekday_day_counts=weekday_day_counts if band == "D" else None,
                )
            )
        footer_rows.append(
            "<tr class='tally-row'>"
            f"<td class='lab-emp-col lab-tally-label tally-label'>"
            f"{html_lib.escape(label)}<span class='lab-emp-sub'>{html_lib.escape(need_note)}</span>"
            f"</td>"
            + "".join(cells)
            + "</tr>"
        )
    return "".join(footer_rows)


def _tally_footer_rows_html(
    period_id: str,
    view_dates: List[date],
    *,
    matrix_fallback: pd.DataFrame,
) -> str:
    """Render D/E/N footer counts from schedule_matrix_{period_id} (live token counts)."""

    matrix = _load_live_schedule_matrix(period_id, matrix_fallback, view_dates)
    return _tally_footer_rows_html_from_matrix(matrix, view_dates)


def _employee_qual_pool(
    employee: Mapping[str, object],
    *,
    emp_quals: Mapping[str, Set[str]],
    qual_id_to_code: Mapping[str, str],
) -> str:
    employee_id = str(employee.get("id", "") or "")
    for qual_id in emp_quals.get(employee_id, set()):
        code = str(qual_id_to_code.get(str(qual_id), "") or "").upper()
        if code in {"MLT", "MLA"}:
            return code
    label = str(employee.get("full_name") or employee.get("Employee") or "").upper()
    if " MLA " in f" {label} " or label.startswith("MLA "):
        return "MLA"
    if " MLT " in f" {label} " or label.startswith("MLT "):
        return "MLT"
    return "MLT"


def _split_schedule_matrix_by_qual_pool(
    frame: pd.DataFrame,
    employees: Sequence[Mapping[str, object]],
    *,
    emp_quals: Mapping[str, Set[str]],
    qual_id_to_code: Mapping[str, str],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if frame.empty or "employee_id" not in frame.columns:
        empty = frame.iloc[0:0].copy()
        return empty, empty
    pool_by_id = {
        str(employee.get("id", "") or ""): _employee_qual_pool(
            employee,
            emp_quals=emp_quals,
            qual_id_to_code=qual_id_to_code,
        )
        for employee in employees
    }
    mlt_ids = {employee_id for employee_id, pool in pool_by_id.items() if pool == "MLT"}
    mla_ids = {employee_id for employee_id, pool in pool_by_id.items() if pool == "MLA"}
    mlt_frame = frame[frame["employee_id"].isin(mlt_ids)].copy()
    mla_frame = frame[frame["employee_id"].isin(mla_ids)].copy()
    return mlt_frame, mla_frame


def _build_master_schedule_grid_html(
    matrix: pd.DataFrame,
    view_dates: List[date],
    *,
    period_id: str,
    edit_mode: bool = False,
    fullscreen: bool = False,
    contract_rows: Optional[Mapping[str, ContractTrackingRow]] = None,
    full_employee_matrix: Optional[pd.DataFrame] = None,
    all_date_keys: Optional[Sequence[str]] = None,
    hours_per_shift: float = 8.0,
    contract_hour_tolerance: float = FULLTIME_CONTRACT_HOUR_TOLERANCE,
    equity_drift_by_employee: Optional[Mapping[str, object]] = None,
    locked_cells: Optional[Set[Tuple[str, date]]] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    focus_fit: bool = False,
    tally_matrix: Optional[pd.DataFrame] = None,
    staging_revision: int = 0,
    ops_metrics_json: Optional[str] = None,
    health_focus_date: Optional[date] = None,
) -> str:
    from lab_scheduler.ui.schedule_focus import focus_mode_grid_stylesheet

    live_matrix = _load_live_schedule_matrix(period_id, matrix, view_dates)
    locked_cells = locked_cells or set()
    locked_lookup = locked_cells
    full_matrix = (
        full_employee_matrix
        if full_employee_matrix is not None
        else _employee_schedule_matrix(live_matrix)
    )
    date_keys = list(all_date_keys or [day.isoformat() for day in view_dates])
    contract_rows = contract_rows or {}
    equity_drift_by_employee = equity_drift_by_employee or {}
    contract_config_json = _contract_row_config_json(
        contract_rows=contract_rows,
        full_matrix=full_matrix,
        view_dates=view_dates,
        date_keys=date_keys,
        hours_per_shift=hours_per_shift,
        tolerance=contract_hour_tolerance,
    )
    rows_by_id = {
        str(row.get("employee_id", "")): row
        for _, row in full_matrix.iterrows()
        if not is_daily_tally_employee_id(row.get("employee_id"))
    }
    header_html = _schedule_grid_thead_html(
        view_dates,
        period_start=period_start,
        focus_date=health_focus_date,
    )
    body_rows: List[str] = []
    for _, row in live_matrix.iterrows():
        employee_id = str(row.get("employee_id", "") or "")
        if is_daily_tally_employee_id(employee_id):
            continue
        employee_label = str(row.get("Employee", ""))
        contract_line = row.get("contract_line_type", "D/E")
        tracking = contract_rows.get(employee_id)
        target_hours = float(tracking.target_hours) if tracking is not None else 0.0
        actual_hours = float(tracking.actual_hours) if tracking is not None else 0.0
        row_class = (
            _contract_hour_row_class(
                actual_hours,
                target_hours,
                hours_per_shift=hours_per_shift,
                tolerance=contract_hour_tolerance,
            )
            if tracking is not None
            else ""
        )
        off_view_shifts = _off_view_worked_shift_count(
            rows_by_id.get(employee_id, row),
            view_dates,
            date_keys,
        )
        full_row = rows_by_id.get(employee_id, row)
        shift_stats = _employee_grid_shift_stats(
            full_row,
            date_keys,
            contract_line,
        )
        equity_drift = equity_drift_by_employee.get(employee_id)
        off_view_mix = _off_view_shift_mix(full_row, view_dates, date_keys)
        emp_cell = (
            f"<td class='lab-emp-col'>{_format_grid_employee_cell(employee_label, actual_hours=actual_hours, target_hours=target_hours, shift_stats=shift_stats, equity_drift=equity_drift)}</td>"
            if tracking is not None
            else f"<td class='lab-emp-col'>{_format_grid_employee_cell(employee_label, shift_stats=shift_stats, equity_drift=equity_drift)}</td>"
        )
        day_cells: List[str] = []
        for day in view_dates:
            day_key = day.isoformat()
            display_value = _display_shift_cell(row.get(day_key, ""))
            cell_locked = (employee_id, day) in locked_lookup
            day_cells.append(
                "<td class='"
                + _schedule_day_css_classes(day, focus_date=health_focus_date)
                + "'>"
                + _shift_pill_html(
                    employee_id=employee_id,
                    day_key=day_key,
                    display_value=display_value,
                    edit_mode=edit_mode,
                    contract_line=contract_line,
                    is_locked=cell_locked,
                )
                + "</td>"
            )
        row_attrs = ""
        if row_class:
            row_attrs = f" class='{row_class}'"
        if employee_id:
            row_attrs += (
                f" data-employee-id='{html_lib.escape(employee_id, quote=True)}'"
                f" data-contract-line='{html_lib.escape(str(contract_line), quote=True)}'"
                f" data-off-view-d='{off_view_mix['D']}'"
                f" data-off-view-e='{off_view_mix['E']}'"
                f" data-off-view-n='{off_view_mix['N']}'"
                f" data-off-view-weekend='{off_view_mix['weekend']}'"
            )
        if tracking is not None:
            row_attrs += (
                f" data-target-hours='{target_hours:g}'"
                f" data-off-view-shifts='{off_view_shifts}'"
                f" data-hours-per-shift='{hours_per_shift:g}'"
            )
        body_rows.append("<tr" + row_attrs + ">" + emp_cell + "".join(day_cells) + "</tr>")

    tally_source = tally_matrix if tally_matrix is not None else live_matrix
    tally_footer = _tally_footer_rows_html_from_matrix(
        tally_source,
        view_dates,
        compact=True,
    )
    tally_targets_json = _tally_targets_json(view_dates)

    wrap_class = "lab-schedule-wrap" + (" lab-edit-mode" if edit_mode else "")
    if fullscreen:
        wrap_class += " lab-schedule-wrap--fullscreen"
    if focus_fit:
        wrap_class += " lab-schedule-wrap--focus-fit"
    edit_flag = "true" if edit_mode else "false"
    focus_fit_flag = "true" if focus_fit else "false"
    focus_hint = (
        "<div class='lab-fullscreen-toolbar'>"
        "<span class='lab-fs-toolbar-title'>Fullscreen</span>"
        "<label class='lab-fs-control'>Zoom "
        "<input type='range' id='lab-fs-zoom' min='50' max='200' value='100' step='5'>"
        "<span id='lab-fs-zoom-val'>100%</span></label>"
        "<label class='lab-fs-control lab-fs-stretch-toggle'>"
        "<input type='checkbox' id='lab-fs-stretch' checked> Stretch to screen</label>"
        "</div>"
        if focus_fit
        else ""
    )
    focus_styles = focus_mode_grid_stylesheet() if focus_fit else ""

    return (
        focus_styles
        + _master_schedule_grid_stylesheet(fullscreen=fullscreen)
        + f"<div class='{wrap_class}' id='lab-schedule-wrap'>"
        + focus_hint
        + "<div class='lab-tally-legend'>"
        + "<strong>Daily totals</strong> (sticky footer): "
        + "<span style='color:#166534'>■</span> on target · "
        + "<span style='color:#854d0e'>■</span> short · "
        + "<span style='color:#991b1b'>■</span> over — "
        + "Weekday <strong>Days</strong> show count only (green when every weekday is within "
        + "<strong>±1</strong> of the others). "
        + "Weekend Days and all Evenings/Nights show <strong>actual / needed</strong> (2 daily). "
        + "Employee rows show <strong>Alt %</strong> (evening on D/E, night on D/N), day %, and "
        + "<strong>Wknd</strong> shift count for the full period. "
        + "Employee rows: <span style='color:#166534'>green</span> on target, "
        + "<span style='color:#854d0e'>yellow</span> any hour under, "
        + "<span style='color:#991b1b'>red</span> any hour over. "
        + "Equity line (separate from hours): "
        + "<span style='color:#6b21a8'>purple</span> alternate-band drift, "
        + "<span style='color:#115e59'>teal</span> weekend drift, "
        + "<span style='color:#92400e'>amber</span> both. "
        + "<strong>Green ring</strong> = week locked by you (right-click any day that week to unlock). "
        + "Locked cells are skipped when you drag-fill. "
        + "<strong>Drag</strong> across cells, then pick D/E/N/— to fill unlocked cells (locked cells are skipped)."
        + "</div>"
        + ("<div class='lab-focus-scaler'><div class='lab-focus-scaler-inner'>" if focus_fit else "")
        + "<table class='lab-schedule-grid'>"
        + header_html
        + "<tbody>"
        + "".join(body_rows)
        + "</tbody><tfoot>"
        + tally_footer
        + "</tfoot></table>"
        + ("</div></div>" if focus_fit else "")
        + "</div>"
        + """
<script>
(function () {
  const editMode = """
        + edit_flag
        + """;
  const contractConfig = """
        + contract_config_json
        + """;
  const contractRows = contractConfig.rows || {};
  const contractHoursPerShift = contractConfig.hoursPerShift || 8;
  const contractHourTolerance = contractConfig.tolerance || 8;
  const tallyConfig = """
        + tally_targets_json
        + """;
  const tallyTargets = tallyConfig.targets || tallyConfig;
  const weekdayDateKeys = tallyConfig.weekdayDateKeys || [];
  const weekdayDayBalanceTolerance = tallyConfig.weekdayDayBalanceTolerance || 1;
  const styleLookup = """
        + _shift_style_lookup_json()
        + """;
  const lockedCellList = """
        + _shift_cell_locks_json(locked_cells)
        + """;
  const weekLockConfig = """
        + json.dumps(
            {
                "periodStart": period_start.isoformat() if period_start else "",
                "periodEnd": period_end.isoformat() if period_end else "",
            }
        )
        + """;
  const periodStart = weekLockConfig.periodStart || "";
  const periodEnd = weekLockConfig.periodEnd || "";
  const lockedCells = new Set(
    (lockedCellList || []).map(function (item) {
      return item[0] + "|" + item[1];
    })
  );
  function lockKey(employeeId, dateKey) {
    return employeeId + "|" + dateKey;
  }
  function styleForToken(token) {
    if (styleLookup[token]) return styleLookup[token];
    if (!token || token === "—" || token === "-") return styleLookup["."] || styleLookup["—"];
    return styleLookup[token.slice(0, 1)] || styleLookup["."];
  }
  function tokenClassForToken(token) {
    if (token === "D" || token === "M") return "lab-shift-pill-d";
    if (token === "E") return "lab-shift-pill-e";
    if (token === "N") return "lab-shift-pill-n";
    return "lab-shift-pill-off";
  }
  function paintSelect(select, token) {
    const style = styleForToken(token);
    select.dataset.displayToken = token;
    select.classList.remove("lab-shift-pill-d", "lab-shift-pill-e", "lab-shift-pill-n", "lab-shift-pill-off");
    select.classList.add(tokenClassForToken(token));
    select.style.background = style.bg;
    select.style.color = style.fg;
  }
  """
        + _GRID_SHARED_SESSION_STORAGE_JS
        + """
  const gridChangeQueueKey = """
        + json.dumps(period_id)
        + """;
  const stagingRevision = """
        + json.dumps(staging_revision)
        + """;
  const opsMetricsConfig = """
        + (ops_metrics_json or "{}")
        + """;
  if (!window.__labGridChangeQueues) {
    window.__labGridChangeQueues = {};
  }
  window.__labGridChangeQueues[gridChangeQueueKey] = [];
  const gridSessionStorageKey = "lab_grid_pending_" + gridChangeQueueKey;
  function mergeGridChangeItem(queue, item) {
    if (!item || !item.employee_id || !item.date) return;
    const existing = queue.findIndex(function (entry) {
      return entry.employee_id === item.employee_id && entry.date === item.date;
    });
    if (existing >= 0) {
      queue[existing] = item;
    } else {
      queue.push(item);
    }
  }
  function persistGridPayload(payload) {
    try {
      const storage = labSharedSessionStorage();
      const raw = storage.getItem(gridSessionStorageKey);
      const stored = raw ? JSON.parse(raw) : { changes: [] };
      if (!stored.changes) stored.changes = [];
      if (payload.changes && Array.isArray(payload.changes)) {
        payload.changes.forEach(function (item) {
          mergeGridChangeItem(stored.changes, item);
        });
      } else if (payload.employee_id && payload.date) {
        mergeGridChangeItem(stored.changes, payload);
      }
      if (payload.lock_toggles) {
        stored.lock_toggles = payload.lock_toggles;
      }
      if (payload.tally_select) {
        stored.tally_select = payload.tally_select;
      }
      storage.setItem(gridSessionStorageKey, JSON.stringify(stored));
      labGridPendingStoreSet(gridSessionStorageKey, stored);
      ensureTopGridPersistListener();
      try {
        var topWin = labGridTopRoot();
        topWin.postMessage(
          { type: "lab-grid-persist", storageKey: gridSessionStorageKey, payload: stored },
          "*"
        );
      } catch (postErr) {}
    } catch (storageError) {
      /* ignore quota / privacy mode */
    }
  }
  function postChange(payload) {
    persistGridPayload(payload);
  }
  function flushPendingGridChanges() {
    if (window.__labGridPostTimer) {
      clearTimeout(window.__labGridPostTimer);
      window.__labGridPostTimer = null;
    }
    const pending = window.__labGridChangeQueues[gridChangeQueueKey] || [];
    if (pending.length) {
      postChange({ changes: pending.slice() });
      window.__labGridChangeQueues[gridChangeQueueKey] = [];
    }
  }
  function queueGridChange(payload, immediate) {
    const queue = window.__labGridChangeQueues[gridChangeQueueKey] || [];
    const existing = queue.findIndex(function (item) {
      return item.employee_id === payload.employee_id && item.date === payload.date;
    });
    if (existing >= 0) {
      queue[existing] = payload;
    } else {
      queue.push(payload);
    }
    window.__labGridChangeQueues[gridChangeQueueKey] = queue;
    // Persist immediately so Save-triggered reruns can drain sessionStorage even
    // when the debounced Streamlit round-trip has not fired yet.
    persistGridPayload({ changes: [payload] });
    if (immediate) {
      flushPendingGridChanges();
      return;
    }
    if (window.__labGridPostTimer) {
      clearTimeout(window.__labGridPostTimer);
    }
    window.__labGridPostTimer = setTimeout(flushPendingGridChanges, 400);
  }
  function normalizeBand(token) {
    if (!token || token === "—" || token === "-") return null;
    if (token === "D" || token === "M") return "D";
    if (token === "E") return "E";
    if (token === "N") return "N";
    return null;
  }
  function weekdayDayTallyStatus(count, weekdayCounts) {
    if (!weekdayCounts.length) return "tally-ok";
    var lo = Math.min.apply(null, weekdayCounts);
    var hi = Math.max.apply(null, weekdayCounts);
    var tol = weekdayDayBalanceTolerance;
    if (count >= hi - tol && count <= lo + tol) return "tally-ok";
    if (count < hi - tol) return "tally-short";
    return "tally-over";
  }
  function isWorkedShiftToken(token) {
    if (!token || token === "—" || token === "-") return false;
    if (token === "D" || token === "M" || token === "E" || token === "N") return true;
    return false;
  }
  function applyLockVisual(el, locked) {
    const wrap = el.closest(".lab-shift-cell-wrap");
    if (!wrap) return;
    wrap.classList.remove("lab-shift-locked", "lab-shift-locked-empty");
    if (!locked) {
      if (el.tagName === "SELECT") el.disabled = false;
      el.dataset.locked = "false";
      return;
    }
    const token = el.value || el.dataset.displayToken || "";
    const worked = isWorkedShiftToken(token);
    wrap.classList.add(worked ? "lab-shift-locked" : "lab-shift-locked-empty");
    if (el.tagName === "SELECT") el.disabled = worked;
    el.dataset.locked = "true";
  }
  function queueGridLockToggle(payload) {
    postChange({ lock_toggles: [payload] });
  }
  function parseDateKey(dateKey) {
    const parts = dateKey.split("-");
    if (parts.length !== 3) return null;
    return new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
  }
  function formatDateKey(dateObj) {
    const y = dateObj.getFullYear();
    const m = String(dateObj.getMonth() + 1).padStart(2, "0");
    const d = String(dateObj.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + d;
  }
  function datesInLockWeek(anchorDateKey) {
    const anchor = parseDateKey(anchorDateKey);
    if (!anchor) return [anchorDateKey];
    const mondayOffset = (anchor.getDay() + 6) % 7;
    const weekStart = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate() - mondayOffset);
    const dates = [];
    for (let i = 0; i < 7; i++) {
      const day = new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + i);
      const key = formatDateKey(day);
      if (periodStart && key < periodStart) continue;
      if (periodEnd && key > periodEnd) continue;
      dates.push(key);
    }
    return dates.length ? dates : [anchorDateKey];
  }
  function weekLockState(employeeId, weekDates) {
    let lockedCount = 0;
    weekDates.forEach(function (dateKey) {
      if (lockedCells.has(lockKey(employeeId, dateKey))) lockedCount += 1;
    });
    if (lockedCount === 0) return "none";
    if (lockedCount === weekDates.length) return "all";
    return "partial";
  }
  function refreshWeekLockVisuals(employeeId, weekDates) {
    weekDates.forEach(function (dateKey) {
      const cellEl = document.querySelector(
        '.lab-shift-inline-select[data-employee-id="' +
          employeeId +
          '"][data-date="' +
          dateKey +
          '"], .lab-shift-pill-readonly[data-employee-id="' +
          employeeId +
          '"][data-date="' +
          dateKey +
          '"]'
      );
      if (cellEl) {
        applyLockVisual(cellEl, lockedCells.has(lockKey(employeeId, dateKey)));
      }
    });
  }
  function wireCellLockToggle(el) {
    el.addEventListener("contextmenu", function (event) {
      event.preventDefault();
      const employeeId = el.dataset.employeeId;
      const dateKey = el.dataset.date;
      if (!employeeId || !dateKey) return;
      const band = normalizeBand(el.value || el.dataset.displayToken || "");
      const row = el.closest("tr[data-contract-line]");
      const contractLine = row ? (row.dataset.contractLine || "").toUpperCase() : "";
      const weekDates = datesInLockWeek(dateKey);
      const weekFullyLocked = weekLockState(employeeId, weekDates) === "all";
      if (band === "N" && contractLine === "D/N" && !weekFullyLocked) return;
      const nextLocked = !weekFullyLocked;
      const lockBand = band === "D" || band === "E" ? band : "ANY";
      weekDates.forEach(function (weekDateKey) {
        const key = lockKey(employeeId, weekDateKey);
        if (nextLocked) lockedCells.add(key);
        else lockedCells.delete(key);
      });
      refreshWeekLockVisuals(employeeId, weekDates);
      queueGridLockToggle({
        employee_id: employeeId,
        date: dateKey,
        locked: nextLocked,
        scope: "week",
        lock_band: lockBand,
      });
    });
  }
  function isWeekendDateKey(dateKey) {
    if (!dateKey) return false;
    const parts = dateKey.split("-");
    if (parts.length !== 3) return false;
    const day = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
    const weekday = day.getDay();
    return weekday === 0 || weekday === 6;
  }
  function recomputeRowShiftStats() {
    document.querySelectorAll("tr[data-employee-id][data-contract-line]").forEach(function (row) {
      const contractLine = (row.dataset.contractLine || "D/E").toUpperCase();
      const counts = {
        D: parseInt(row.dataset.offViewD || "0", 10) || 0,
        E: parseInt(row.dataset.offViewE || "0", 10) || 0,
        N: parseInt(row.dataset.offViewN || "0", 10) || 0,
        weekend: parseInt(row.dataset.offViewWeekend || "0", 10) || 0,
      };
      row.querySelectorAll(".lab-shift-inline-select, .lab-shift-pill-readonly").forEach(function (el) {
        const token = el.value || el.dataset.displayToken || "";
        const band = normalizeBand(token);
        if (!band) return;
        counts[band] += 1;
        const dateKey = el.dataset.date;
        if (dateKey && isWeekendDateKey(dateKey)) counts.weekend += 1;
      });
      let total = 0;
      let alternate = 0;
      let altBand = "E";
      let altLabel = "evening";
      const dayCount = counts.D;
      const dayLabel = "D";
      if (contractLine === "D/N") {
        total = counts.D + counts.N;
        alternate = counts.N;
        altBand = "N";
        altLabel = "night";
      } else if (contractLine === "D/E") {
        total = counts.D + counts.E;
        alternate = counts.E;
        altBand = "E";
        altLabel = "evening";
      } else {
        total = counts.D + counts.E + counts.N;
        alternate = counts.E + counts.N;
        altBand = "E+N";
        altLabel = "alt";
      }
      const stats = row.querySelector("[data-role='shift-stats']");
      if (!stats || total <= 0) return;
      const altPct = (100.0 * alternate / total);
      const dayPct = (100.0 * dayCount / total);
      const compactRow = !!row.querySelector(".lab-emp-cell-compact");
      if (compactRow) {
        stats.textContent =
          "Alt " + altPct.toFixed(0) + "% " + alternate + altBand + "/" + total + " · " +
          dayPct.toFixed(0) + "% " + dayLabel + " · Wk " + counts.weekend;
      } else {
        stats.textContent =
          "Alt " + altPct.toFixed(1) + "% " + altLabel + " (" + alternate + altBand + "/" + total + ") · " +
          dayPct.toFixed(0) + "% " + dayLabel + " · Wknd " + counts.weekend;
      }
    });
  }
  function contractRowClass(actual, target) {
    if (!target) return "";
    if (actual < target - 0.25) return "lab-emp-row-short";
    if (actual > target + contractHoursPerShift + 0.25) return "lab-emp-row-over";
    if (actual > target + 0.25) return "lab-emp-row-over-warn";
    return "lab-emp-row-ok";
  }
  function contractHoursSummaryClass(actual, target) {
    const rowClass = contractRowClass(actual, target);
    if (rowClass === "lab-emp-row-short") return "lab-emp-hours-short";
    if (rowClass === "lab-emp-row-over-warn") return "lab-emp-hours-over-warn";
    if (rowClass === "lab-emp-row-over") return "lab-emp-hours-over";
    return "lab-emp-hours-ok";
  }
  function recomputeRowContractHours() {
    document.querySelectorAll("tr[data-employee-id][data-target-hours]").forEach(function (row) {
      const employeeId = row.dataset.employeeId;
      const target = parseFloat(row.dataset.targetHours || "0");
      if (!target) return;
      const offView = parseInt(row.dataset.offViewShifts || "0", 10) || 0;
      let visibleWorked = 0;
      row.querySelectorAll(".lab-shift-inline-select, .lab-shift-pill-readonly").forEach(function (el) {
        const token = el.value || el.dataset.displayToken || "";
        if (isWorkedShiftToken(token)) visibleWorked += 1;
      });
      const actual = (offView + visibleWorked) * contractHoursPerShift;
      row.classList.remove("lab-emp-row-short", "lab-emp-row-over-warn", "lab-emp-row-over", "lab-emp-row-ok");
      row.classList.add(contractRowClass(actual, target));
      const summary = row.querySelector("[data-role='hours-summary']");
      if (summary) {
        const compactRow = !!row.querySelector(".lab-emp-cell-compact");
        summary.textContent = compactRow
          ? actual.toFixed(0) + "/" + target.toFixed(0) + "h"
          : actual.toFixed(0) + "h / " + target.toFixed(0) + "h target";
        summary.classList.remove("lab-emp-hours-short", "lab-emp-hours-over-warn", "lab-emp-hours-over", "lab-emp-hours-ok");
        summary.classList.add("lab-emp-hours", contractHoursSummaryClass(actual, target));
      }
      if (contractRows[employeeId]) {
        contractRows[employeeId].actual = actual;
      }
    });
  }
  function visibleViewGapCount() {
    var gaps = 0;
    document.querySelectorAll("td.tally-cell[data-tally-band]").forEach(function (cell) {
      var mode = cell.dataset.tallyMode || "fixed";
      if (mode === "balance") return;
      var parts = (cell.textContent || "").split("/");
      if (parts.length !== 2) return;
      var count = parseInt(parts[0], 10);
      var target = parseInt(parts[1], 10);
      if (!isNaN(count) && !isNaN(target) && count < target) {
        gaps += target - count;
      }
    });
    return gaps;
  }
  function applyOpsRibbonMetricsToDocument(doc, hoursDelta, gapCount) {
    if (!doc) return false;
    var hoursEl = doc.getElementById("lab-ops-hours-deficit");
    var gapEl = doc.getElementById("lab-ops-gap-count");
    if (!hoursEl && !gapEl) return false;
    if (hoursEl) {
      if (Math.abs(hoursDelta) < 0.5) {
        hoursEl.textContent = "Balanced";
        hoursEl.className = "lab-ops-metric-value lab-ops-metric-ok";
      } else {
        hoursEl.textContent = (hoursDelta >= 0 ? "+" : "") + Math.round(hoursDelta) + "h";
        hoursEl.className = "lab-ops-metric-value lab-ops-metric-warn";
      }
    }
    if (gapEl) {
      gapEl.textContent = String(Math.max(0, Math.round(gapCount)));
    }
    return true;
  }
  function shouldApplyOpsMetrics(rev) {
    var root = window;
    try {
      if (window.parent && window.parent.document) {
        root = window.parent;
      }
    } catch (parentErr) {}
    if (!root.__labOpsMetricsRevision) root.__labOpsMetricsRevision = 0;
    rev = Number(rev || 0);
    if (rev > 0 && rev < root.__labOpsMetricsRevision) return false;
    if (rev > 0) root.__labOpsMetricsRevision = rev;
    return true;
  }
  function recomputeOpsRibbonMetrics() {
    if (!shouldApplyOpsMetrics(stagingRevision)) return;
    var totalTarget = Number(opsMetricsConfig.contractTargetTotal || 0);
    var totalActual = 0;
    document.querySelectorAll("tr[data-employee-id][data-target-hours]").forEach(function (row) {
      var offView = parseInt(row.dataset.offViewShifts || "0", 10) || 0;
      var visibleWorked = 0;
      row.querySelectorAll(".lab-shift-inline-select, .lab-shift-pill-readonly").forEach(function (el) {
        var token = el.value || el.dataset.displayToken || "";
        if (isWorkedShiftToken(token)) visibleWorked += 1;
      });
      totalActual += (offView + visibleWorked) * contractHoursPerShift;
    });
    var hoursDelta = totalActual - totalTarget;
    var visibleGapsNow = visibleViewGapCount();
    var visibleBaseline = Number(opsMetricsConfig.visibleGapBaseline || 0);
    var fullGapCount = Number(opsMetricsConfig.fullGapCount || 0);
    var gapCount = fullGapCount + (visibleGapsNow - visibleBaseline);
    var payload = {
      type: "lab-ops-metrics",
      hoursDelta: hoursDelta,
      gapCount: gapCount,
      stagingRevision: Number(stagingRevision || 0),
    };
    var updated = false;
    try {
      if (window.parent && window.parent.document) {
        updated = applyOpsRibbonMetricsToDocument(window.parent.document, hoursDelta, gapCount);
      }
    } catch (parentErr) {
      updated = false;
    }
    if (!updated) {
      try {
        window.parent.postMessage(payload, "*");
      } catch (err) {}
    }
    try {
      applyOpsRibbonMetricsToDocument(document, hoursDelta, gapCount);
    } catch (localErr) {}
  }
  var opsRibbonRefreshTimerIds = [];
  function scheduleOpsRibbonRefresh() {
    opsRibbonRefreshTimerIds.forEach(function (timerId) { clearTimeout(timerId); });
    opsRibbonRefreshTimerIds = [];
    recomputeOpsRibbonMetrics();
  }
  window.addEventListener("beforeunload", function () {
    opsRibbonRefreshTimerIds.forEach(function (timerId) { clearTimeout(timerId); });
    opsRibbonRefreshTimerIds = [];
  });
  function recomputeTallies(skipOpsRibbon) {
    const counts = { D: {}, E: {}, N: {} };
    document.querySelectorAll(".lab-shift-inline-select").forEach(function (select) {
      const dateKey = select.dataset.date;
      const band = normalizeBand(select.value || select.dataset.displayToken);
      if (!dateKey || !band) return;
      counts[band][dateKey] = (counts[band][dateKey] || 0) + 1;
    });
    document.querySelectorAll(".lab-shift-pill-readonly").forEach(function (pill) {
      const dateKey = pill.dataset.date;
      const band = normalizeBand(pill.dataset.displayToken);
      if (!dateKey || !band) return;
      counts[band][dateKey] = (counts[band][dateKey] || 0) + 1;
    });
    const weekdayDayCounts = weekdayDateKeys.map(function (dateKey) {
      return (counts.D && counts.D[dateKey]) || 0;
    });
    document.querySelectorAll("td.tally-cell[data-tally-band]").forEach(function (cell) {
      const band = cell.dataset.tallyBand;
      const dateKey = cell.dataset.tallyDate;
      const mode = cell.dataset.tallyMode || "fixed";
      const count = (counts[band] && counts[band][dateKey]) || 0;
      cell.classList.remove("tally-ok", "tally-short", "tally-over");
      if (mode === "balance") {
        cell.textContent = String(count);
        cell.classList.add(weekdayDayTallyStatus(count, weekdayDayCounts));
        return;
      }
      const dayTargets = tallyTargets[dateKey] || {};
      const target = dayTargets[band] || 0;
      cell.textContent = count + "/" + target;
      if (count === target) cell.classList.add("tally-ok");
      else if (count < target) cell.classList.add("tally-short");
      else cell.classList.add("tally-over");
    });
    recomputeRowContractHours();
    recomputeRowShiftStats();
    if (!skipOpsRibbon) {
      recomputeOpsRibbonMetrics();
    }
    requestAnimationFrame(function () {
      if (typeof fitEpicGridToViewport === "function") {
        fitEpicGridToViewport();
      }
    });
  }
  function wireInlineSelect(select) {
    paintSelect(select, select.value || select.dataset.displayToken || "—");
    if (select.dataset.locked === "true") {
      applyLockVisual(select, true);
    }
    wireCellLockToggle(select);
    select.addEventListener("change", function () {
      if (select.disabled) {
        select.value = select.dataset.displayToken || select.value;
        return;
      }
      const token = select.value;
      paintSelect(select, token);
      recomputeTallies();
      recomputeRowContractHours();
      recomputeRowShiftStats();
      queueGridChange({
        employee_id: select.dataset.employeeId,
        date: select.dataset.date,
        token: token,
      });
    });
  }
  function wireReadonlyPill(pill) {
    const token = pill.dataset.displayToken || "—";
    pill.textContent = (!token || token === "—" || token === "-") ? "—" : token;
    if (pill.dataset.locked === "true") {
      applyLockVisual(pill, true);
    }
    wireCellLockToggle(pill);
  }
  document.querySelectorAll(".lab-shift-inline-select").forEach(wireInlineSelect);
  document.querySelectorAll(".lab-shift-pill-readonly").forEach(wireReadonlyPill);
  ensureTopGridPersistListener();
  function initDragAreaFill() {
    if (editMode !== true && editMode !== "true") return;
    const table = document.querySelector(".lab-schedule-grid tbody");
    if (!table || table.dataset.dragFillReady === "1") return;
    table.dataset.dragFillReady = "1";
    let dragging = false;
    let dragAdditive = false;
    let dragStartX = 0;
    let dragStartY = 0;
    let selectSessionActive = false;
    const selected = new Set();
    const baseSelection = new Set();
    let paletteEl = null;
    let marqueeEl = null;
    let suppressPaletteDismissUntil = 0;
    let activePointerId = null;
    const DRAG_THRESHOLD = 4;
    const offToken = """
        + json.dumps(EMPTY_SHIFT_DISPLAY)
        + """;
    function rectsIntersect(a, b) {
      return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
    }
    function ensureMarquee() {
      if (!marqueeEl) {
        marqueeEl = document.createElement("div");
        marqueeEl.className = "lab-marquee-select";
        marqueeEl.setAttribute("aria-hidden", "true");
        document.body.appendChild(marqueeEl);
      }
      return marqueeEl;
    }
    function hideMarquee() {
      if (marqueeEl) marqueeEl.style.display = "none";
    }
    function updateMarquee(x1, y1, x2, y2) {
      const el = ensureMarquee();
      el.style.display = "block";
      el.style.left = x1 + "px";
      el.style.top = y1 + "px";
      el.style.width = Math.max(0, x2 - x1) + "px";
      el.style.height = Math.max(0, y2 - y1) + "px";
    }
    function contractLineForSelect(select) {
      const row = select.closest("tr[data-contract-line]");
      return row ? String(row.dataset.contractLine || "D/E").toUpperCase() : "D/E";
    }
    function allowedWorkedTokensForLine(line) {
      if (line === "D/N") return ["D", "N"];
      return ["D", "E"];
    }
    function allowedTokensForSelect(select) {
      return [offToken].concat(allowedWorkedTokensForLine(contractLineForSelect(select)));
    }
    function allowedTokensForSelection() {
      const allowed = new Set();
      selected.forEach(function (key) {
        const parts = key.split("|");
        const select = document.querySelector(
          'select.lab-shift-inline-select[data-employee-id="' +
            parts[0] +
            '"][data-date="' +
            parts[1] +
            '"]'
        );
        if (!select) return;
        allowedTokensForSelect(select).forEach(function (token) {
          allowed.add(token);
        });
      });
      return allowed;
    }
    function cellKey(wrap) {
      const el = wrap.querySelector("[data-employee-id]");
      if (!el) return "";
      return el.dataset.employeeId + "|" + el.dataset.date;
    }
    function wrapFromTarget(target) {
      const td = target.closest("td");
      if (!td || td.classList.contains("lab-emp-col")) return null;
      return td.querySelector(".lab-shift-cell-wrap");
    }
    function isEditableWrap(wrap) {
      if (!wrap) return false;
      const select = wrap.querySelector(".lab-shift-inline-select");
      if (!select) return false;
      if (select.dataset.locked === "true" || select.disabled) return false;
      return true;
    }
    function applyRectSelection(x1, y1, x2, y2) {
      selected.clear();
      baseSelection.forEach(function (key) {
        selected.add(key);
      });
      const selRect = { left: x1, top: y1, right: x2, bottom: y2 };
      table.querySelectorAll(".lab-shift-cell-wrap").forEach(function (wrap) {
        if (!isEditableWrap(wrap)) return;
        const cellRect = wrap.getBoundingClientRect();
        if (!rectsIntersect(selRect, cellRect)) return;
        const key = cellKey(wrap);
        if (key) selected.add(key);
      });
    }
    function updateSelectionVisual() {
      document.querySelectorAll(".lab-shift-cell-wrap.lab-drag-selected").forEach(function (wrap) {
        wrap.classList.remove("lab-drag-selected");
      });
      selected.forEach(function (key) {
        const parts = key.split("|");
        const select = document.querySelector(
          'select.lab-shift-inline-select[data-employee-id="' +
            parts[0] +
            '"][data-date="' +
            parts[1] +
            '"]'
        );
        if (select && select.closest(".lab-shift-cell-wrap")) {
          select.closest(".lab-shift-cell-wrap").classList.add("lab-drag-selected");
        }
      });
    }
    function hidePalette() {
      if (paletteEl && paletteEl.parentNode) paletteEl.parentNode.removeChild(paletteEl);
      paletteEl = null;
    }
    function clearSelection() {
      selected.clear();
      baseSelection.clear();
      updateSelectionVisual();
      hidePalette();
      hideMarquee();
    }
    function applyDragToken(token) {
      let applied = 0;
      let skippedContract = 0;
      const batchChanges = [];
      selected.forEach(function (key) {
        const parts = key.split("|");
        const select = document.querySelector(
          'select.lab-shift-inline-select[data-employee-id="' +
            parts[0] +
            '"][data-date="' +
            parts[1] +
            '"]'
        );
        const wrap = select ? select.closest(".lab-shift-cell-wrap") : null;
        if (!isEditableWrap(wrap) || !select) return;
        const allowed = allowedTokensForSelect(select);
        if (allowed.indexOf(token) < 0) {
          skippedContract += 1;
          return;
        }
        if ((select.value || select.dataset.displayToken || "") === token) return;
        select.value = token;
        paintSelect(select, token);
        batchChanges.push({
          employee_id: select.dataset.employeeId,
          date: select.dataset.date,
          token: token,
        });
        applied += 1;
      });
      clearSelection();
      if (applied > 0) {
        postChange({ changes: batchChanges });
        recomputeTallies();
        recomputeRowContractHours();
        recomputeRowShiftStats();
      }
    }
    function bindPaletteToken(btn, token) {
      function activate(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        applyDragToken(token);
      }
      btn.addEventListener("pointerdown", activate);
      btn.addEventListener("click", activate);
    }
    function showPalette(clientX, clientY, editableCount, skippedLocked) {
      hidePalette();
      const allowed = allowedTokensForSelection();
      paletteEl = document.createElement("div");
      paletteEl.className = "lab-drag-fill-palette";
      const label = document.createElement("span");
      label.className = "lab-drag-fill-label";
      label.textContent = "Set " + editableCount + " cell(s)";
      paletteEl.appendChild(label);
      [offToken, "D", "E", "N"].forEach(function (token) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "lab-drag-fill-btn";
        btn.textContent = token === offToken ? "—" : token;
        const enabled = allowed.has(token);
        btn.disabled = !enabled;
        btn.title = enabled
          ? "Apply " + (token === offToken ? "off" : token) + " to selection"
          : "Not valid for every selected row (check D/E vs D/N lines)";
        if (enabled) {
          bindPaletteToken(btn, token);
        }
        paletteEl.appendChild(btn);
      });
      if (skippedLocked > 0) {
        const note = document.createElement("span");
        note.className = "lab-drag-fill-skipped";
        note.textContent = skippedLocked + " locked skipped";
        paletteEl.appendChild(note);
      }
      paletteEl.addEventListener("mousedown", function (ev) {
        ev.stopPropagation();
      });
      paletteEl.addEventListener("mouseup", function (ev) {
        ev.stopPropagation();
      });
      paletteEl.addEventListener("pointerdown", function (ev) {
        ev.stopPropagation();
      });
      document.body.appendChild(paletteEl);
      const rect = paletteEl.getBoundingClientRect();
      paletteEl.style.left = Math.max(8, Math.min(clientX, window.innerWidth - rect.width - 8)) + "px";
      paletteEl.style.top = Math.max(8, Math.min(clientY, window.innerHeight - rect.height - 8)) + "px";
    }
    function finishSelectionSession(e) {
      if (!selectSessionActive) return false;
      const wasDragging = dragging;
      selectSessionActive = false;
      document.body.classList.remove("lab-marquee-dragging");
      hideMarquee();
      if (table.releasePointerCapture && activePointerId !== null) {
        try { table.releasePointerCapture(activePointerId); } catch (_) {}
        activePointerId = null;
      }
      if (!wasDragging) {
        if (!dragAdditive) clearSelection();
        dragging = false;
        return true;
      }
      dragging = false;
      if (e && e.preventDefault) e.preventDefault();
      let editable = 0;
      let skippedLocked = 0;
      selected.forEach(function (key) {
        const parts = key.split("|");
        const select = document.querySelector(
          'select.lab-shift-inline-select[data-employee-id="' +
            parts[0] +
            '"][data-date="' +
            parts[1] +
            '"]'
        );
        const wrap = select ? select.closest(".lab-shift-cell-wrap") : null;
        if (isEditableWrap(wrap)) editable += 1;
        else skippedLocked += 1;
      });
      if (editable > 0) {
        const clientX = e.clientX;
        const clientY = e.clientY;
        suppressPaletteDismissUntil = Date.now() + 450;
        showPalette(clientX, clientY, editable, skippedLocked);
      } else {
        clearSelection();
      }
      return true;
    }
    table.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return;
      if (Date.now() < suppressPaletteDismissUntil) return;
      if (e.target.closest(".lab-drag-fill-palette")) return;
      const td = e.target.closest("td");
      if (!td || !table.contains(td) || td.classList.contains("lab-emp-col")) return;
      selectSessionActive = true;
      dragStartX = e.clientX;
      dragStartY = e.clientY;
      dragging = false;
      dragAdditive = e.shiftKey;
      baseSelection.clear();
      if (dragAdditive) {
        selected.forEach(function (key) {
          baseSelection.add(key);
        });
      } else {
        selected.clear();
        updateSelectionVisual();
      }
      hidePalette();
      hideMarquee();
      if (e.pointerId !== undefined && table.setPointerCapture) {
        try {
          table.setPointerCapture(e.pointerId);
          activePointerId = e.pointerId;
        } catch (_) {}
      }
    });
    document.addEventListener("mousemove", function (e) {
      if (!selectSessionActive || !(e.buttons & 1)) return;
      const travel = Math.abs(e.clientX - dragStartX) + Math.abs(e.clientY - dragStartY);
      if (!dragging && travel < DRAG_THRESHOLD) return;
      if (!dragging) {
        dragging = true;
        document.body.classList.add("lab-marquee-dragging");
      }
      const x1 = Math.min(dragStartX, e.clientX);
      const y1 = Math.min(dragStartY, e.clientY);
      const x2 = Math.max(dragStartX, e.clientX);
      const y2 = Math.max(dragStartY, e.clientY);
      updateMarquee(x1, y1, x2, y2);
      applyRectSelection(x1, y1, x2, y2);
      updateSelectionVisual();
    });
    document.addEventListener("mouseup", function (e) {
      if (e.target.closest(".lab-drag-fill-palette")) return;
      finishSelectionSession(e);
    });
    table.addEventListener("click", function (e) {
      if (Date.now() < suppressPaletteDismissUntil) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        selectSessionActive = false;
        document.body.classList.remove("lab-marquee-dragging");
        hideMarquee();
        clearSelection();
        return;
      }
      if (!paletteEl) return;
      const shortcuts = {
        d: "D",
        D: "D",
        e: "E",
        E: "E",
        n: "N",
        N: "N",
        "-": offToken,
        Delete: offToken,
        Backspace: offToken,
      };
      const token = shortcuts[e.key];
      if (!token || !allowedTokensForSelection().has(token)) return;
      e.preventDefault();
      applyDragToken(token);
    });
  }
  initDragAreaFill();
  document.querySelectorAll("td.tally-cell[data-tally-band]").forEach(function (cell) {
    cell.addEventListener("click", function () {
      if (!cell.classList.contains("tally-short") && !cell.classList.contains("tally-over")) {
        return;
      }
      postChange({
        tally_select: {
          band: cell.dataset.tallyBand,
          date: cell.dataset.tallyDate,
        },
      });
    });
  });
  recomputeTallies(true);
  recomputeRowContractHours();
  recomputeRowShiftStats();
  const focusFit = """
        + focus_fit_flag
        + """;
  function fullscreenViewportBox() {
    if (window.__labFsViewport && window.__labFsViewport.w > 0 && window.__labFsViewport.h > 0) {
      return window.__labFsViewport;
    }
    const frame = window.frameElement;
    if (frame && frame.clientWidth > 0 && frame.clientHeight > 0) {
      return { w: frame.clientWidth, h: frame.clientHeight };
    }
    return {
      w: window.innerWidth || document.documentElement.clientWidth || 0,
      h: window.innerHeight || document.documentElement.clientHeight || 0,
    };
  }
  function measureTableNaturalSize(table) {
    table.style.transform = "none";
    table.style.margin = "0";
    const tbody = table.querySelector("tbody");
    const thead = table.querySelector("thead");
    const tfoot = table.querySelector("tfoot");
    let natH = 0;
    if (thead) natH += thead.offsetHeight;
    if (tbody) natH += tbody.offsetHeight;
    if (tfoot) natH += tfoot.offsetHeight;
    table.querySelectorAll("tbody tr").forEach(function (row) {
      natH = Math.max(natH, row.offsetTop + row.offsetHeight + (thead ? thead.offsetHeight : 0));
    });
    const natW = Math.max(
      table.offsetWidth || 0,
      table.scrollWidth || 0,
      table.getBoundingClientRect().width || 0
    );
    natH = Math.max(
      natH,
      table.offsetHeight || 0,
      table.scrollHeight || 0,
      table.getBoundingClientRect().height || 0
    );
    const rowCount = table.querySelectorAll("tbody tr").length;
    return { natW: natW, natH: natH, rowCount: rowCount };
  }
  function fitFocusGridToViewport() {
    const wrap = document.getElementById("lab-schedule-wrap");
    if (!wrap || !wrap.classList.contains("lab-schedule-wrap--focus-fit")) return;
    const scaler = wrap.querySelector(".lab-focus-scaler") || wrap;
    const table = wrap.querySelector(".lab-schedule-grid");
    if (!table) return;
    const vp = fullscreenViewportBox();
    const availW = vp.w;
    const availH = vp.h;
    if (availW <= 0 || availH <= 0) return;
    wrap.style.width = availW + "px";
    wrap.style.height = availH + "px";
    scaler.style.width = availW + "px";
    scaler.style.height = availH + "px";
    const measured = measureTableNaturalSize(table);
    const natW = measured.natW;
    const natH = measured.natH;
    if (natW <= 0 || natH <= 0) return;
    let zoomPct = 100;
    let stretch = true;
    try {
      zoomPct = parseInt(sessionStorage.getItem("labFsZoom") || "100", 10) || 100;
      stretch = sessionStorage.getItem("labFsStretch") !== "0";
    } catch (_) {}
    const zoom = Math.max(0.5, Math.min(2.0, zoomPct / 100));
    const scaleX = (availW / natW) * zoom;
    const scaleY = (availH / natH) * zoom;
    table.style.transformOrigin = "top left";
    if (stretch) {
      table.style.transform = "scale(" + scaleX + "," + scaleY + ")";
    } else {
      const scale = Math.min(scaleX, scaleY);
      const scaledW = natW * scale;
      const scaledH = natH * scale;
      const offsetX = Math.max(0, (availW - scaledW) / 2);
      const offsetY = Math.max(0, (availH - scaledH) / 2);
      table.style.transform = "translate(" + offsetX + "px," + offsetY + "px) scale(" + scale + ")";
    }
  }
  window.fitFocusGridToViewport = fitFocusGridToViewport;
  window.addEventListener("message", function (event) {
    if (!event.data || event.data.type !== "lab-fs-viewport") return;
    window.__labFsViewport = {
      w: Number(event.data.width) || 0,
      h: Number(event.data.height) || 0,
    };
    fitFocusGridToViewport();
  });
  function lockFullscreenViewport() {
    document.documentElement.style.overflow = "hidden";
    document.documentElement.style.height = "100%";
    document.body.style.overflow = "hidden";
    document.body.style.touchAction = "none";
    document.body.style.overscrollBehavior = "none";
    function blockScroll(ev) {
      if (ev.target && ev.target.closest && ev.target.closest(".lab-fullscreen-toolbar")) return;
      if (ev.target && ev.target.closest && ev.target.closest(".lab-drag-fill-palette")) return;
      ev.preventDefault();
    }
    document.addEventListener("wheel", blockScroll, { passive: false, capture: true });
    document.addEventListener("touchmove", blockScroll, { passive: false, capture: true });
    document.addEventListener("scroll", blockScroll, { passive: false, capture: true });
  }
  function initFullscreenControls() {
    const zoomInput = document.getElementById("lab-fs-zoom");
    const zoomVal = document.getElementById("lab-fs-zoom-val");
    const stretchInput = document.getElementById("lab-fs-stretch");
    if (!zoomInput || !stretchInput) return;
    try {
      const savedZoom = sessionStorage.getItem("labFsZoom");
      if (savedZoom) {
        zoomInput.value = savedZoom;
        if (zoomVal) zoomVal.textContent = savedZoom + "%";
      }
      stretchInput.checked = sessionStorage.getItem("labFsStretch") !== "0";
    } catch (_) {}
    function persistAndFit() {
      try {
        sessionStorage.setItem("labFsZoom", String(zoomInput.value || "100"));
        sessionStorage.setItem("labFsStretch", stretchInput.checked ? "1" : "0");
      } catch (_) {}
      if (zoomVal) zoomVal.textContent = (zoomInput.value || "100") + "%";
      fitFocusGridToViewport();
    }
    zoomInput.addEventListener("input", persistAndFit);
    stretchInput.addEventListener("change", persistAndFit);
  }
  if (focusFit === true || focusFit === "true") {
    lockFullscreenViewport();
    initFullscreenControls();
    fitFocusGridToViewport();
    window.addEventListener("resize", fitFocusGridToViewport);
    requestAnimationFrame(function () {
      fitFocusGridToViewport();
      requestAnimationFrame(fitFocusGridToViewport);
    });
    setTimeout(fitFocusGridToViewport, 40);
    setTimeout(fitFocusGridToViewport, 180);
    setTimeout(fitFocusGridToViewport, 500);
    setTimeout(fitFocusGridToViewport, 1200);
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(fitFocusGridToViewport);
    }
    if (typeof ResizeObserver !== "undefined") {
      const focusFitObserver = new ResizeObserver(function () {
        fitFocusGridToViewport();
      });
      focusFitObserver.observe(document.documentElement);
      if (document.body) focusFitObserver.observe(document.body);
    }
  }
  function notifyReady() {
    if (window.Streamlit && window.Streamlit.setComponentReady) {
      window.Streamlit.setComponentReady();
    }
  }
  notifyReady();
  window.addEventListener("load", notifyReady);
})();
</script>
"""
    )


def _render_master_schedule_shift_grid(
    matrix: pd.DataFrame,
    view_dates: List[date],
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
    locked_cells: Optional[Set[Tuple[str, date]]] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    focus_fit: bool = False,
    tally_matrix: Optional[pd.DataFrame] = None,
    role_suffix: Optional[str] = None,
    ops_metrics_json: Optional[str] = None,
    health_focus_date: Optional[date] = None,
) -> Optional[Dict[str, object]]:
    return _render_master_schedule_shift_grid_impl(
        matrix,
        view_dates,
        period_id=period_id,
        view_chunk_index=view_chunk_index,
        staging_revision=staging_revision,
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
        role_suffix=role_suffix,
        ops_metrics_json=ops_metrics_json,
        health_focus_date=health_focus_date,
        build_grid_html=_build_master_schedule_grid_html,
    )


def _render_shift_legend() -> None:
    chips: List[str] = []
    for code in ("D", "E", "N"):
        style = SHIFT_CELL_STYLES[code]
        chips.append(
            f'<span class="lab-legend-chip">'
            f'<span class="lab-legend-swatch" style="background:{style["bg"]};color:{style["fg"]};">{code}</span>'
            f"{html_lib.escape(style['label'])}</span>"
        )
    for code in (OFF_CODE_VACATION, OFF_CODE_SICK):
        style = SHIFT_CELL_STYLES[code]
        chips.append(
            f'<span class="lab-legend-chip">'
            f'<span class="lab-legend-swatch" style="background:{style["bg"]};color:{style["fg"]};">{code}</span>'
            f"{html_lib.escape(style['label'])}</span>"
        )
    off = SHIFT_CELL_STYLES["."]
    chips.append(
        f'<span class="lab-legend-chip">'
        f'<span class="lab-legend-swatch" style="background:{off["bg"]};color:{off["fg"]};border:1px solid #e2e8f0;">—</span>'
        f"Off / unscheduled</span>"
    )
    st.markdown(
        "<div style='margin:8px 0 10px;line-height:1.8;'>" + "".join(chips)
        + '<span class="lab-legend-chip">'
        + '<span class="lab-legend-swatch" style="background:#ffffff;color:#16a34a;border:2px solid #16a34a;">🔒</span>'
        + "Locked week (right-click any day) — locked cells are skipped when drag-filling</span>"
        + "</div>",
        unsafe_allow_html=True,
    )


@dataclass(frozen=True)
class TenantPeriod:
    id: str
    name: str
    period_start: date
    week_count: int
    period_end_inclusive: date


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _daterange(start: date, end_inclusive: date) -> Iterable[date]:
    d = start
    while d <= end_inclusive:
        yield d
        d += timedelta(days=1)


def _schedule_view_offset_key(period_id: str) -> str:
    """Legacy sliding-week offset (pre–chunk navigation)."""

    return f"schedule_view_week_offset_{period_id}"


def _schedule_view_chunk_key(period_id: str) -> str:
    return f"schedule_view_chunk_{period_id}"


def _schedule_view_chunk_index(period: TenantPeriod, *, max_chunk_index: int) -> int:
    chunk_key = _schedule_view_chunk_key(period.id)
    if chunk_key not in st.session_state:
        legacy = int(st.session_state.get(_schedule_view_offset_key(period.id), 0))
        if legacy >= SCHEDULE_GRID_VIEW_WEEKS:
            st.session_state[chunk_key] = min(
                legacy // SCHEDULE_GRID_VIEW_WEEKS,
                max_chunk_index,
            )
        else:
            st.session_state[chunk_key] = 0
    chunk_index = int(st.session_state.get(chunk_key, 0))
    chunk_index = max(0, min(chunk_index, max_chunk_index))
    st.session_state[chunk_key] = chunk_index
    return chunk_index


def _schedule_view_chunk_labels(period: TenantPeriod) -> List[str]:
    """Human labels for each grid chunk (e.g. Weeks 1–8, Weeks 9–16)."""

    if period.week_count <= SCHEDULE_GRID_VIEW_WEEKS:
        return []
    chunk_count = (period.week_count + SCHEDULE_GRID_VIEW_WEEKS - 1) // SCHEDULE_GRID_VIEW_WEEKS
    labels: List[str] = []
    for chunk_index in range(chunk_count):
        start_week = chunk_index * SCHEDULE_GRID_VIEW_WEEKS + 1
        end_week = min((chunk_index + 1) * SCHEDULE_GRID_VIEW_WEEKS, period.week_count)
        labels.append(f"Weeks {start_week}–{end_week}")
    return labels


def _resolve_schedule_view_dates(
    all_dates: List[date],
    *,
    period: TenantPeriod,
) -> Tuple[List[date], int, int, List[str]]:
    """Fixed UI chunks for long periods; solver and exports always use the full period."""

    chunk_labels = _schedule_view_chunk_labels(period)
    if not chunk_labels:
        return all_dates, 0, 0, chunk_labels
    max_chunk_index = len(chunk_labels) - 1
    chunk_index = _schedule_view_chunk_index(period, max_chunk_index=max_chunk_index)
    start_idx = chunk_index * SCHEDULE_GRID_VIEW_WEEKS * 7
    end_idx = start_idx + (SCHEDULE_GRID_VIEW_WEEKS * 7)
    return all_dates[start_idx:end_idx], chunk_index, max_chunk_index, chunk_labels


def _schedule_grid_static_columns() -> Tuple[str, ...]:
    return ("Employee", "employee_id", "fte", "contract_line_type")


def _slice_schedule_frame_for_view(
    frame: pd.DataFrame,
    view_dates: List[date],
) -> pd.DataFrame:
    static_cols = [col for col in _schedule_grid_static_columns() if col in frame.columns]
    day_cols = [day.isoformat() for day in view_dates if day.isoformat() in frame.columns]
    return frame[static_cols + day_cols]


def _display_shift_cell(value: object) -> str:
    """Map a draft cell value to the token shown in the schedule grid."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return EMPTY_SHIFT_DISPLAY
    raw = str(value).strip()
    if not raw or raw in {EMPTY_SHIFT_DISPLAY, "—", "-", "."}:
        return EMPTY_SHIFT_DISPLAY
    if raw.upper() == "FORCED_CLINICAL_OT":
        return raw
    normalized = _normalize_shift_cell(value)
    if not normalized:
        return EMPTY_SHIFT_DISPLAY
    return normalized


def _schedule_frame_needs_display_resync(frame: pd.DataFrame, dates: List[date]) -> bool:
    """True when a cached draft no longer matches the active period date columns."""
    if frame is None or frame.empty:
        return True
    expected = {day.isoformat() for day in dates}
    present = {str(col) for col in frame.columns if is_schedule_date_column(str(col), dates)}
    return present != expected


def _sanitize_schedule_dataframe(frame: pd.DataFrame, dates: List[date]) -> pd.DataFrame:
    """Align draft columns to the active period and normalize shift display tokens."""
    if frame is None or frame.empty:
        return frame

    date_keys = [day.isoformat() for day in dates]
    static_cols = list(_schedule_grid_static_columns())
    static_col_set = set(static_cols)
    sanitized = frame.copy()

    for col in static_cols:
        if col not in sanitized.columns:
            sanitized[col] = 1.0 if col == "fte" else ""

    for key in date_keys:
        if key not in sanitized.columns:
            sanitized[key] = EMPTY_SHIFT_DISPLAY

    for row_idx in sanitized.index:
        row = sanitized.loc[row_idx]
        is_tally = False
        if "employee_id" in sanitized.columns:
            is_tally = is_daily_tally_employee_id(row.get("employee_id"))
        if not is_tally and "Employee" in sanitized.columns:
            is_tally = str(row.get("Employee", "")) in ALL_DAILY_TALLY_ROW_NAMES

        for key in date_keys:
            if is_tally:
                cell = sanitized.at[row_idx, key]
                if cell is None or (isinstance(cell, float) and pd.isna(cell)):
                    sanitized.at[row_idx, key] = ""
                continue
            sanitized.at[row_idx, key] = _display_shift_cell(sanitized.at[row_idx, key])

    stale_date_cols: List[str] = []
    for col in sanitized.columns:
        col_key = str(col)
        if col_key in date_keys or col_key in static_col_set:
            continue
        try:
            date.fromisoformat(col_key)
        except (TypeError, ValueError):
            continue
        stale_date_cols.append(col_key)
    if stale_date_cols:
        sanitized = sanitized.drop(columns=stale_date_cols)

    ordered = [col for col in static_cols if col in sanitized.columns]
    ordered.extend(date_keys)
    extras = [col for col in sanitized.columns if col not in ordered]
    return sanitized[ordered + extras]


def _migrate_shift_qualification_triggers(conn: sqlite3.Connection) -> None:
    """Require at least one listed qualification per shift (OR), not all rows (AND)."""

    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trg_shift_assignments_required_qualifications;
        DROP TRIGGER IF EXISTS trg_shift_assignments_required_qualifications_update;

        CREATE TRIGGER trg_shift_assignments_required_qualifications
        BEFORE INSERT ON shift_assignments
        FOR EACH ROW
        BEGIN
          SELECT
            CASE
              WHEN EXISTS (
                SELECT 1
                FROM shift_template_qualifications stq
                WHERE stq.tenant_id = NEW.tenant_id
                  AND stq.shift_template_id = NEW.shift_template_id
              )
              AND NOT EXISTS (
                SELECT 1
                FROM shift_template_qualifications stq
                INNER JOIN employee_qualifications eq
                  ON eq.tenant_id = stq.tenant_id
                 AND eq.employee_id = NEW.employee_id
                 AND eq.qualification_id = stq.qualification_id
                 AND (eq.expires_on IS NULL OR eq.expires_on >= NEW.assignment_date)
                 AND (eq.awarded_on IS NULL OR eq.awarded_on <= NEW.assignment_date)
                WHERE stq.tenant_id = NEW.tenant_id
                  AND stq.shift_template_id = NEW.shift_template_id
              )
              THEN RAISE(ABORT, 'employee lacks required qualification for this shift')
            END;
        END;

        CREATE TRIGGER trg_shift_assignments_required_qualifications_update
        BEFORE UPDATE ON shift_assignments
        FOR EACH ROW
        BEGIN
          SELECT
            CASE
              WHEN EXISTS (
                SELECT 1
                FROM shift_template_qualifications stq
                WHERE stq.tenant_id = NEW.tenant_id
                  AND stq.shift_template_id = NEW.shift_template_id
              )
              AND NOT EXISTS (
                SELECT 1
                FROM shift_template_qualifications stq
                INNER JOIN employee_qualifications eq
                  ON eq.tenant_id = stq.tenant_id
                 AND eq.employee_id = NEW.employee_id
                 AND eq.qualification_id = stq.qualification_id
                 AND (eq.expires_on IS NULL OR eq.expires_on >= NEW.assignment_date)
                 AND (eq.awarded_on IS NULL OR eq.awarded_on <= NEW.assignment_date)
                WHERE stq.tenant_id = NEW.tenant_id
                  AND stq.shift_template_id = NEW.shift_template_id
              )
              THEN RAISE(ABORT, 'employee lacks required qualification for this shift')
            END;
        END;
        """
    )


def _remove_specimen_shift_type(conn: sqlite3.Connection, tenant_id: str) -> None:
    cur = conn.cursor()
    specimen = cur.execute(
        """
        SELECT id
        FROM shift_templates
        WHERE tenant_id = ?
          AND (id = 'shift-specimen' OR UPPER(code) = 'SPECIMEN')
        """,
        (tenant_id,),
    ).fetchone()
    if specimen is None:
        return

    specimen_id = specimen[0]
    morning = cur.execute(
        """
        SELECT id
        FROM shift_templates
        WHERE tenant_id = ?
          AND (id = 'shift-morning' OR UPPER(code) = 'MORNING')
        """,
        (tenant_id,),
    ).fetchone()
    if morning is not None:
        morning_id = morning[0]
        mla = cur.execute(
            """
            SELECT id FROM qualifications
            WHERE tenant_id = ? AND code = 'MLA'
            """,
            (tenant_id,),
        ).fetchone()
        if mla is not None:
            cur.execute(
                """
                INSERT OR IGNORE INTO shift_template_qualifications (
                  tenant_id, shift_template_id, qualification_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (tenant_id, morning_id, mla[0], _utc_now_iso()),
            )
        cur.execute(
            """
            UPDATE shift_assignments
            SET shift_template_id = ?
            WHERE tenant_id = ? AND shift_template_id = ?
            """,
            (morning_id, tenant_id, specimen_id),
        )

    cur.execute(
        """
        DELETE FROM shift_assignments
        WHERE tenant_id = ? AND shift_template_id = ?
        """,
        (tenant_id, specimen_id),
    )

    cur.execute(
        """
        DELETE FROM shift_template_qualifications
        WHERE tenant_id = ? AND shift_template_id = ?
        """,
        (tenant_id, specimen_id),
    )
    cur.execute(
        "DELETE FROM shift_templates WHERE tenant_id = ? AND id = ?",
        (tenant_id, specimen_id),
    )
    cur.execute(
        """
        UPDATE schedule_audit_logs
        SET previous_shift_code = 'M'
        WHERE tenant_id = ? AND previous_shift_code = 'S'
        """,
        (tenant_id,),
    )
    cur.execute(
        """
        UPDATE schedule_audit_logs
        SET new_shift_code = 'M'
        WHERE tenant_id = ? AND new_shift_code = 'S'
        """,
        (tenant_id,),
    )


def _apply_db_migrations(conn: sqlite3.Connection, tenant_id: str) -> None:
    cur = conn.cursor()

    cols = {row[1] for row in cur.execute("PRAGMA table_info(employees)")}
    if "base_hourly_rate" not in cols:
        cur.execute(
            "ALTER TABLE employees ADD COLUMN base_hourly_rate REAL NOT NULL DEFAULT 40.0"
        )
    if "seniority_hours" not in cols:
        cur.execute(
            "ALTER TABLE employees ADD COLUMN seniority_hours REAL NOT NULL DEFAULT 0.0"
        )
    ensure_contract_line_schema(conn)

    cur.execute(
        """
        UPDATE employees
        SET seniority_hours = CASE id
          WHEN 'emp-b1' THEN 9200.0
          WHEN 'emp-a1' THEN 6800.0
          WHEN 'emp-c1' THEN 2400.0
          ELSE seniority_hours
        END
        WHERE tenant_id = ?
          AND id IN ('emp-a1', 'emp-b1', 'emp-c1')
        """,
        (tenant_id,),
    )
    cur.execute(
        """
        UPDATE employees
        SET seniority_hours = ROUND(
          MAX(0.0, (julianday('now') - julianday(hire_date)) / 365.25) * 2080.0,
          1
        )
        WHERE tenant_id = ?
          AND (seniority_hours IS NULL OR seniority_hours = 0.0)
        """,
        (tenant_id,),
    )

    cur.execute(
        """
        UPDATE qualifications
        SET code = 'MLA',
            display_name = 'Medical Laboratory Assistant',
            description = 'Specimen processing and front-end laboratory support.'
        WHERE tenant_id = ? AND code IN ('LA', 'Lab Assistant')
        """,
        (tenant_id,),
    )

    cur.execute(
        """
        UPDATE qualifications
        SET display_name = 'Medical Laboratory Technologist',
            description = 'Performs diagnostic laboratory testing and analysis.'
        WHERE tenant_id = ? AND code = 'MLT'
        """,
        (tenant_id,),
    )

    mls = cur.execute(
        "SELECT id FROM qualifications WHERE tenant_id = ? AND code = 'MLS'",
        (tenant_id,),
    ).fetchone()
    mlt = cur.execute(
        "SELECT id FROM qualifications WHERE tenant_id = ? AND code = 'MLT'",
        (tenant_id,),
    ).fetchone()

    if mls and mlt:
        mls_id, mlt_id = mls[0], mlt[0]
        cur.execute(
            """
            INSERT OR IGNORE INTO employee_qualifications (
              tenant_id, employee_id, qualification_id, awarded_on, expires_on, created_at
            )
            SELECT tenant_id, employee_id, ?, awarded_on, expires_on, created_at
            FROM employee_qualifications
            WHERE tenant_id = ? AND qualification_id = ?
            """,
            (mlt_id, tenant_id, mls_id),
        )
        cur.execute(
            "DELETE FROM employee_qualifications WHERE tenant_id = ? AND qualification_id = ?",
            (tenant_id, mls_id),
        )
        cur.execute(
            """
            UPDATE shift_template_qualifications
            SET qualification_id = ?
            WHERE tenant_id = ? AND qualification_id = ?
            """,
            (mlt_id, tenant_id, mls_id),
        )
        cur.execute("DELETE FROM qualifications WHERE id = ?", (mls_id,))
    elif mls and not mlt:
        cur.execute(
            """
            UPDATE qualifications
            SET code = 'MLT',
                display_name = 'Medical Laboratory Technologist',
                description = 'Performs diagnostic laboratory testing and analysis.'
            WHERE id = ?
            """,
            (mls[0],),
        )

    cur.execute(
        """
        UPDATE employees
        SET base_hourly_rate = ?
        WHERE tenant_id = ?
          AND id IN (
            SELECT eq.employee_id
            FROM employee_qualifications eq
            JOIN qualifications q
              ON q.id = eq.qualification_id AND q.tenant_id = eq.tenant_id
            WHERE eq.tenant_id = ? AND q.code = 'MLA'
          )
        """,
        (DEFAULT_HOURLY_RATE_MLA, tenant_id, tenant_id),
    )
    cur.execute(
        """
        UPDATE employees
        SET base_hourly_rate = ?
        WHERE tenant_id = ?
          AND id IN (
            SELECT eq.employee_id
            FROM employee_qualifications eq
            JOIN qualifications q
              ON q.id = eq.qualification_id AND q.tenant_id = eq.tenant_id
            WHERE eq.tenant_id = ? AND q.code = 'MLT'
          )
        """,
        (DEFAULT_HOURLY_RATE_MLT, tenant_id, tenant_id),
    )

    _remove_specimen_shift_type(conn, tenant_id)
    conn.commit()


def _apply_global_db_migrations(conn: sqlite3.Connection) -> None:
    from lab_scheduler.staff.lifecycle import ensure_staff_lifecycle_schema

    ensure_sentry_schema(conn)
    ensure_staff_lifecycle_schema(conn)
    ensure_seniority_cba_schema(conn)
    ensure_contract_line_schema(conn)
    ensure_shift_cell_locks_schema(conn)
    _migrate_shift_qualification_triggers(conn)
    conn.commit()


def _seed_availability_if_empty(conn: sqlite3.Connection, tenant_id: str) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM availability_exceptions WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()[0]
    if count > 0:
        return

    # Legacy demo availability rows reference emp-a1/b1/c1 from seed_demo_lab.sql.
    # After a Portage hard reset those employees are gone — skip re-seeding.
    demo_seed_ids = ("emp-a1", "emp-b1", "emp-c1")
    placeholders = ",".join("?" for _ in demo_seed_ids)
    present = conn.execute(
        f"""
        SELECT COUNT(*) FROM employees
        WHERE tenant_id = ? AND id IN ({placeholders})
        """,
        (tenant_id, *demo_seed_ids),
    ).fetchone()[0]
    if present != len(demo_seed_ids):
        return

    conn.executescript((SQL_DIR / "seed_availability_demo.sql").read_text(encoding="utf-8"))


def _configure_sqlite_connection(conn: sqlite3.Connection, db_path: Path) -> None:
    """Apply the standard runtime PRAGMAs for every app connection.

    - ``foreign_keys=ON`` enforces referential integrity (per-connection).
    - ``journal_mode=WAL`` on local disks for concurrent reads/writes.
    - ``journal_mode=DELETE`` on cloud-synced paths (OneDrive corrupts WAL sidecars).
    - ``busy_timeout`` waits instead of failing immediately on lock contention.
    """

    conn.execute("PRAGMA foreign_keys = ON;")
    if _sqlite_path_is_cloud_synced(db_path):
        conn.execute("PRAGMA journal_mode = DELETE;")
    else:
        conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")


def _connect_app_db() -> sqlite3.Connection:
    """Open the app database (Streamlit may touch connections across threads)."""

    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _configure_sqlite_connection(conn, DB_PATH)
    return conn


def _ensure_demo_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _maybe_restore_from_project_backup(db_path)
    if db_path.is_file() and not _sqlite_integrity_ok(db_path):
        quarantined = _quarantine_corrupt_db(db_path)
        st.session_state["lab_db_quarantine_notice"] = (
            f"Database file was corrupted and moved to `{quarantined.name}`. "
            "A fresh database was created in local app storage. "
            "Re-save any schedules you still need from backups."
        )
    conn = sqlite3.connect(str(db_path))
    try:
        _configure_sqlite_connection(conn, db_path)
        cur = conn.cursor()

        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              slug TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )

        for fname in (
            "03_lab_core_tables.sql",
            "04_schedule_periods_and_assignments.sql",
            "06_availability_exceptions.sql",
            "07_schedule_audit_logs.sql",
            "08_tenant_accounts.sql",
            "11_sys_sentry_logs.sql",
            "12_staff_lifecycle.sql",
            "13_seniority_cba.sql",
            "17_shift_cell_locks.sql",
        ):
            cur.executescript((SQL_DIR / fname).read_text(encoding="utf-8"))

        tenant_present = (
            cur.execute("SELECT 1 FROM tenants WHERE id = ?", (NORTHSTAR_TENANT_ID,)).fetchone()
            is not None
        )
        if not tenant_present:
            cur.executescript((SQL_DIR / "seed_demo_lab.sql").read_text(encoding="utf-8"))
            cur.executescript((SQL_DIR / "seed_availability_demo.sql").read_text(encoding="utf-8"))

        southbridge_present = (
            cur.execute("SELECT 1 FROM tenants WHERE id = ?", (SOUTHBRIDGE_TENANT_ID,)).fetchone()
            is not None
        )
        if not southbridge_present:
            cur.executescript((SQL_DIR / "seed_southbridge_tenant.sql").read_text(encoding="utf-8"))

        cur.executescript(
            (SQL_DIR / "15_twelve_hour_topup_template.sql").read_text(encoding="utf-8")
        )
        cur.executescript(
            (SQL_DIR / "16_tenant_configurations.sql").read_text(encoding="utf-8")
        )
        cur.executescript(
            (SQL_DIR / "18_business_prospects.sql").read_text(encoding="utf-8")
        )
        cur.executescript(
            (SQL_DIR / "19_business_inbound.sql").read_text(encoding="utf-8")
        )
        ensure_tenant_configuration_schema(conn)

        seed_default_accounts(conn)
        ensure_demo_account_credentials(conn)

        ensure_billing_schema(conn)
        seed_default_billing_state(
            conn,
            northstar_tenant_id=NORTHSTAR_TENANT_ID,
            southbridge_tenant_id=SOUTHBRIDGE_TENANT_ID,
        )

        tenant_ids = [row[0] for row in cur.execute("SELECT id FROM tenants").fetchall()]
        _apply_global_db_migrations(conn)
        for tenant_id in tenant_ids:
            _apply_db_migrations(conn, tenant_id)
        _seed_availability_if_empty(conn, NORTHSTAR_TENANT_ID)

        conn.commit()
    finally:
        conn.close()


def _fetch_availability_exceptions(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    period_start: date,
    period_end: date,
) -> List[AvailabilityException]:
    rows = conn.execute(
        """
        SELECT id, tenant_id, employee_id, start_date, end_date, reason, status
        FROM availability_exceptions
        WHERE tenant_id = ?
          AND start_date <= ?
          AND end_date >= ?
        ORDER BY start_date, employee_id
        """,
        (tenant_id, period_end.isoformat(), period_start.isoformat()),
    ).fetchall()
    return [
        AvailabilityException(
            id=r[0],
            tenant_id=r[1],
            employee_id=r[2],
            start_date=date.fromisoformat(r[3]),
            end_date=date.fromisoformat(r[4]),
            reason=r[5],
            status=r[6],
        )
        for r in rows
    ]


def _availability_context(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
) -> Tuple[Dict[str, Dict[date, str]], Dict[str, Set[date]], Dict[str, float]]:
    exceptions = _fetch_availability_exceptions(
        conn,
        tenant_id,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
    )
    blocked_map = expand_blocked_dates(
        exceptions,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
    )
    blocked_sets = blocked_dates_by_employee(blocked_map)
    target_hours = compute_employee_target_hours(
        rules=rules,
        employees=employees,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        weeks_in_period=period.week_count,
        blocked_map=blocked_map,
    )
    return blocked_map, blocked_sets, target_hours


def _fte_contract_target_hours(
    *,
    rules: JurisdictionRules,
    period: TenantPeriod,
    employees: List[Dict],
    target_hours: Mapping[str, float],
    emp_quals: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, float]:
    """Period targets for grid/breakroom: payroll FTE with catalog override on vacant master lines."""

    from lab_scheduler.scheduling.contract_payroll import apply_catalog_targets_for_vacant_master_lines
    from lab_scheduler.scheduling.profiles import EmployeeProfile

    profiles = [
        EmployeeProfile(
            id=str(employee["id"]),
            full_name=str(employee.get("full_name") or ""),
            fte=float(employee.get("fte") or 1.0),
            qualification_ids=(emp_quals or {}).get(employee["id"], set()),
            seniority_hours=float(employee.get("seniority_hours") or 0.0),
            base_hourly_rate=float(employee.get("base_hourly_rate") or 40.0),
            contract_line_type=employee.get("contract_line_type"),
        )
        for employee in employees
    ]
    return apply_catalog_targets_for_vacant_master_lines(
        profiles,
        target_hours,
        rules=rules,
        weeks_in_period=period.week_count,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
    )


def _fetch_tenant_metadata(conn: sqlite3.Connection, tenant_id: str) -> TenantMetadata:
    row = conn.execute(
        "SELECT id, name, slug, status FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    if row is None:
        return TenantMetadata(id=tenant_id, name=tenant_id, slug=tenant_id, status="unknown")
    return TenantMetadata(id=row[0], name=row[1], slug=row[2], status=row[3])


def _load_tenant_jurisdiction(conn: sqlite3.Connection, *, tenant_id: str) -> str:
    stored = get_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key="jurisdiction",
        default=DEFAULT_JURISDICTION_NAME,
    )
    if stored in JURISDICTIONS:
        return str(stored)
    return DEFAULT_JURISDICTION_NAME


def _effective_period_and_employees(
    period: TenantPeriod,
    employees: List[Dict],
    gates: FeatureGates,
    *,
    manager_mode: bool = False,
) -> Tuple[TenantPeriod, List[Dict]]:
    if manager_mode or gates.is_premium or not gates.is_trial_tier:
        return period, employees
    trial_end = trial_period_end(period.period_start, gates)
    trial_period = TenantPeriod(
        id=period.id,
        name=period.name,
        period_start=period.period_start,
        week_count=TRIAL_MAX_WEEKS,
        period_end_inclusive=trial_end,
    )
    return trial_period, apply_employee_cap(employees, gates)




def _render_trial_limits_banner(
    gates: FeatureGates,
    *,
    total_employees: int,
) -> None:
    if not gates.is_trial_tier or gates.is_premium:
        return
    cap = gates.trial_employee_cap or TRIAL_MAX_EMPLOYEES
    weeks = gates.trial_week_cap or TRIAL_MAX_WEEKS
    st.info(
        f"Trial workspace: up to **{cap}** roster lines and **{weeks}** weeks. "
        f"Showing **{min(total_employees, cap)}** lines in this view."
    )


def _render_account_subscription_sidebar(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    tenant_slug: str,
    billing: object,
    gates: FeatureGates,
) -> None:
    st.sidebar.markdown("---")
    st.sidebar.markdown("**Subscription**")
    status = getattr(billing, "subscription_status", "unknown")
    tier = "Premium" if gates.is_premium else "Trial"
    st.sidebar.caption(f"Plan: **{tier}** · status `{status}`")
    if gates.is_premium:
        st.sidebar.caption("Premium exports and compliance audit are unlocked.")
        return
    if st.sidebar.button(
        f"Upgrade — {PREMIUM_PRICE_DISPLAY}",
        key=f"sidebar_upgrade_{tenant_id}",
        width="stretch",
    ):
        st.session_state[schedule_sess.billing_checkout_trigger_key(tenant_id)] = True
        st.rerun()




def _fetch_tenant_periods(conn: sqlite3.Connection, tenant_id: str) -> List[TenantPeriod]:
    rows = conn.execute(
        """
        SELECT id, name, period_start, week_count, period_end_inclusive
        FROM schedule_periods
        WHERE tenant_id = ?
        ORDER BY period_start DESC
        """,
        (tenant_id,),
    ).fetchall()

    return [
        TenantPeriod(
            id=r[0],
            name=r[1],
            period_start=date.fromisoformat(r[2]),
            week_count=int(r[3]),
            period_end_inclusive=date.fromisoformat(r[4]),
        )
        for r in rows
    ]


def _fetch_employees(conn: sqlite3.Connection, tenant_id: str) -> List[Dict]:
    rows = conn.execute(
        """
        SELECT id, employee_code, first_name, last_name, fte, base_hourly_rate, seniority_hours,
               contract_line_type
        FROM employees
        WHERE tenant_id = ? AND is_active = 1
        ORDER BY last_name, first_name
        """,
        (tenant_id,),
    ).fetchall()

    return [
        {
            "id": r[0],
            "code": r[1] or "",
            "first_name": r[2],
            "last_name": r[3],
            "full_name": f"{r[2]} {r[3]}",
            "fte": float(r[4]),
            "base_hourly_rate": float(r[5]),
            "seniority_hours": float(r[6]),
            "contract_line_type": r[7],
        }
        for r in rows
    ]


def _fetch_employee_hourly_rates(conn: sqlite3.Connection, tenant_id: str) -> Dict[str, float]:
    rows = conn.execute(
        """
        SELECT id, base_hourly_rate
        FROM employees
        WHERE tenant_id = ? AND is_active = 1
        """,
        (tenant_id,),
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def _default_rate_for_qualification_code(code: str) -> float:
    if code == "MLA":
        return DEFAULT_HOURLY_RATE_MLA
    return DEFAULT_HOURLY_RATE_MLT


def _fetch_shift_templates(conn: sqlite3.Connection, tenant_id: str) -> Dict[str, Dict]:
    rows = conn.execute(
        """
        SELECT id, code, name, start_time, end_time, duration_minutes, crosses_midnight
        FROM shift_templates
        WHERE tenant_id = ? AND is_active = 1 AND UPPER(code) != 'SPECIMEN'
        ORDER BY code
        """,
        (tenant_id,),
    ).fetchall()

    out: Dict[str, Dict] = {}
    for r in rows:
        shift_id = r[0]
        code, name = r[1], r[2]
        out[shift_id] = {
            "id": shift_id,
            "code": code,
            "short": shift_code_to_display_token(code)
            or (code[:1] if code else shift_id[:1]).upper(),
            "name": name,
            "start_time": r[3],
            "end_time": r[4],
            "duration_minutes": int(r[5]),
            "crosses_midnight": bool(r[6]),
        }
    return out


def _fetch_qualification_id_to_code(conn: sqlite3.Connection, tenant_id: str) -> Dict[str, str]:
    rows = conn.execute(
        """
        SELECT id, code
        FROM qualifications
        WHERE tenant_id = ? AND is_active = 1
        """,
        (tenant_id,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}












def _prioritize_fill_key(period_id: str) -> str:
    return f"prioritize_fill_{period_id}"


def _set_prioritize_fill(
    period_id: str,
    *,
    employee_id: str,
    employee_name: str,
    role: str,
) -> None:
    st.session_state[_prioritize_fill_key(period_id)] = {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "role": role,
    }


def _focused_employee_key(period_id: str) -> str:
    return f"workspace_focus_employee_{period_id}"


def _workspace_publish_notice_key(period_id: str) -> str:
    return f"workspace_publish_notice_{period_id}"


def _get_prioritize_fill(period_id: str) -> Optional[Dict[str, str]]:
    payload = st.session_state.get(_prioritize_fill_key(period_id))
    if not isinstance(payload, dict):
        return None
    return payload


def _staff_deactivate_pending_key(tenant_id: str) -> str:
    return f"staff_deactivate_pending_{tenant_id}"


def _roster_import_preview_key(tenant_id: str) -> str:
    return f"roster_import_preview_{tenant_id}"


def _roster_row_seniority_key(tenant_id: str, employee_id: str) -> str:
    return f"roster_seniority_{tenant_id}_{employee_id}"


def _roster_row_contract_key(tenant_id: str, employee_id: str) -> str:
    return f"roster_contract_{tenant_id}_{employee_id}"


def _roster_row_hours_key(tenant_id: str, employee_id: str) -> str:
    return f"roster_hours_{tenant_id}_{employee_id}"


def _roster_pending_commit_key(tenant_id: str) -> str:
    return f"roster_pending_commit_{tenant_id}"


def _roster_row_widget_values(
    member: Dict,
    *,
    tenant_id: str,
    weekly_hour_options: Sequence[float],
) -> Tuple[float, str, float]:
    seniority_key = _roster_row_seniority_key(tenant_id, member["id"])
    contract_key = _roster_row_contract_key(tenant_id, member["id"])
    hours_key = _roster_row_hours_key(tenant_id, member["id"])

    seniority = float(st.session_state.get(seniority_key, member["seniority_hours"]))
    contract_line = str(
        st.session_state.get(contract_key, member["contract_line_type"])
    )
    target_hours = float(
        st.session_state.get(hours_key, member["target_weekly_hours"])
    )
    if target_hours not in weekly_hour_options:
        target_hours = member["target_weekly_hours"]
    return seniority, contract_line, target_hours


def _roster_row_is_dirty(
    member: Dict,
    *,
    tenant_id: str,
    weekly_hour_options: Sequence[float],
) -> bool:
    seniority, contract_line, target_hours = _roster_row_widget_values(
        member,
        tenant_id=tenant_id,
        weekly_hour_options=weekly_hour_options,
    )
    return (
        seniority != float(member["seniority_hours"])
        or contract_line != member["contract_line_type"]
        or target_hours != float(member["target_weekly_hours"])
    )


def _reset_roster_row_widgets(member: Dict, *, tenant_id: str) -> None:
    st.session_state[_roster_row_seniority_key(tenant_id, member["id"])] = float(
        member["seniority_hours"]
    )
    st.session_state[_roster_row_contract_key(tenant_id, member["id"])] = str(
        member["contract_line_type"]
    )
    st.session_state[_roster_row_hours_key(tenant_id, member["id"])] = float(
        member["target_weekly_hours"]
    )


def _clear_roster_row_widgets(tenant_id: str, employee_id: str) -> None:
    for key in (
        _roster_row_seniority_key(tenant_id, employee_id),
        _roster_row_contract_key(tenant_id, employee_id),
        _roster_row_hours_key(tenant_id, employee_id),
    ):
        st.session_state.pop(key, None)

def _serialize_deactivation_result(result: DeactivationResult) -> Dict[str, object]:
    return {
        "employee_id": result.employee_id,
        "employee_name": result.employee_name,
        "shifts_vacated_count": result.shifts_vacated_count,
        "audit_log_id": result.audit_log_id,
        "vacated_shifts": [
            {
                "shift_id": shift.shift_id,
                "schedule_period_id": shift.schedule_period_id,
                "shift_template_id": shift.shift_template_id,
                "assignment_date": shift.assignment_date.isoformat(),
                "vacated_from_employee_id": shift.vacated_from_employee_id,
                "vacated_from_employee_name": shift.vacated_from_employee_name,
                "system_note": shift.system_note,
            }
            for shift in result.vacated_shifts
        ],
    }


def _deserialize_vacated_shifts(payload: Sequence[Dict[str, object]]) -> List[VacatedShift]:
    return [
        VacatedShift(
            shift_id=str(item["shift_id"]),
            tenant_id=str(item.get("tenant_id", "")),
            schedule_period_id=str(item["schedule_period_id"]),
            shift_template_id=str(item["shift_template_id"]),
            assignment_date=date.fromisoformat(str(item["assignment_date"])),
            vacated_from_employee_id=str(item["vacated_from_employee_id"]),
            vacated_from_employee_name=str(item["vacated_from_employee_name"]),
            system_note=str(item.get("system_note", "")),
        )
        for item in payload
    ]




def _fetch_qualifications_by_code(conn: sqlite3.Connection, tenant_id: str) -> Dict[str, Dict]:
    rows = conn.execute(
        """
        SELECT id, code, display_name
        FROM qualifications
        WHERE tenant_id = ? AND is_active = 1
        ORDER BY code
        """,
        (tenant_id,),
    ).fetchall()
    return {
        r[1]: {"id": r[0], "code": r[1], "display_name": r[2]}
        for r in rows
    }


def _fetch_roster_rows(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
    period_start: date,
    period_end: date,
) -> List[Dict]:
    rows = conn.execute(
        """
        SELECT
          e.id,
          e.employee_code,
          e.first_name,
          e.last_name,
          e.fte,
          e.seniority_hours,
          e.hire_date,
          e.contract_line_type,
          GROUP_CONCAT(q.code, ', ') AS qual_codes
        FROM employees e
        LEFT JOIN employee_qualifications eq
          ON eq.tenant_id = e.tenant_id AND eq.employee_id = e.id
        LEFT JOIN qualifications q
          ON q.tenant_id = eq.tenant_id AND q.id = eq.qualification_id
        WHERE e.tenant_id = ? AND e.is_active = 1
        GROUP BY e.id
        ORDER BY e.last_name, e.first_name
        """,
        (tenant_id,),
    ).fetchall()

    blocked_map = expand_blocked_dates(
        _fetch_availability_exceptions(
            conn, tenant_id, period_start=period_start, period_end=period_end
        ),
        period_start=period_start,
        period_end=period_end,
    )

    standard_weekly_hours = rules.standard_hours_per_week_at_1_0_fte
    weekly_hour_options = bulk_target_weekly_hours_options(standard_weekly_hours)

    roster: List[Dict] = []
    for r in rows:
        fte = float(r[4])
        emp_id = r[0]
        seniority_hours = float(r[5] or 0.0)
        target = compute_employee_target_hours(
            rules=rules,
            employees=[{"id": emp_id, "fte": fte}],
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            blocked_map=blocked_map,
        )[emp_id]
        target_weekly_hours = min(
            weekly_hour_options,
            key=lambda hours: abs(hours - fte * standard_weekly_hours),
        )
        roster.append(
            {
                "id": r[0],
                "code": r[1] or "",
                "first_name": r[2],
                "last_name": r[3],
                "full_name": f"{r[2]} {r[3]}",
                "fte": fte,
                "seniority_hours": seniority_hours,
                "target_weekly_hours": target_weekly_hours,
                "hire_date": r[6],
                "contract_line_type": r[7] or "D/N",
                "qualifications": r[8] or "—",
                "target_hours_4wk": round(target, 1),
            }
        )
    return roster


def _fetch_existing_employee_records(
    conn: sqlite3.Connection,
    tenant_id: str,
) -> List[ExistingEmployeeRecord]:
    rows = conn.execute(
        """
        SELECT id, first_name, last_name
        FROM employees
        WHERE tenant_id = ?
        ORDER BY last_name, first_name
        """,
        (tenant_id,),
    ).fetchall()
    return [
        ExistingEmployeeRecord(
            id=row[0],
            full_name=f"{row[1]} {row[2]}".strip(),
            first_name=row[1],
            last_name=row[2],
        )
        for row in rows
    ]


def _render_roster_import_panel(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    rules: JurisdictionRules,
    qual_catalog: Dict[str, Dict],
) -> None:
    available_quals = [code for code in QUALIFICATION_CODES if code in qual_catalog]
    if not available_quals:
        return

    preview_key = _roster_import_preview_key(tenant_id)
    st.caption(
        "Upload a roster file with columns **Full Name**, **Role (MLT/MLA)**, "
        "**Seniority Hours**, and **Target Weekly Hours**. "
        "Review the mapped preview before committing."
    )

    uploaded = st.file_uploader(
        "Roster file",
        type=["csv", "xlsx", "xls"],
        key=f"roster_import_upload_{tenant_id}",
        help="Accepted formats: .csv, .xlsx",
    )

    if uploaded is not None:
        try:
            frame = parse_roster_file(content=uploaded.getvalue(), filename=uploaded.name)
            preview = build_import_preview(
                frame,
                source_filename=uploaded.name,
                existing_employees=_fetch_existing_employee_records(conn, tenant_id),
                standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
            )
        except RosterImportError as exc:
            st.error(str(exc))
        else:
            st.session_state[preview_key] = preview_to_dict(preview)
            st.rerun()

    payload = st.session_state.get(preview_key)
    if not payload:
        st.info("Upload a roster file to preview mapped employees before import.")
        return

    preview = preview_from_dict(payload)
    st.markdown(
        f"**Preview:** `{html_lib.escape(preview.source_filename)}` · "
        f"{preview.insert_count} new · {preview.update_count} updates · "
        f"{preview.needs_seniority_count} need seniority"
    )

    if preview.error_count:
        st.warning(f"{preview.error_count} row(s) have validation errors and will be skipped.")

    table_rows: List[Dict[str, object]] = []
    for row in preview.rows:
        if row.validation_errors:
            status = "; ".join(row.validation_errors)
        elif row.needs_seniority_manual and row.seniority_hours is None:
            status = "Needs seniority (required before import)"
        elif row.matched_existing_name:
            status = (
                f"Fuzzy match → {row.matched_existing_name} "
                f"({row.match_confidence:.0%})"
            )
        else:
            status = "Ready"

        table_rows.append(
            {
                "Row": row.row_number,
                "Full Name": row.full_name,
                "Role": row.role_code,
                "Seniority Hours": (
                    "— fill in below —"
                    if row.needs_seniority_manual and row.seniority_hours is None
                    else row.seniority_hours
                ),
                "Target Weekly Hrs": row.target_weekly_hours,
                "FTE": f"{row.fte:.1f}",
                "Action": row.action,
                "Status": status,
            }
        )
    st.dataframe(table_rows, width="stretch", hide_index=True)

    manual_rows = [
        row for row in preview.valid_rows if row.needs_seniority_manual
    ]
    if manual_rows:
        st.markdown("##### Missing seniority — manager fill-in")
        for row in manual_rows:
            value = st.number_input(
                f"Seniority hours for {row.full_name} (row {row.row_number})",
                min_value=0.0,
                step=1.0,
                value=float(row.seniority_hours or 0.0),
                key=f"roster_import_seniority_{tenant_id}_{row.row_number}",
            )
            row.seniority_hours = value
            row.needs_seniority_manual = False

        st.session_state[preview_key] = preview_to_dict(preview)

    action_col, cancel_col = st.columns(2)
    if action_col.button(
        "Confirm Import",
        type="primary",
        width="stretch",
        key=f"roster_import_confirm_{tenant_id}",
        disabled=not preview.can_commit,
    ):
        qualification_ids = {
            code: qual_catalog[code]["id"] for code in available_quals
        }
        try:
            _create_system_snapshot(f"pre-roster-import-{tenant_id}")
            result = commit_import_preview(
                conn,
                tenant_id=tenant_id,
                preview=preview,
                qualification_ids=qualification_ids,
                hire_date=date.today(),
            )
        except RosterImportError as exc:
            st.error(str(exc))
        else:
            st.session_state.pop(preview_key, None)
            st.session_state["roster_success"] = (
                f"Imported roster from **{preview.source_filename}**: "
                f"**{result.inserted}** new, **{result.updated}** updated."
            )
            st.session_state.pop("auto_gen_summary", None)
            st.rerun()

    if cancel_col.button(
        "Discard Preview",
        width="stretch",
        key=f"roster_import_discard_{tenant_id}",
    ):
        st.session_state.pop(preview_key, None)
        st.rerun()


def _render_add_vacant_line_panel(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    rules: JurisdictionRules,
    qual_catalog: Dict[str, Dict],
) -> None:
    available_quals = [code for code in QUALIFICATION_CODES if code in qual_catalog]
    if not available_quals:
        return

    standard_hours = rules.standard_hours_per_week_at_1_0_fte
    hour_options = list(bulk_target_weekly_hours_options(standard_hours))
    default_hours = round(0.7 * standard_hours, 2)
    form_key = f"add_vacant_line_form_{tenant_id}"

    st.markdown("#### Add Vacant Line")
    st.caption(
        "Provision one vacant roster placeholder. The Portage 25-line chart is fixed; "
        "use this only for exceptional one-off additions."
    )

    with st.form(form_key, clear_on_submit=False):
        role_code = st.selectbox(
            "Role",
            options=available_quals,
            format_func=lambda code: f"{code} — {qual_catalog[code]['display_name']}",
            key=f"vacant_line_role_{tenant_id}",
        )
        contract_line_type = st.selectbox(
            "Contract line type",
            options=list(CONTRACT_LINE_TYPES),
            index=0 if role_code == "MLT" else 2,
            format_func=lambda line: {
                "D/N": "D/N — Day / Night (no Evening)",
                "D/E": "D/E — Day / Evening (no Night)",
                "M-F": "M-F — Monday–Friday day line (Morning only)",
            }.get(line, line),
            key=f"vacant_line_contract_{tenant_id}",
        )
        target_hours = st.select_slider(
            "Target weekly hours",
            options=hour_options,
            value=default_hours if default_hours in hour_options else hour_options[0],
            format_func=lambda hours: (
                f"{hours:g} h/week ({hours / standard_hours:.1f} FTE)"
            ),
            key=f"vacant_line_hours_{tenant_id}",
        )
        submitted = st.form_submit_button(
            "Add Vacant Line",
            type="primary",
            width="stretch",
        )

        if submitted:
            st.session_state.pop("roster_success", None)
            st.session_state.pop("roster_error", None)
            qual_id = qual_catalog[role_code]["id"]
            try:
                result = create_vacant_line(
                    conn,
                    tenant_id=tenant_id,
                    role=role_code,
                    contract_line_type=contract_line_type,
                    target_weekly_hours=float(target_hours),
                    qualification_id=qual_id,
                    standard_weekly_hours=standard_hours,
                )
            except StaffLifecycleError as exc:
                st.session_state["roster_error"] = str(exc)
                st.rerun()
            except sqlite3.IntegrityError as exc:
                st.session_state["roster_error"] = (
                    f"Could not add vacant line — database rejected the request: `{exc}`"
                )
                st.rerun()
            else:
                st.session_state["roster_success"] = (
                    f"Added vacant line **{result.display_name}** at "
                    f"**{target_hours:g}h/week**."
                )
                st.session_state.pop("auto_gen_summary", None)
                st.toast(f"Added {result.display_name}.", icon="✅")
                st.rerun()


def _parse_employee_name(full_name: str) -> Optional[Tuple[str, str]]:
    cleaned = " ".join(full_name.strip().split())
    if len(cleaned) < 2:
        return None
    parts = cleaned.split(" ")
    if len(parts) < 2:
        return None
    return parts[0], " ".join(parts[1:])


def _default_contract_line_for_qualification(code: str) -> str:
    return "D/N" if code == "MLT" else "M-F"


def _insert_new_employee(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    first_name: str,
    last_name: str,
    fte: float,
    qualification_id: str,
    qualification_code: str,
    hire_date: date,
    contract_line_type: Optional[str] = None,
) -> str:
    employee_id = f"emp-{uuid.uuid4().hex[:10]}"
    employee_code = next_employee_code(conn, tenant_id)
    now = _utc_now_iso()
    hourly_rate = _default_rate_for_qualification_code(qualification_code)
    line_type = contract_line_type or _default_contract_line_for_qualification(qualification_code)

    conn.execute(
        """
        INSERT INTO employees (
          id, tenant_id, employee_code, first_name, last_name,
          hire_date, fte, base_hourly_rate, contract_line_type, is_active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            employee_id,
            tenant_id,
            employee_code,
            first_name,
            last_name,
            hire_date.isoformat(),
            fte,
            hourly_rate,
            line_type,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO employee_qualifications (
          tenant_id, employee_id, qualification_id, awarded_on, expires_on, created_at
        ) VALUES (?, ?, ?, ?, NULL, ?)
        """,
        (tenant_id, employee_id, qualification_id, hire_date.isoformat(), now),
    )
    conn.commit()
    return employee_id


def _fetch_employee_qualification_ids(
    conn: sqlite3.Connection, tenant_id: str
) -> Dict[str, Set[str]]:
    rows = conn.execute(
        """
        SELECT employee_id, qualification_id
        FROM employee_qualifications
        WHERE tenant_id = ?
        """,
        (tenant_id,),
    ).fetchall()
    out: Dict[str, Set[str]] = {}
    for emp_id, qual_id in rows:
        out.setdefault(emp_id, set()).add(qual_id)
    return out


def _fetch_shift_required_qualification_ids(
    conn: sqlite3.Connection, tenant_id: str
) -> Dict[str, Set[str]]:
    rows = conn.execute(
        """
        SELECT shift_template_id, qualification_id
        FROM shift_template_qualifications
        WHERE tenant_id = ?
        """,
        (tenant_id,),
    ).fetchall()
    out: Dict[str, Set[str]] = {}
    for shift_id, qual_id in rows:
        out.setdefault(shift_id, set()).add(qual_id)
    return out


def _shift_templates_for_compliance(templates: Dict[str, Dict]) -> Dict[str, ShiftTemplateInfo]:
    return {
        tid: ShiftTemplateInfo(
            id=tid,
            code=t["code"],
            name=t["name"],
            start_time=t["start_time"],
            end_time=t["end_time"],
            duration_minutes=t["duration_minutes"],
            crosses_midnight=t["crosses_midnight"],
        )
        for tid, t in templates.items()
    }


def _build_compliance_report(
    rules: JurisdictionRules,
    *,
    employees: List[Dict],
    assignments: List[Dict],
    templates: Dict[str, Dict],
    period: TenantPeriod,
    employee_target_hours: Optional[Dict[str, float]] = None,
) -> ComplianceReport:
    emp_names = {e["id"]: e["full_name"] for e in employees}
    scheduled = [
        ScheduledShift(
            employee_id=a["employee_id"],
            employee_name=emp_names.get(a["employee_id"], a["employee_id"]),
            assignment_date=a["assignment_date"],
            shift_template_id=a["shift_template_id"],
        )
        for a in assignments
    ]
    return evaluate_schedule(
        rules,
        employees=employees,
        assignments=scheduled,
        shift_templates=_shift_templates_for_compliance(templates),
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        weeks_in_period=period.week_count,
        employee_target_hours=employee_target_hours,
    )


def _fetch_shift_required_qual_labels(
    conn: sqlite3.Connection, tenant_id: str, shift_template_id: str
) -> List[str]:
    rows = conn.execute(
        """
        SELECT q.code, q.display_name
        FROM shift_template_qualifications stq
        JOIN qualifications q
          ON q.tenant_id = stq.tenant_id
         AND q.id = stq.qualification_id
        WHERE stq.tenant_id = ? AND stq.shift_template_id = ?
        ORDER BY q.code
        """,
        (tenant_id, shift_template_id),
    ).fetchall()
    return [f"{code} ({name})" for code, name in rows]


def _fetch_employee_qual_labels(
    conn: sqlite3.Connection, tenant_id: str, employee_id: str
) -> List[str]:
    rows = conn.execute(
        """
        SELECT q.code, q.display_name
        FROM employee_qualifications eq
        JOIN qualifications q
          ON q.tenant_id = eq.tenant_id
         AND q.id = eq.qualification_id
        WHERE eq.tenant_id = ? AND eq.employee_id = ?
        ORDER BY q.code
        """,
        (tenant_id, employee_id),
    ).fetchall()
    return [f"{code} ({name})" for code, name in rows]


def _schedule_import_stamp_path() -> Path:
    return ROOT / ".last_schedule_import"


def _apply_external_schedule_import_if_needed(period_id: str) -> None:
    """Reload grid from DB after CLI/manual imports without restarting Streamlit."""
    stamp_path = _schedule_import_stamp_path()
    if not stamp_path.exists():
        return
    stamp_mtime = stamp_path.stat().st_mtime
    seen_key = f"schedule_import_seen_{period_id}"
    if float(st.session_state.get(seen_key, 0.0) or 0.0) >= stamp_mtime:
        return
    st.session_state[seen_key] = stamp_mtime
    st.session_state[f"schedule_sync_{period_id}"] = True


def _resolve_assignments_for_grid(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    gates: FeatureGates,
) -> List[Dict]:
    assignments = _fetch_assignments(conn, tenant_id, period.id)
    if not gates.is_premium:
        assignments = _filter_assignments_through(
            period.period_end_inclusive,
            assignments,
        )
    return assignments



def _fetch_assignments(
    conn: sqlite3.Connection, tenant_id: str, schedule_period_id: str
) -> List[Dict]:
    rows = conn.execute(
        """
        SELECT id, employee_id, shift_template_id, assignment_date
        FROM shift_assignments
        WHERE tenant_id = ? AND schedule_period_id = ?
          AND COALESCE(assignment_status, 'assigned') = 'assigned'
        ORDER BY assignment_date, employee_id
        """,
        (tenant_id, schedule_period_id),
    ).fetchall()

    return [
        {
            "id": r[0],
            "employee_id": r[1],
            "shift_template_id": r[2],
            "assignment_date": date.fromisoformat(r[3]),
        }
        for r in rows
    ]


def _find_assignment_id(
    conn: sqlite3.Connection,
    tenant_id: str,
    employee_id: str,
    assignment_date: date,
) -> Optional[str]:
    row = conn.execute(
        """
        SELECT id
        FROM shift_assignments
        WHERE tenant_id = ? AND employee_id = ? AND assignment_date = ?
          AND COALESCE(assignment_status, 'assigned') = 'assigned'
        """,
        (tenant_id, employee_id, assignment_date.isoformat()),
    ).fetchone()
    return row[0] if row else None


def _upsert_assignment(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    employee_id: str,
    shift_template_id: str,
    assignment_date: date,
) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    now = _utc_now_iso()
    existing_id = _find_assignment_id(conn, tenant_id, employee_id, assignment_date)

    if existing_id:
        conn.execute(
            """
            UPDATE shift_assignments
            SET shift_template_id = ?,
                schedule_period_id = ?,
                updated_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                shift_template_id,
                schedule_period_id,
                now,
                existing_id,
                tenant_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO shift_assignments (
              id, tenant_id, schedule_period_id, employee_id,
              shift_template_id, assignment_date, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"asg-{uuid.uuid4().hex[:12]}",
                tenant_id,
                schedule_period_id,
                employee_id,
                shift_template_id,
                assignment_date.isoformat(),
                now,
                now,
            ),
        )

    conn.commit()






def _delete_assignment(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    employee_id: str,
    assignment_date: date,
) -> bool:
    cur = conn.execute(
        """
        DELETE FROM shift_assignments
        WHERE tenant_id = ? AND employee_id = ? AND assignment_date = ?
        """,
        (tenant_id, employee_id, assignment_date.isoformat()),
    )
    conn.commit()
    return cur.rowcount > 0


def _delete_all_period_assignments(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
) -> int:
    """Remove every assigned shift row for a schedule period."""

    cur = conn.execute(
        """
        DELETE FROM shift_assignments
        WHERE tenant_id = ? AND schedule_period_id = ?
          AND COALESCE(assignment_status, 'assigned') = 'assigned'
        """,
        (tenant_id, schedule_period_id),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def _build_compliance_error_message(
    conn: sqlite3.Connection,
    exc: sqlite3.IntegrityError,
    *,
    tenant_id: str,
    employee: Dict,
    shift: Dict,
    assignment_date: date,
    period: TenantPeriod,
) -> str:
    raw = str(exc).lower()

    if "employee lacks required qualification" in raw:
        required = _fetch_shift_required_qual_labels(conn, tenant_id, shift["id"])
        held = _fetch_employee_qual_labels(conn, tenant_id, employee["id"])
        return (
            f"**Qualification mismatch:** {employee['full_name']} cannot be assigned to "
            f"**{shift['name']}** ({shift['code']}) on **{assignment_date.isoformat()}**.\n\n"
            f"- **Shift requires:** {', '.join(required) if required else 'n/a'}\n"
            f"- **Employee holds:** {', '.join(held) if held else 'none'}\n\n"
            "Example blocked action: assigning an **MLA** to an **MLT-only** shift."
        )

    if "assignment_date must fall within the schedule period" in raw:
        return (
            f"**Schedule period violation:** {assignment_date.isoformat()} is outside "
            f"**{period.name}** ({period.period_start.isoformat()} to "
            f"{period.period_end_inclusive.isoformat()})."
        )

    if "unique" in raw:
        return (
            f"**Duplicate assignment:** {employee['full_name']} already has a shift on "
            f"{assignment_date.isoformat()}. Use a different date or clear the existing shift first."
        )

    return f"**Database compliance rule triggered:**\n\n`{exc}`"


def _render_critical_contract_line_violation(message: str) -> None:
    st.markdown(
        _html_micro_banner(
            message,
            "error",
            title="CRITICAL · Contract Line Violation",
            escape_message=False,
        ),
        unsafe_allow_html=True,
    )


def _render_compliance_flash(title: str, body_markdown: str) -> None:
    st.markdown(
        _html_micro_banner(
            "The schedule was NOT changed. Database triggers rejected this update.",
            "error",
            title=title,
        ),
        unsafe_allow_html=True,
    )
    st.markdown(body_markdown)


def _create_system_snapshot(label: str) -> None:
    try:
        create_snapshot(DB_PATH, label=label, snapshots_dir=SNAPSHOTS_DIR)
    except SnapshotError:
        pass


def _qual_code_map(emp_quals: Dict[str, Set[str]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for qual_ids in emp_quals.values():
        for qual_id in qual_ids:
            upper = qual_id.upper()
            if "MLA" in upper:
                mapping[qual_id] = "MLA"
            elif "MLT" in upper:
                mapping[qual_id] = "MLT"
    return mapping




def _agency_request_session_key(period_id: str) -> str:
    return f"agency_request_path_{period_id}"


def _resolve_agency_request_path(period_id: str) -> Optional[Path]:
    raw = st.session_state.get(_agency_request_session_key(period_id))
    if raw:
        candidate = resolve_project_path(ROOT, str(raw))
        if candidate.is_file():
            return candidate
    return find_latest_agency_request(ROOT, schedule_period_id=period_id)


def _triage_escalation_session_key(period_id: str) -> str:
    return f"triage_escalation_path_{period_id}"


def _resolve_triage_escalation_path(period_id: str) -> Optional[Path]:
    raw = st.session_state.get(_triage_escalation_session_key(period_id))
    if not raw:
        return None
    return resolve_project_path(ROOT, str(raw))


def _apply_live_triage_tags_to_schedule_rows(
    schedule_rows: List[Dict],
    *,
    dates: List[date],
    period_id: str,
    schedule_archetype: str | None = None,
) -> List[Dict]:
    # Air gap: 12-hour (7-on/7-off) schedules are authored entirely by the
    # deterministic stamper. Triage escalation is a solver concept and must never
    # paint "[UNFILLED - ESCALATED]" onto these lines, even if a stale triage JSON
    # from a prior STANDARD run is still referenced in session state.
    if schedule_archetype is None:
        schedule_archetype = st.session_state.get(
            _schedule_archetype_session_key(period_id),
            ScheduleArchetype.STANDARD.value,
        )
    if schedule_archetype == ScheduleArchetype.TWELVE_HOUR.value:
        return filter_breakroom_export_rows(schedule_rows)
    triage_path = _resolve_triage_escalation_path(period_id)
    if triage_path is None or not triage_path.is_file():
        return filter_breakroom_export_rows(schedule_rows)
    triage_list = load_triage_escalation_payload(triage_path).get("triage_list", ())
    if not triage_list:
        return filter_breakroom_export_rows(schedule_rows)
    tagged = apply_triage_escalation_tags(schedule_rows, triage_list, dates)
    return filter_breakroom_export_rows(tagged)






def _schedule_archetype_session_key(period_id: str) -> str:
    return f"schedule_archetype_{period_id}"


def _schedule_archetype_options() -> dict[str, str]:
    return {
        "Regular schedule (M/E/N master rotation)": ScheduleArchetype.STANDARD.value,
        "7-on / 7-off (12-hour lines)": ScheduleArchetype.TWELVE_HOUR.value,
    }




def _employee_qual_codes(
    profiles: Sequence[EmployeeProfile],
    emp_quals: Dict[str, Set[str]],
) -> Dict[str, str]:
    return {
        profile.id: infer_qual_code(profile, qual_codes=emp_quals)
        for profile in profiles
    }








def _build_cleared_schedule_draft(
    draft: pd.DataFrame,
    *,
    employees: List[Dict],
    dates: List[date],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Optional[Dict[str, Dict[date, str]]] = None,
) -> Tuple[pd.DataFrame, int]:
    """Blank worked D/E/N cells; preserve locks and time-off blocks."""

    blocked_map = blocked_map or {}
    cleared = draft.copy()
    changed = 0
    roster_ids = {str(employee["id"]) for employee in employees}
    for row_idx in cleared.index:
        employee_id = str(cleared.at[row_idx, "employee_id"] or "")
        if not employee_id or employee_id not in roster_ids:
            continue
        if is_daily_tally_employee_id(employee_id):
            continue
        for day in dates:
            if (employee_id, day) in locked_cells:
                continue
            if day in blocked_map.get(employee_id, {}):
                continue
            day_key = day.isoformat()
            if _normalize_shift_cell(cleared.at[row_idx, day_key]) in ALLOWED_SHIFT_CODES:
                cleared.at[row_idx, day_key] = EMPTY_SHIFT_DISPLAY
                changed += 1
    return cleared, changed


def _focus_grid_sizer_javascript() -> str:
    """Parent-page JS: apply focus body class and pin/resize the schedule stIFrame."""

    return """
<div id="lab-focus-sizer-root" style="height:0;width:0;overflow:hidden;margin:0;padding:0" aria-hidden="true"></div>
<script>
(function () {
  document.body.classList.add("lab-schedule-focus-mode");

  function lockParentViewport() {
    if (document.body.dataset.labFsParentLocked === "1") return;
    document.body.dataset.labFsParentLocked = "1";
    document.documentElement.style.overflow = "hidden";
    document.documentElement.style.height = "100%";
    document.body.style.overflow = "hidden";
    document.body.style.position = "fixed";
    document.body.style.inset = "0";
    document.body.style.width = "100%";
    document.body.style.height = "100%";
    document.body.style.touchAction = "none";
    document.body.style.overscrollBehavior = "none";
    function blockParentScroll(ev) {
      if (ev.target && ev.target.closest) {
        if (ev.target.closest('[data-testid="stSidebar"]')) return;
        if (ev.target.closest('[data-testid="stSidebarCollapsedControl"]')) return;
      }
      ev.preventDefault();
    }
    window.addEventListener("wheel", blockParentScroll, { passive: false, capture: true });
    window.addEventListener("touchmove", blockParentScroll, { passive: false, capture: true });
    window.addEventListener("scroll", blockParentScroll, { passive: false, capture: true });
  }
  lockParentViewport();

  function focusGridFrame() {
    var anchor = document.getElementById("lab-focus-grid-anchor");
    if (!anchor) return null;
    var frames = document.querySelectorAll("iframe");
    for (var i = 0; i < frames.length; i++) {
      if (anchor.compareDocumentPosition(frames[i]) & Node.DOCUMENT_POSITION_FOLLOWING) {
        return frames[i];
      }
    }
    return null;
  }

  function focusGridHost(frame) {
    if (!frame) return null;
    return frame.closest('[data-testid="stElementContainer"]');
  }

  function collapseFocusChrome() {
    var anchor = document.getElementById("lab-focus-grid-anchor");
    if (!anchor) return;
    var main = document.querySelector("section.main");
    if (main) {
      var beforeAnchor = true;
      main.querySelectorAll('[data-testid="stElementContainer"]').forEach(function (host) {
        if (host.contains(anchor)) {
          beforeAnchor = false;
          return;
        }
        if (beforeAnchor) {
          host.style.display = "none";
        }
      });
    }
    var anchorHost = anchor.closest('[data-testid="stElementContainer"]');
    var block = anchorHost && anchorHost.closest('[data-testid="stVerticalBlock"]');
    if (!block) return;
    var wraps = block.querySelectorAll('[data-testid="stVerticalBlockBorderWrapper"]');
    wraps.forEach(function (wrap) {
      if (wrap.contains(anchor)) return;
      wrap.style.display = "none";
    });
  }

  function viewportSize() {
    var side = focusSidebarOffset();
    var vv = window.visualViewport;
    var innerW = vv && vv.width ? vv.width : window.innerWidth;
    var innerH = vv && vv.height ? vv.height : window.innerHeight;
    return {
      width: Math.max(320, Math.floor(innerW - side)),
      height: Math.max(320, Math.floor(innerH)),
      side: side,
    };
  }

  function focusSidebarOffset() {
    var sidebar = document.querySelector('[data-testid="stSidebar"]');
    if (!sidebar) return 0;
    var rect = sidebar.getBoundingClientRect();
    if (!rect || rect.width < 8) return 0;
    return rect.width;
  }

  function sizeFocusGrid() {
    collapseFocusChrome();
    var vp = viewportSize();
    var side = vp.side;
    var height = vp.height;
    var width = vp.width;
    var frame = focusGridFrame();
    var gridContainer = focusGridHost(frame);
    if (!frame || !gridContainer) {
      return;
    }

    gridContainer.classList.add("lab-focus-grid-pinned");
    gridContainer.style.position = "fixed";
    gridContainer.style.zIndex = "1002";
    gridContainer.style.top = "0px";
    gridContainer.style.left = side + "px";
    gridContainer.style.width = width + "px";
    gridContainer.style.height = height + "px";
    gridContainer.style.right = "auto";
    gridContainer.style.bottom = "auto";

    var htmlWrap =
      gridContainer.querySelector('[data-testid="stHtml"]') ||
      gridContainer.querySelector('[data-testid="stIFrame"]') ||
      frame.parentElement;

    [frame, htmlWrap, gridContainer].forEach(function (el) {
      if (!el) return;
      el.style.width = "100%";
      el.style.height = height + "px";
      el.style.minHeight = height + "px";
      el.style.maxHeight = height + "px";
      el.style.overflow = "hidden";
    });
    frame.setAttribute("scrolling", "no");
    gridContainer.style.marginBottom = "0";
    gridContainer.style.paddingBottom = "0";
    var borderWrap = gridContainer.closest('[data-testid="stVerticalBlockBorderWrapper"]');
    if (borderWrap) {
      borderWrap.style.height = height + "px";
      borderWrap.style.minHeight = height + "px";
      borderWrap.style.maxHeight = height + "px";
      borderWrap.style.marginBottom = "0";
      borderWrap.style.overflow = "hidden";
    }
    try {
      if (frame.contentWindow) {
        frame.contentWindow.postMessage(
          { type: "lab-fs-viewport", width: width, height: height },
          "*"
        );
        if (typeof frame.contentWindow.fitFocusGridToViewport === "function") {
          frame.contentWindow.fitFocusGridToViewport();
        }
      }
    } catch (_) {}
  }

  sizeFocusGrid();
  window.addEventListener("resize", sizeFocusGrid);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", sizeFocusGrid);
  }
  setTimeout(sizeFocusGrid, 40);
  setTimeout(sizeFocusGrid, 180);
  setTimeout(sizeFocusGrid, 500);
  if (typeof MutationObserver !== "undefined") {
    new MutationObserver(function () {
      sizeFocusGrid();
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
</script>
"""


def _inject_focus_layout_css() -> None:
    """Focus mode page chrome: light full-bleed shell (sizer runs after grid render)."""

    from lab_scheduler.ui.schedule_focus import focus_mode_page_stylesheet

    st.markdown(focus_mode_page_stylesheet(), unsafe_allow_html=True)


def _query_param_scalar(name: str) -> Optional[str]:
    value = st.query_params.get(name)
    if isinstance(value, list):
        return str(value[0]) if value else None
    if value is None:
        return None
    return str(value)


def _clear_query_param(name: str) -> None:
    try:
        del st.query_params[name]
    except (KeyError, TypeError):
        st.query_params.from_dict(
            {key: value for key, value in st.query_params.to_dict().items() if key != name}
        )


def _handle_focus_exit_query_param(*, period_id: str) -> bool:
    """Exit focus when the floating link sets ?exit_focus=<period_id>."""

    exit_period = _query_param_scalar("exit_focus")
    if not exit_period or exit_period != str(period_id):
        return False
    st.session_state[_schedule_focus_key(period_id)] = False
    _clear_query_param("exit_focus")
    st.rerun()
    return True


def _render_manager_sidebar_save_panel(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    employee_target_hours: Dict[str, float],
    availability_blocked: Dict[str, Set[date]],
    manager_mode: bool,
) -> None:
    """Primary save controls for manual editing (manager + ops console)."""

    del conn, tenant_id, rules, employees, templates, employee_target_hours, availability_blocked, manager_mode

    focus_active = _schedule_focus_active(period.id)
    pending_count = len(_load_pending_mutations(period.id))
    backup_name = _standard_json_backup_path(period.id).name

    st.sidebar.markdown("---")
    st.sidebar.markdown("**Save**")
    if focus_active:
        st.sidebar.caption(
            "Fullscreen mode — sidebar stays open for **Save**. Collapse it (») for more grid width."
        )
    else:
        st.sidebar.caption(
            f"Commits grid edits to the database and refreshes `{backup_name}`. "
            "Prefer this button over the ribbon Save when edits do not stick."
        )

    if pending_count:
        st.sidebar.warning(
            f"{pending_count} unsaved grid change{'s' if pending_count != 1 else ''}."
        )

    save_notice = st.session_state.pop(_workspace_publish_notice_key(period.id), None)
    if save_notice:
        st.sidebar.success(save_notice.replace("**", ""))

    if st.sidebar.button(
        _save_button_label(period.id),
        type="primary",
        width="stretch",
        key=f"sidebar_save_{period.id}",
    ):
        handle_save_button_click(st.session_state, period.id, rerun=st.rerun)
        st.rerun()

    if focus_active and st.sidebar.button(
        "Exit fullscreen",
        width="stretch",
        key=f"sidebar_exit_focus_{period.id}",
    ):
        st.session_state[_schedule_focus_key(period.id)] = False
        st.rerun()




def _render_focus_controls(
    *,
    period_id: str,
    health_snapshot: Optional[ScheduleHealthSnapshot] = None,
) -> bool:
    """Fullscreen enter + clear controls for normal view. Returns True when fullscreen is active."""

    focus_key = _schedule_focus_key(period_id)
    focus_active = _schedule_focus_active(period_id)

    if focus_active:
        return True

    focus_col, clear_col, distribute_col, fill_col, info_col = st.columns(
        [1, 1, 1.15, 1.15, 2.7], gap="small"
    )
    confirm_key = f"schedule_clear_confirm_{period_id}"
    distribute_confirm_key = f"schedule_distribute_alt_confirm_{period_id}"
    fill_confirm_key = f"schedule_alternate_fill_confirm_{period_id}"
    fill_override_key = f"schedule_alternate_fill_override_{period_id}"
    with focus_col:
        if st.button(
            "⛶ Fullscreen",
            key=f"enter_schedule_focus_{period_id}",
            help="Show the full period stretched to your screen for ultra-wide editing",
            width="stretch",
        ):
            st.session_state[focus_key] = True
            st.session_state[_schedule_edit_mode_key(period_id)] = True
            st.rerun()
    with clear_col:
        if st.button(
            "Clear schedule",
            key=f"clear_schedule_{period_id}",
            help=(
                "Blank editable D/E/N shifts for the full period. "
                "Green-ring locked weeks and time-off blocks are kept."
            ),
            width="stretch",
        ):
            pending_count = len(_load_pending_mutations(period_id))
            needs_confirm = pending_count > 0 and not st.session_state.pop(confirm_key, False)
            if needs_confirm:
                st.session_state[confirm_key] = True
                st.toast(
                    "Unsaved edits present — click Clear schedule again to confirm.",
                    icon="⚠️",
                )
                st.rerun()
            else:
                st.session_state.pop(confirm_key, None)
                st.session_state[_schedule_clear_pending_key(period_id)] = True
                st.rerun()
    with distribute_col:
        if st.button(
            "Distribute weekend shifts",
            key=f"distribute_alt_{period_id}",
            help=(
                "Full-time vacant lines: stagger weekends by line (1→W1–2, 2→W3–4, …). "
                "D/E lines 1–4 get E, lines 5–8 get D; D/N lines get N. E/N are placed "
                "first, then weekend D. Respects locks and time-off. Click Save to persist."
            ),
            width="stretch",
        ):
            pending_count = len(_load_pending_mutations(period_id))
            needs_confirm = pending_count > 0 and not st.session_state.pop(
                distribute_confirm_key, False
            )
            if needs_confirm:
                st.session_state[distribute_confirm_key] = True
                st.toast(
                    "Unsaved edits present — click Distribute weekend shifts again to confirm.",
                    icon="⚠️",
                )
                st.rerun()
            else:
                st.session_state.pop(distribute_confirm_key, None)
                st.session_state[_schedule_distribute_alt_pending_key(period_id)] = True
                st.rerun()
    with fill_col:
        if st.button(
            "Fill alternate shifts",
            key=f"alternate_fill_{period_id}",
            help=(
                "One 7-day E block per FT D/E line (staggered), then weekday N and "
                "clinical-floor top-up. Does not replace D/N catalog nights. "
                "Empty cells only; safe to re-run after roster changes."
            ),
            width="stretch",
        ):
            pending_count = len(_load_pending_mutations(period_id))
            soft_gate = pending_count >= 50 or (
                health_snapshot is not None and not health_snapshot.is_operational_floor_ok
            )
            needs_unsaved_confirm = pending_count > 0 and not st.session_state.pop(
                fill_confirm_key, False
            )
            needs_override_confirm = soft_gate and not st.session_state.pop(
                fill_override_key, False
            )
            if needs_unsaved_confirm or needs_override_confirm:
                if pending_count > 0:
                    st.session_state[fill_confirm_key] = True
                if soft_gate:
                    st.session_state[fill_override_key] = True
                toast_parts: List[str] = []
                if pending_count > 0:
                    toast_parts.append("unsaved edits present")
                if soft_gate:
                    toast_parts.append("operational floor or heavy edit load")
                st.toast(
                    f"{' and '.join(toast_parts).capitalize()} — "
                    "click Fill alternate shifts again to confirm.",
                    icon="⚠️",
                )
                st.rerun()
            else:
                st.session_state.pop(fill_confirm_key, None)
                st.session_state.pop(fill_override_key, None)
                st.session_state[_schedule_alternate_fill_pending_key(period_id)] = True
                st.rerun()
    with info_col:
        st.caption(
            "Show the entire period in **Fullscreen** — stretch and zoom to fit any screen. "
            "**Distribute weekend shifts** staggers weekend blocks only; "
            "**Fill alternate shifts** adds weekday E/N. Use **Save** to commit."
        )
    return False






def _format_print_shift_cell_inner(token: str) -> str:
    text = str(token).strip()
    if text == TRIAGE_ESCALATED_CELL_TAG:
        return (
            f'<span class="triage-escalated-tag">'
            f"{html_lib.escape(TRIAGE_ESCALATED_CELL_TAG)}</span>"
        )
    if text == "AGY":
        return '<span class="lab-print-token agency-placeholder-tag">AGY</span>'
    normalized = _normalize_shift_cell(token)
    if normalized in ("D", "M", "E", "N", "I"):
        token_class = _print_shift_token_class(token)
        style = _shift_style_for_value(token)
        class_attr = f"lab-print-token {token_class}".strip()
        return (
            f'<span class="{class_attr}" '
            f'style="background:{style["bg"]};color:{style["fg"]};">'
            f"{html_lib.escape('D' if normalized in ('D', 'M') else normalized)}"
            f"</span>"
        )
    if normalized:
        return html_lib.escape(normalized)
    return "&nbsp;"


def _format_print_shift_cell(token: str) -> str:
    return f"<td>{_format_print_shift_cell_inner(token)}</td>"


def _build_watermarked_breakroom_preview(html_doc: str) -> str:
    watermark = (
        '<div style="position:fixed;top:38%;left:10%;transform:rotate(-28deg);'
        'font-size:42px;color:rgba(180,83,9,0.28);font-weight:800;z-index:9999;'
        'pointer-events:none;letter-spacing:0.06em;">'
        "TRIAL PREVIEW — UPGRADE TO POST"
        "</div>"
    )
    if "</body>" in html_doc:
        return html_doc.replace("</body>", f"{watermark}</body>", 1)
    return f"{html_doc}{watermark}"


def _build_breakroom_print_document(
    *,
    facility_name: str,
    period: TenantPeriod,
    employees: List[Dict],
    dates: List[date],
    assignments: List[Dict],
    templates: Dict[str, Dict],
    blocked_map: Optional[Dict[str, Dict[date, str]]] = None,
    schedule_archetype: str | None = None,
    paper_size: str = "legal",
    rules: Optional[JurisdictionRules] = None,
    qual_codes: Optional[Dict[str, str]] = None,
    qual_ids_by_employee: Optional[Dict[str, Set[str]]] = None,
    contract_target_hours: Optional[Dict[str, float]] = None,
    posting_context: Optional[BreakroomPostingContext] = None,
) -> str:
    if schedule_archetype is None:
        schedule_archetype = st.session_state.get(
            _schedule_archetype_session_key(period.id),
            ScheduleArchetype.STANDARD.value,
        )
    agency_path = _resolve_agency_request_path(period.id)
    schedule_df = _build_schedule_dataframe(
        employees,
        dates,
        assignments,
        templates,
        blocked_map=blocked_map,
        include_daily_tallies=True,
        agency_request_path=agency_path,
    )
    schedule_rows = _apply_live_triage_tags_to_schedule_rows(
        schedule_df.to_dict("records"),
        dates=dates,
        period_id=period.id,
        schedule_archetype=schedule_archetype,
    )
    template_info = _shift_templates_for_compliance(templates)
    open_slots = list_open_shift_slots(
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        shift_templates=template_info,
        assignments=_assignments_to_scheduled(assignments, employees),
        schedule_archetype=schedule_archetype,
    )
    coverage_gaps_by_day = build_coverage_gaps_by_day(open_slots)
    contract_target_hours_by_employee = contract_target_hours
    triage_path = _resolve_triage_escalation_path(period.id)
    if triage_path is not None and triage_path.is_file():
        _, html = render_breakroom_schedule_html(
            schedule_rows=schedule_rows,
            employees=employees,
            dates=dates,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
            week_count=period.week_count,
            triage_escalation_path=triage_path,
            facility_name=facility_name,
            period_name=period.name,
            compliance_verified_on=date.today(),
            schedule_archetype=schedule_archetype,
            coverage_gaps_by_day=coverage_gaps_by_day,
            paper_size=paper_size,
            contract_target_hours_by_employee=contract_target_hours_by_employee,
            posting_context=posting_context,
        )
        return html
    return generate_breakroom_print_html(
        facility_name=facility_name,
        period_name=period.name,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        week_count=period.week_count,
        employees=employees,
        dates=dates,
        schedule_rows=schedule_rows,
        compliance_verified_on=date.today(),
        schedule_archetype=schedule_archetype,
        coverage_gaps_by_day=coverage_gaps_by_day,
        paper_size=paper_size,
        contract_target_hours_by_employee=contract_target_hours_by_employee,
        posting_context=posting_context,
    )




def _provisional_assignments_session_key(period_id: str) -> str:
    return f"provisional_assignments_{period_id}"


def _provisional_assignments_from_session(
    period_id: str,
) -> List[ProvisionalAssignment]:
    payload = st.session_state.get(_provisional_assignments_session_key(period_id))
    if not isinstance(payload, list):
        return []
    rows: List[ProvisionalAssignment] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        assignment_date = item.get("assignment_date")
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        rows.append(
            ProvisionalAssignment(
                employee_id=str(item.get("employee_id", "")),
                employee_name=str(item.get("employee_name", "")),
                assignment_date=assignment_date,
                shift_template_id=str(item.get("shift_template_id", "")),
                shift_code=str(item.get("shift_code", "")),
                violation_code=str(item.get("violation_code", "")),
                violation_label=str(item.get("violation_label", "")),
                message=str(item.get("message", "")),
                reason=str(item.get("reason", "clinical_floor_mandate")),
                assignment_id=str(item.get("assignment_id", "")),
            )
        )
    return rows


def _store_provisional_assignments_session(
    period_id: str,
    rows: Sequence[ProvisionalAssignment],
) -> None:
    st.session_state[_provisional_assignments_session_key(period_id)] = [
        item.to_dict() for item in rows
    ]


def _render_suggested_compliance_overrides_expander(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    assignments: List[Dict],
    templates: Dict[str, Dict],
    employees: List[Dict],
) -> None:
    pending = _provisional_assignments_from_session(period.id)
    if not pending:
        pending = load_pending_provisional_assignments(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=period.id,
            assignments=assignments,
            templates=templates,
        )
        pending = attach_assignment_ids(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=period.id,
            provisional_assignments=pending,
        )
    pending = [
        item
        for item in pending
        if not _provisional_override_resolved(
            next(
                (
                    str(row.get("system_note") or "")
                    for row in assignments
                    if str(row.get("employee_id")) == item.employee_id
                    and row.get("assignment_date") == item.assignment_date
                    and str(row.get("shift_template_id")) == item.shift_template_id
                ),
                "",
            )
        )
    ]
    if not pending:
        st.session_state.pop(_provisional_assignments_session_key(period.id), None)
        return

    _store_provisional_assignments_session(period.id, pending)
    employee_lookup = {employee["id"]: employee["full_name"] for employee in employees}

    with st.expander("Suggested Compliance Overrides", expanded=True):
        st.caption(
            "These assignments use a stretch, turnaround, or **Contract Line Exception** "
            "override to preserve the 2-seat Evening/Night clinical floor. Approve each row "
            "to authorize it without rerunning Auto-Pilot."
        )
        if st.session_state.get("provisional_approval_notice"):
            st.success(st.session_state.pop("provisional_approval_notice"))

        for index, item in enumerate(pending):
            display_name = employee_lookup.get(item.employee_id, item.employee_name)
            detail_col, action_col = st.columns([0.78, 0.22], gap="small")
            with detail_col:
                st.markdown(
                    f"**{html_lib.escape(display_name)}** · "
                    f"{item.assignment_date.isoformat()} · "
                    f"{html_lib.escape(item.shift_code)}"
                )
                st.caption(
                    f"{html_lib.escape(item.violation_label)} — "
                    f"{html_lib.escape(item.message)}"
                )
            with action_col:
                if st.button(
                    "One-Click Approve",
                    key=f"approve_provisional_{period.id}_{index}",
                    type="primary",
                    width="stretch",
                ):
                    actor = st.session_state.get("username", "manager")
                    approved = approve_provisional_assignment(
                        conn,
                        tenant_id=tenant_id,
                        schedule_period_id=period.id,
                        provisional=item,
                        actor=actor,
                    )
                    if not approved:
                        st.error("Could not approve this override. Refresh and try again.")
                        continue
                    remaining = [row for row_index, row in enumerate(pending) if row_index != index]
                    _store_provisional_assignments_session(period.id, remaining)
                    log_manual_edit(
                        conn,
                        tenant_id=tenant_id,
                        schedule_period_id=period.id,
                        employee_id=item.employee_id,
                        shift_date=item.assignment_date,
                        previous_shift_code=item.shift_code,
                        new_shift_code=item.shift_code,
                        actor=f"{actor} [approved provisional stretch]",
                    )
                    st.session_state["provisional_approval_notice"] = (
                        f"Approved stretch override for {display_name} on "
                        f"{item.assignment_date.isoformat()}."
                    )
                    st.session_state[f"schedule_sync_{period.id}"] = True
                    st.rerun()




def _schedule_baseline_key(period_id: str) -> str:
    return schedule_sess.baseline_key(period_id)


def _schedule_errors_key(period_id: str) -> str:
    return f"schedule_cell_errors_{period_id}"


def _schedule_ignore_grid_echo_key(period_id: str) -> str:
    return schedule_sess.ignore_grid_echo_key(period_id)


def _schedule_save_requested_key(period_id: str) -> str:
    return schedule_sess.save_requested_key(period_id)


def _schedule_draft_key(period_id: str) -> str:
    return schedule_sess.draft_key(period_id)


def _schedule_matrix_key(period_id: str) -> str:
    """Session key for the live employee shift matrix (date columns only)."""

    return schedule_sess.matrix_cache_key(period_id)


def _employee_schedule_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    cleaned = frame.copy()
    if "employee_id" in cleaned.columns:
        cleaned = cleaned[~cleaned["employee_id"].apply(is_daily_tally_employee_id)]
    elif "Employee" in cleaned.columns:
        cleaned = cleaned[~cleaned["Employee"].isin(ALL_DAILY_TALLY_ROW_NAMES)]
    return cleaned


def _matrix_frame_for_view(
    period_id: str,
    draft: pd.DataFrame,
    view_dates: List[date],
    employees: List[Dict],
    *,
    cell_change: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """Build the editable matrix slice and persist it for tally/footer rendering."""

    matrix = _slice_schedule_frame_for_view(_employee_schedule_matrix(draft), view_dates)
    if cell_change and cell_change.get("employee_id") and cell_change.get("date"):
        employee_id = str(cell_change["employee_id"])
        date_col = str(cell_change["date"])
        if "employee_id" in matrix.columns and date_col in matrix.columns:
            row_indices = matrix.index[matrix["employee_id"] == employee_id].tolist()
            if row_indices:
                matrix.at[row_indices[0], date_col] = cell_change["token"]
    st.session_state[_schedule_matrix_key(period_id)] = matrix
    return matrix


def _merge_matrix_cache_into_draft(
    period_id: str,
    draft: pd.DataFrame,
) -> pd.DataFrame:
    """Merge any live grid slice cached in session into the full-period draft."""

    cached = st.session_state.get(_schedule_matrix_key(period_id))
    if not isinstance(cached, pd.DataFrame) or cached.empty or draft.empty:
        return draft
    if "employee_id" not in draft.columns or "employee_id" not in cached.columns:
        return draft

    merged = draft.copy()
    row_index = schedule_frame_row_index_by_employee_id(merged)
    static_cols = set(_schedule_grid_static_columns())
    for col in cached.columns:
        col_key = str(col)
        if col_key in static_cols or col_key not in merged.columns:
            continue
        try:
            date.fromisoformat(col_key)
        except (TypeError, ValueError):
            continue
        for _, cache_row in cached.iterrows():
            employee_id = str(cache_row.get("employee_id", "") or "")
            row_idx = row_index.get(employee_id)
            if row_idx is None:
                continue
            cache_token = _normalize_shift_cell(cache_row[col_key])
            draft_token = _normalize_shift_cell(merged.at[row_idx, col_key])
            if not cache_token and draft_token:
                continue
            merged.at[row_idx, col_key] = _display_shift_cell(cache_row[col_key])
    return merged


def _prepare_draft_for_save(
    period_id: str,
    *,
    dates: List[date],
    employees: List[Dict],
    templates: Dict[str, Dict],
    persist: bool = True,
) -> Tuple[pd.DataFrame, int, int]:
    """Merge pending edits and matrix cache into the session draft before publish."""

    draft_key = _schedule_draft_key(period_id)
    draft = st.session_state.get(draft_key)
    if not isinstance(draft, pd.DataFrame):
        return pd.DataFrame(), 0, 0
    before_shifts = _count_worked_shifts_in_frame(
        draft,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    merged = _apply_pending_mutations_to_draft(
        draft,
        period_id=period_id,
        dates=dates,
    )
    merged = _merge_matrix_cache_into_draft(period_id, merged)
    sanitized = _sanitize_schedule_dataframe(merged, dates)
    draft_shifts = _count_worked_shifts_in_frame(
        sanitized,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    cache_shifts = 0
    cached = st.session_state.get(_schedule_matrix_key(period_id))
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        cache_shifts = _count_worked_shifts_in_frame(
            cached,
            employees=employees,
            dates=dates,
            templates=templates,
        )
    if persist:
        st.session_state[draft_key] = sanitized
    return sanitized, draft_shifts, cache_shifts


def _schedule_pending_mutations_key(period_id: str) -> str:
    return schedule_sess.pending_mutations_key(period_id)


def _load_pending_mutations(period_id: str) -> List[CellMutation]:
    return schedule_sess.load_pending_mutations(st.session_state, period_id)


def _save_pending_mutations(period_id: str, mutations: Sequence[CellMutation]) -> None:
    schedule_sess.save_pending_mutations(st.session_state, period_id, mutations)


def _enrich_employee_column_labels(
    frame: pd.DataFrame,
    *,
    employees: List[Dict],
    contract_rows: Dict[str, object],
) -> pd.DataFrame:
    display = frame.copy()
    employees_by_id = {str(employee["id"]): employee for employee in employees}
    labels: List[str] = []
    for _, row in display.iterrows():
        employee_id = str(row.get("employee_id", "") or "")
        if is_daily_tally_employee_id(employee_id):
            labels.append(str(row.get("Employee", "") or ""))
            continue
        employee = employees_by_id.get(employee_id, {})
        role_code = infer_role_code_from_employee(
            {**employee, "Employee": row.get("Employee", ""), "id": employee_id}
        )
        tracking = contract_rows.get(employee_id)
        target_hours = float(tracking.target_hours) if tracking is not None else None
        labels.append(
            format_schedule_employee_label(
                str(row.get("Employee", "") or ""),
                role_code=role_code,
                target_hours=target_hours,
            )
        )
    display["Employee"] = labels
    return display




def _update_schedule_period_status(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period_id: str,
    status: str,
) -> None:
    conn.execute(
        """
        UPDATE schedule_periods
        SET status = ?, updated_at = ?
        WHERE tenant_id = ? AND id = ?
        """,
        (status, datetime.now(timezone.utc).isoformat(), tenant_id, period_id),
    )
    conn.commit()


def _normalize_shift_cell(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip().upper()
    if text in ("", "—", "-", "OFF", "NONE", "NAN", "."):
        return ""
    if text in AVAILABILITY_OFF_CODES:
        return text
    if text == "AGY":
        return "AGY"
    if text in ("S", "SPECIMEN"):
        return "D"
    if text in ("D", "M", "E", "N"):
        return "D" if text == "M" else text
    short = text[:1] if text else ""
    if short == "M":
        return "D"
    return short if short in ALLOWED_SHIFT_CODES else ""


def _template_id_from_short(templates: Dict[str, Dict], short: str) -> Optional[str]:
    if not short:
        return None
    normalized = "D" if short == "M" else short
    for tid, tmpl in templates.items():
        tmpl_short = str(tmpl.get("short", "") or "")
        if tmpl_short == normalized or tmpl_short == short:
            return tid
        if normalized == "D" and tmpl.get("code") == "MORNING":
            return tid
    return None


def _shift_short_from_template_id(
    templates: Dict[str, Dict],
    template_id: Optional[str],
) -> str:
    if not template_id:
        return ""
    template = templates.get(str(template_id))
    if not template:
        return ""
    short = str(template.get("short", "") or "")
    if short == "M":
        return "D"
    return short


def _schedule_shift_cells_equal(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    employees: List[Dict],
    dates: List[date],
) -> bool:
    """Compare worked shift tokens only (ignores Employee label / metadata columns)."""

    left_rows = schedule_frame_row_index_by_employee_id(left)
    right_rows = schedule_frame_row_index_by_employee_id(right)
    for employee in employees:
        employee_id = str(employee["id"])
        left_idx = left_rows.get(employee_id)
        right_idx = right_rows.get(employee_id)
        if left_idx is None or right_idx is None:
            continue
        for day in dates:
            col = day.isoformat()
            if col not in left.columns or col not in right.columns:
                continue
            if _normalize_shift_cell(left.at[left_idx, col]) != _normalize_shift_cell(
                right.at[right_idx, col]
            ):
                return False
    return True


def _collect_schedule_frame_db_diffs(
    draft_frame: pd.DataFrame,
    *,
    employees: List[Dict],
    dates: List[date],
    templates: Dict[str, Dict],
    assignments: List[Dict],
) -> List[Tuple[str, date, str, str]]:
    """Return (employee_id, date, previous_short, new_short) for draft vs database."""

    desired = assignments_from_schedule_frame(
        draft_frame,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    desired_by_cell = {
        (shift.employee_id, shift.assignment_date): shift.shift_template_id for shift in desired
    }
    current_by_cell = {
        (assignment["employee_id"], assignment["assignment_date"]): assignment["shift_template_id"]
        for assignment in assignments
    }
    diffs: List[Tuple[str, date, str, str]] = []
    for employee in employees:
        employee_id = str(employee["id"])
        for day in dates:
            key = (employee_id, day)
            current_template_id = current_by_cell.get(key)
            desired_template_id = desired_by_cell.get(key)
            if current_template_id == desired_template_id:
                continue
            diffs.append(
                (
                    employee_id,
                    day,
                    _shift_short_from_template_id(templates, current_template_id),
                    _shift_short_from_template_id(templates, desired_template_id),
                )
            )
    return diffs


def _count_raw_grid_shift_cells_in_frame(
    draft_frame: pd.DataFrame,
    *,
    dates: List[date],
) -> int:
    """Count D/E/N tokens in the grid frame without template resolution."""

    if draft_frame.empty:
        return 0
    total = 0
    date_keys = [day.isoformat() for day in dates]
    for _, row in draft_frame.iterrows():
        employee_id = str(row.get("employee_id", "") or "")
        if not employee_id or is_daily_tally_employee_id(employee_id):
            continue
        for day_key in date_keys:
            if day_key not in draft_frame.columns:
                continue
            token = _normalize_shift_cell(row.get(day_key, ""))
            if token in {"D", "E", "N"}:
                total += 1
    return total


def _schedule_shift_cells_match(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    dates: List[date],
) -> bool:
    """True when both frames show the same D/E/N/off tokens for roster rows."""

    if left.empty or right.empty or "employee_id" not in left.columns or "employee_id" not in right.columns:
        return left.empty and right.empty
    date_keys = [day.isoformat() for day in dates]
    right_index = schedule_frame_row_index_by_employee_id(right)
    for row_idx, row in left.iterrows():
        employee_id = str(row.get("employee_id", "") or "")
        if not employee_id or is_daily_tally_employee_id(employee_id):
            continue
        right_idx = right_index.get(employee_id)
        if right_idx is None:
            return False
        for day_key in date_keys:
            if day_key not in left.columns or day_key not in right.columns:
                continue
            left_token = _normalize_shift_cell(left.at[row_idx, day_key])
            right_token = _normalize_shift_cell(right.at[right_idx, day_key])
            if left_token != right_token:
                return False
    return True


def _discard_stale_pending_when_draft_matches_db(
    *,
    period_id: str,
    draft: pd.DataFrame,
    baseline_from_db: pd.DataFrame,
    dates: List[date],
) -> None:
    """Drop orphaned pending edits when the visible draft already matches the database."""

    pending = _load_pending_mutations(period_id)
    if not pending:
        return
    if not _schedule_shift_cells_match(draft, baseline_from_db, dates=dates):
        return
    _save_pending_mutations(period_id, [])
    _clear_accumulated_grid_changes(period_id)
    _clear_grid_component_echo(period_id)
    _clear_grid_session_storage_bridge(period_id)
    st.session_state.pop(f"schedule_clear_confirm_{period_id}", None)


def _count_worked_shifts_in_frame(
    draft_frame: pd.DataFrame,
    *,
    employees: List[Dict],
    dates: List[date],
    templates: Dict[str, Dict],
) -> int:
    return len(
        assignments_from_schedule_frame(
            draft_frame,
            employees=employees,
            dates=dates,
            templates=templates,
        )
    )


def _publish_would_wipe_saved_schedule(
    *,
    diffs: Sequence[Tuple[str, date, str, str]],
    assignments: Sequence[Dict],
    draft_frame: pd.DataFrame,
    employees: List[Dict],
    dates: List[date],
    templates: Dict[str, Dict],
    min_saved_shifts: int = 8,
) -> bool:
    """True when publishing the draft would delete most saved shifts unexpectedly."""

    current_count = len(assignments)
    desired_count = _count_worked_shifts_in_frame(
        draft_frame,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    if current_count > 0 and desired_count == 0:
        return True
    if current_count < min_saved_shifts:
        return False
    delete_count = sum(1 for _, _, _, new_short in diffs if not new_short)
    if delete_count < max(1, current_count // 2):
        return False
    if desired_count <= max(1, current_count // 4):
        return True
    if (
        current_count >= 4
        and desired_count < int(current_count * 0.5)
        and delete_count >= max(4, current_count // 4)
    ):
        return True
    return False


def _maybe_complete_deferred_schedule_save(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    employee_target_hours: Dict[str, float],
    availability_blocked: Dict[str, Set[date]],
    manager_mode: bool,
) -> bool:
    del manager_mode
    return _complete_deferred_save(
        conn,
        st.session_state,
        period_id=period.id,
        tenant_id=tenant_id,
        period=period,
        rules=rules,
        employees=employees,
        templates=templates,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        standard_manual_save=_standard_manual_save,
        workspace_publish_notice_key=_workspace_publish_notice_key,
        set_publish_notice=lambda key, message: st.session_state.__setitem__(key, message),
        toast=st.toast,
        rerun=st.rerun,
    )


def _schedule_truth_frame_for_period(
    period_id: str,
    dates: List[date],
    *,
    fallback_frame: pd.DataFrame,
) -> pd.DataFrame:
    return schedule_truth_frame(
        st.session_state,
        period_id=period_id,
        dates=dates,
        fallback_frame=fallback_frame,
        sanitize=_sanitize_schedule_dataframe,
    )


def _collect_agency_placements(request_path: Path) -> List[Dict]:
    payload = load_agency_request(request_path)
    placements: List[Dict] = []
    for item in payload.get("line_items") or []:
        placements.extend(item.get("placements") or [])
    return placements


def _build_schedule_dataframe(
    employees: List[Dict],
    dates: List[date],
    assignments: List[Dict],
    templates: Dict[str, Dict],
    blocked_map: Optional[Dict[str, Dict[date, str]]] = None,
    *,
    include_daily_tallies: bool = False,
    agency_request_path: Optional[Path] = None,
) -> pd.DataFrame:
    blocked_map = blocked_map or {}
    sorted_employees = sorted(employees, key=portage_roster_sort_key)
    rows = build_schedule_export_rows(
        sorted_employees,
        dates,
        assignments,
        templates,
        blocked_map=blocked_map,
        off_code_for_reason=reason_to_off_code,
        include_daily_tallies=include_daily_tallies,
    )
    if agency_request_path is not None and agency_request_path.is_file():
        rows = apply_agency_placements_to_schedule_rows(
            rows,
            _collect_agency_placements(agency_request_path),
            dates,
        )
    return pd.DataFrame(rows)


def _assignments_to_scheduled(
    assignments: List[Dict], employees: List[Dict]
) -> List[ScheduledShift]:
    names = {e["id"]: e["full_name"] for e in employees}
    return [
        ScheduledShift(
            employee_id=a["employee_id"],
            employee_name=names.get(a["employee_id"], a["employee_id"]),
            assignment_date=a["assignment_date"],
            shift_template_id=a["shift_template_id"],
            approved_stretch=approved_stretch_from_system_note(a.get("system_note")),
            clinical_floor_stretch=bool(a.get("clinical_floor_stretch")),
        )
        for a in assignments
    ]


def _profiles_from_db(
    employees: List[Dict], emp_quals: Dict[str, Set[str]]
) -> List[EmployeeProfile]:
    return [
        EmployeeProfile(
            id=e["id"],
            full_name=e["full_name"],
            fte=float(e["fte"]),
            qualification_ids=emp_quals.get(e["id"], set()),
            seniority_hours=float(e.get("seniority_hours", 0.0)),
            base_hourly_rate=float(e.get("base_hourly_rate", 40.0)),
            contract_line_type=e.get("contract_line_type"),
        )
        for e in employees
    ]


def _apply_inline_cell_change(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    assignments: List[Dict],
    emp_quals: Dict[str, Set[str]],
    shift_quals: Dict[str, Set[str]],
    employee: Dict,
    assignment_date: date,
    new_short: str,
    previous_short: str,
    employee_target_hours: Optional[Dict[str, float]] = None,
    availability_blocked: Optional[Dict[str, Set[date]]] = None,
    enforce_assignment_rules: bool = True,
) -> Tuple[bool, str]:
    if availability_blocked and assignment_date in availability_blocked.get(employee["id"], set()):
        return False, "Approved time off — this cell is locked."

    if is_availability_off_code(new_short):
        return False, "Time-off is managed through availability records, not manual entry."

    template_info = _shift_templates_for_compliance(templates)
    profiles = {p.id: p for p in _profiles_from_db(employees, emp_quals)}
    profile = profiles[employee["id"]]
    scheduled = _assignments_to_scheduled(assignments, employees)

    new_shift_id = _template_id_from_short(templates, new_short)
    if new_short and new_shift_id is None:
        return False, f"Unknown shift code '{new_short}'."

    violation = None
    if enforce_assignment_rules:
        violation = validate_assignment_change(
            rules=rules,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
            weeks_in_period=period.week_count,
            employee=profile,
            all_assignments=scheduled,
            shift_templates=template_info,
            shift_required_qualifications=shift_quals,
            assignment_date=assignment_date,
            new_shift_template_id=new_shift_id,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
        )
    if violation:
        friendly = format_manual_assignment_warning(
            employee_name=employee["full_name"],
            contract_line_type=employee.get("contract_line_type"),
            assignment_date=assignment_date,
            shift_type=new_short or "—",
            violation=violation,
        )
        return False, friendly

    try:
        if new_shift_id is None:
            _delete_assignment(
                conn,
                tenant_id=tenant_id,
                employee_id=employee["id"],
                assignment_date=assignment_date,
            )
        else:
            _upsert_assignment(
                conn,
                tenant_id=tenant_id,
                schedule_period_id=period.id,
                employee_id=employee["id"],
                shift_template_id=new_shift_id,
                assignment_date=assignment_date,
            )
    except sqlite3.IntegrityError as exc:
        shift = templates[new_shift_id] if new_shift_id else {"id": "", "name": "", "code": ""}
        return False, _build_compliance_error_message(
            conn,
            exc,
            tenant_id=tenant_id,
            employee=employee,
            shift=shift,
            assignment_date=assignment_date,
            period=period,
        ).replace("**", "")

    log_manual_edit(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=period.id,
        employee_id=employee["id"],
        shift_date=assignment_date,
        previous_shift_code=previous_short,
        new_shift_code=new_short,
        actor=st.session_state.get("username", "manager"),
    )

    return True, ""




def _process_staged_grid_edits(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    draft_frame: pd.DataFrame,
    edited: pd.DataFrame,
    dates: List[date],
    blocked_map: Dict[str, Dict[date, str]],
    employee_target_hours: Dict[str, float],
    availability_blocked: Dict[str, Set[date]],
    locked_cells: Optional[Set[Tuple[str, date]]] = None,
) -> Tuple[PolicyViewModel, bool]:
    emp_quals = _fetch_employee_qualification_ids(conn, tenant_id)
    shift_quals = _fetch_shift_required_qualification_ids(conn, tenant_id)
    profiles = {profile.id: profile for profile in _profiles_from_db(employees, emp_quals)}
    template_info = _shift_templates_for_compliance(templates)
    errors_key = _schedule_errors_key(period.id)
    engine = SchedulePolicyEngine()
    display_target_hours = _fte_contract_target_hours(
        rules=rules,
        period=period,
        employees=employees,
        target_hours=employee_target_hours,
        emp_quals=emp_quals,
    )
    view_model, any_applied, _toast_messages = engine.apply_mutations(
        draft_frame=draft_frame,
        edited_frame=edited,
        employees=employees,
        dates=dates,
        templates=templates,
        template_info=template_info,
        shift_quals=shift_quals,
        rules=rules,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        weeks_in_period=period.week_count,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        blocked_map=blocked_map,
        pending_mutations=_load_pending_mutations(period.id),
        cell_errors=st.session_state.get(errors_key, {}),
        profiles_by_id=profiles,
        is_availability_off_code=is_availability_off_code,
        reason_to_off_code=reason_to_off_code,
        contract_target_hours=display_target_hours,
        locked_cells=locked_cells,
        enforce_assignment_rules=False,
    )
    st.session_state[errors_key] = view_model.cell_errors
    _save_pending_mutations(period.id, view_model.pending_mutations)
    return view_model, any_applied


def _publish_schedule_frame_to_db(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    draft_frame: pd.DataFrame,
    dates: List[date],
    employee_target_hours: Dict[str, float],
    availability_blocked: Dict[str, Set[date]],
    enforce_assignment_rules: bool = True,
) -> bool:
    """Persist the session draft grid to SQLite by diffing against current assignments."""

    errors_key = _schedule_errors_key(period.id)
    if st.session_state.get(errors_key):
        st.toast("Resolve grid validation errors before publishing.", icon="⚠️")
        return False

    assignments = _fetch_assignments(conn, tenant_id, period.id)
    diffs = _collect_schedule_frame_db_diffs(
        draft_frame,
        employees=employees,
        dates=dates,
        templates=templates,
        assignments=assignments,
    )
    if not diffs:
        return False

    draft_shifts = _count_worked_shifts_in_frame(
        draft_frame,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    intentional_clear = _is_intentional_clear_save(
        period.id,
        draft_shift_count=draft_shifts,
        db_shift_count=len(assignments),
    )
    if intentional_clear and draft_shifts == 0 and assignments:
        _create_system_snapshot(f"pre-clear-publish-{period.id}")
        deleted = _delete_all_period_assignments(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=period.id,
        )
        _update_schedule_period_status(
            conn,
            tenant_id=tenant_id,
            period_id=period.id,
            status="published",
        )
        _save_pending_mutations(period.id, [])
        _bump_schedule_grid_staging_revision(period.id)
        _invalidate_schedule_matrix_view_cache(period.id)
        st.session_state[_schedule_ignore_grid_echo_key(period.id)] = True
        draft_key = _schedule_draft_key(period.id)
        baseline_key = _schedule_baseline_key(period.id)
        published_draft = _sanitize_schedule_dataframe(draft_frame.copy(), dates)
        st.session_state[draft_key] = published_draft
        st.session_state[baseline_key] = published_draft
        _clear_intentional_clear_save_flag(period.id)
        st.session_state[f"schedule_sync_{period.id}"] = True
        return True

    wipe_risk = _publish_would_wipe_saved_schedule(
        diffs=diffs,
        assignments=assignments,
        draft_frame=draft_frame,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    if wipe_risk and not intentional_clear:
        st.toast(
            "Save blocked: the session draft looks empty but the database still has shifts. "
            "Make an edit on the grid, then save again.",
            icon="🛑",
        )
        st.session_state["roster_error"] = (
            "Save blocked to prevent clearing the saved schedule. "
            "Edit the grid once, then click Save again."
        )
        return False

    _create_system_snapshot(f"pre-publish-{period.id}")
    emp_quals = _fetch_employee_qualification_ids(conn, tenant_id)
    shift_quals = _fetch_shift_required_qualification_ids(conn, tenant_id)
    employee_by_id = {employee["id"]: employee for employee in employees}
    publish_errors: List[str] = []
    applied = 0

    for employee_id, assignment_date, previous_short, new_short in diffs:
        employee = employee_by_id.get(employee_id)
        if employee is None:
            continue
        ok, message = _apply_inline_cell_change(
            conn,
            tenant_id=tenant_id,
            period=period,
            rules=rules,
            employees=employees,
            templates=templates,
            assignments=assignments,
            emp_quals=emp_quals,
            shift_quals=shift_quals,
            employee=employee,
            assignment_date=assignment_date,
            new_short=new_short,
            previous_short=previous_short,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            enforce_assignment_rules=enforce_assignment_rules,
        )
        if ok:
            applied += 1
            assignments = _fetch_assignments(conn, tenant_id, period.id)
        elif message:
            publish_errors.append(message)

    if publish_errors:
        st.session_state["roster_error"] = "; ".join(publish_errors[:3])
        return False
    if applied <= 0:
        st.toast("No staged grid changes were applied.", icon="ℹ️")
        return False

    _update_schedule_period_status(
        conn,
        tenant_id=tenant_id,
        period_id=period.id,
        status="published",
    )
    _save_pending_mutations(period.id, [])
    _bump_schedule_grid_staging_revision(period.id)
    _invalidate_schedule_matrix_view_cache(period.id)
    st.session_state[_schedule_ignore_grid_echo_key(period.id)] = True
    draft_key = _schedule_draft_key(period.id)
    baseline_key = _schedule_baseline_key(period.id)
    published_draft = _sanitize_schedule_dataframe(draft_frame.copy(), dates)
    st.session_state[draft_key] = published_draft
    st.session_state[baseline_key] = published_draft
    return True


def _publish_schedule_draft(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    employee_target_hours: Dict[str, float],
    availability_blocked: Dict[str, Set[date]],
    enforce_assignment_rules: bool = True,
) -> bool:
    draft_key = _schedule_draft_key(period.id)
    draft_frame = st.session_state.get(draft_key)
    if draft_frame is None:
        st.toast("No schedule draft to publish.", icon="ℹ️")
        return False
    dates = list(_daterange(period.period_start, period.period_end_inclusive))
    return _publish_schedule_frame_to_db(
        conn,
        tenant_id=tenant_id,
        period=period,
        rules=rules,
        employees=employees,
        templates=templates,
        draft_frame=draft_frame,
        dates=dates,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        enforce_assignment_rules=enforce_assignment_rules,
    )


def _standard_json_backup_path(period_id: str) -> Path:
    return SAVED_SCHEDULES_DIR / f"{period_id}-manual.json"


def _write_standard_json_backup(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
) -> Path:
    payload = export_period_schedule(
        conn,
        tenant_id=tenant_id,
        period_id=period.id,
        name=f"{period.id}-manual",
        description=f"Standard manual save for {period.name}",
    )
    destination = _standard_json_backup_path(period.id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return destination


def _save_button_label(period_id: str) -> str:
    pending_count = len(_load_pending_mutations(period_id))
    return f"Save ({pending_count})" if pending_count else "Save"


def _standard_manual_save(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    employee_target_hours: Dict[str, float],
    availability_blocked: Dict[str, Set[date]],
) -> Tuple[bool, str]:
    """
    Standard save for manual editing:
    1. Flush staged grid edits to the database (when any are pending)
    2. Refresh the standard JSON backup at saved_schedules/<period>-manual.json
    """

    pending_count = len(_load_pending_mutations(period.id))
    database_updated = False

    draft_key = _schedule_draft_key(period.id)
    dates = list(_daterange(period.period_start, period.period_end_inclusive))
    prepared_draft: Optional[pd.DataFrame] = None
    intentional_clear = False
    if st.session_state.get(draft_key) is not None:
        prepared_draft, draft_shift_count, cache_shift_count = _prepare_draft_for_save(
            period.id,
            dates=dates,
            employees=employees,
            templates=templates,
            persist=False,
        )
        db_assignments = _fetch_assignments(conn, tenant_id, period.id)
        db_shift_count = len(db_assignments)
        intentional_clear = _is_intentional_clear_save(
            period.id,
            draft_shift_count=draft_shift_count,
            db_shift_count=db_shift_count,
            cache_shift_count=cache_shift_count,
        )
        if draft_shift_count == 0 and (cache_shift_count > 0 or pending_count > 0):
            if not intentional_clear or cache_shift_count > 0:
                message = (
                    "Save blocked: grid edits have not synced yet. "
                    "Change one cell, wait for row hours to update, then Save again."
                )
                st.session_state["roster_error"] = message
                return False, message
        if draft_shift_count == 0 and db_assignments:
            if not intentional_clear:
                message = (
                    "Save blocked: the session draft is empty but the database still has shifts. "
                    "Make an edit on the grid, wait for row hours to update, then Save again."
                )
                st.session_state["roster_error"] = message
                return False, message
        if draft_shift_count == 0:
            if not intentional_clear:
                message = (
                    "Nothing to save yet — assign shifts on the grid first. "
                    "Row hours (e.g. 80/320h) should update before you Save."
                )
                st.session_state["roster_error"] = message
                return False, message

    if prepared_draft is not None:
        preflight_draft = prepared_draft
        if isinstance(preflight_draft, pd.DataFrame):
            db_assignments = _fetch_assignments(conn, tenant_id, period.id)
            db_shift_count = len(db_assignments)
            draft_shift_count = _count_worked_shifts_in_frame(
                preflight_draft,
                employees=employees,
                dates=dates,
                templates=templates,
            )
            if (
                not intentional_clear
                and db_shift_count >= 8
                and draft_shift_count < max(1, db_shift_count // 2)
            ):
                st.session_state["roster_error"] = (
                    "Save blocked: the session draft is missing most saved shifts. "
                    "Make an edit on the grid, wait for it to update, then save again."
                )
                return False, str(st.session_state["roster_error"])
        original_draft = st.session_state.get(draft_key)
        st.session_state[draft_key] = preflight_draft

        database_updated = _publish_schedule_draft(
            conn,
            tenant_id=tenant_id,
            period=period,
            rules=rules,
            employees=employees,
            templates=templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            enforce_assignment_rules=False,
        )
        if st.session_state.get("roster_error") or not database_updated:
            if isinstance(original_draft, pd.DataFrame):
                st.session_state[draft_key] = original_draft
        if st.session_state.get("roster_error"):
            return False, str(st.session_state.get("roster_error") or "Save failed.")
    elif pending_count:
        return False, "Reload the schedule workspace before saving."
    elif st.session_state.get(_schedule_errors_key(period.id)):
        return False, "Resolve grid validation errors before saving."

    if not database_updated and pending_count > 0:
        return (
            False,
            "Save failed: staged grid edits were not written. Edit a cell, wait for the "
            "grid to update, then click Save again.",
        )

    try:
        json_path = _write_standard_json_backup(
            conn,
            tenant_id=tenant_id,
            period=period,
        )
    except ScheduleArchiveError as exc:
        if database_updated:
            st.toast(f"Saved to database; JSON backup failed: {exc}", icon="⚠️")
            return True, "Saved to database (JSON backup failed)."
        return False, str(exc)

    if database_updated:
        _clear_intentional_clear_save_flag(period.id)
        st.session_state[f"schedule_sync_{period.id}"] = True
        _clear_grid_component_echo(period.id)
        _clear_grid_session_storage_bridge(period.id)
        _invalidate_schedule_matrix_view_cache(period.id)
        _clear_accumulated_grid_changes(period.id)
        st.session_state.pop(f"schedule_browser_store_scrubbed_{period.id}", None)
        return True, f"Saved to database and `{json_path.name}`."
    return (
        False,
        "Nothing to save — the grid already matches the database. "
        "Edit a cell (watch row hours update), then Save again.",
    )


def _finish_pending_workspace_save(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    draft_key: str,
    baseline_key: str,
    dates: List[date],
    assignments: Sequence[Dict],
    blocked_map: Dict[str, Dict[date, str]],
    target_hours: Mapping[str, float],
    blocked_sets: Dict[str, Set[date]],
    grid_cell_changes_count: int = 0,
    only_intentional_clear: bool = False,
) -> bool:
    """Persist a deferred save. Use only_intentional_clear for the pre-metadata fast path."""

    if not schedule_sess.peek_save_requested(st.session_state, period.id):
        return False

    draft_key_frame = st.session_state.get(draft_key)
    if draft_key_frame is None:
        return False
    draft_probe = _sanitize_schedule_dataframe(
        draft_key_frame.copy() if isinstance(draft_key_frame, pd.DataFrame) else draft_key_frame,
        dates,
    )
    draft_shifts_probe = _count_worked_shifts_in_frame(
        draft_probe,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    db_assignments_probe = _fetch_assignments(conn, tenant_id, period.id)
    intentional_clear = _is_intentional_clear_save(
        period.id,
        draft_shift_count=draft_shifts_probe,
        db_shift_count=len(db_assignments_probe),
    )
    if only_intentional_clear:
        if not intentional_clear:
            return False

    _prepare_draft_for_save(
        period.id,
        dates=dates,
        employees=employees,
        templates=templates,
        persist=True,
    )
    draft = _sanitize_schedule_dataframe(st.session_state[draft_key].copy(), dates)
    draft_shifts = _count_worked_shifts_in_frame(
        draft,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    db_assignments = _fetch_assignments(conn, tenant_id, period.id)

    edits_already_merged = draft_shifts > 0 and not intentional_clear
    if not intentional_clear and not edits_already_merged:
        _flush_session_storage_grid_edits_before_save(
            conn,
            tenant_id=tenant_id,
            period=period,
            rules=rules,
            employees=employees,
            templates=templates,
            draft_key=draft_key,
            baseline_key=baseline_key,
            dates=dates,
            blocked_map=blocked_map,
            target_hours=target_hours,
            blocked_sets=blocked_sets,
        )
        draft = _sanitize_schedule_dataframe(st.session_state[draft_key].copy(), dates)
        draft_shifts = _count_worked_shifts_in_frame(
            draft,
            employees=employees,
            dates=dates,
            templates=templates,
        )

    schedule_sess.pop_save_requested(st.session_state, period.id)
    try:
        saved, message = _standard_manual_save(
            conn,
            tenant_id=tenant_id,
            period=period,
            rules=rules,
            employees=employees,
            templates=templates,
            employee_target_hours=dict(target_hours),
            availability_blocked=blocked_sets,
        )
    except Exception as exc:
        st.toast(f"Save failed: {exc}", icon="⚠️")
        return False

    if saved and message:
        st.session_state[_workspace_publish_notice_key(period.id)] = message
        st.toast(message.replace("`", ""), icon="✅")
        _clear_accumulated_grid_changes(period.id)
        _clear_grid_component_echo(period.id)
        _clear_grid_session_storage_bridge(period.id)
        _bump_schedule_grid_staging_revision(period.id)
        st.rerun()
        return True
    if not saved:
        st.toast(
            message or "Save failed — resolve grid errors and try again.",
            icon="⚠️",
        )
    return False








def _render_cell_error_notices(period_id: str) -> None:
    errors: Dict[str, str] = st.session_state.get(_schedule_errors_key(period_id), {})
    if not errors:
        return
    st.markdown("##### Needs attention")
    for cell_key, message in list(errors.items())[:6]:
        label = cell_key.replace("|", " · ")
        if is_critical_contract_line_violation(message):
            variant = "error"
            title = "CRITICAL · Contract Line Violation"
        elif is_transition_burnout_violation(message):
            variant = "warn"
            title = "Transition Burnout Warning"
        else:
            variant = "warn"
            title = label
        st.markdown(
            _html_micro_banner(message, variant, title=title, escape_message=False),
            unsafe_allow_html=True,
        )




def _render_jurisdiction_banner(rules: JurisdictionRules) -> None:
    daily = (
        f"{rules.daily_overtime_threshold_hours:.0f}h/day"
        if rules.daily_overtime_threshold_hours
        else "No daily OT"
    )
    between = (
        f"{rules.min_rest_between_shifts_hours:.0f}h between shifts"
        if rules.min_rest_between_shifts_hours
        else "—"
    )
    daily_rest = (
        f"{rules.min_daily_rest_hours:.0f}h daily rest"
        if rules.min_daily_rest_hours
        else "—"
    )
    st.sidebar.markdown("**Active labor rules**")
    st.sidebar.caption(rules.citation_label)
    st.sidebar.markdown(
        f"- **Overtime:** {daily}; **{rules.weekly_overtime_threshold_hours:.0f}h/week** @ {rules.overtime_rate_multiplier:.1f}×\n"
        f"- **Weekly rest:** {rules.min_weekly_rest_hours:.0f}h minimum\n"
        f"- **Max consecutive days:** {rules.max_consecutive_work_days}\n"
        f"- **Between shifts:** {between}\n"
        f"- **Daily rest:** {daily_rest}\n"
        f"- **FTE baseline:** {rules.standard_hours_per_week_at_1_0_fte:.0f}h/week"
    )


def _build_audit_export_bundle(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    assignments: List[Dict],
    templates: Dict[str, Dict],
    compliance_report: ComplianceReport,
) -> Tuple[ComplianceAuditSummary, str, TenantMetadata]:
    emp_quals = _fetch_employee_qualification_ids(conn, tenant_id)
    shift_quals = _fetch_shift_required_qualification_ids(conn, tenant_id)
    template_info = _shift_templates_for_compliance(templates)
    scheduled = _assignments_to_scheduled(assignments, employees)
    profiles = _profiles_from_db(employees, emp_quals)
    tenant = _fetch_tenant_metadata(conn, tenant_id)

    audit_summary, html_doc = generate_audit_export(
        tenant=tenant,
        period_id=period.id,
        period_name=period.name,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        week_count=period.week_count,
        rules=rules,
        compliance_report=compliance_report,
        assignments=scheduled,
        shift_templates=template_info,
        shift_required_qualifications=shift_quals,
        employees=profiles,
    )
    return audit_summary, html_doc, tenant




def _build_union_compliance_report_bundle(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    tenant_name: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    assignments: List[Dict],
    compliance_report: ComplianceReport,
) -> Tuple[UnionComplianceReport, str]:
    scheduled = _assignments_to_scheduled(assignments, employees)
    return generate_union_compliance_report(
        conn,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        period_id=period.id,
        period_name=period.name,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        rules=rules,
        compliance_report=compliance_report,
        assignments=scheduled,
    )




def _render_union_compliance_report_section(
    union_report: UnionComplianceReport,
    union_html: str,
    *,
    tenant_meta: TenantMetadata,
    period: TenantPeriod,
) -> None:
    st.markdown("### Union-Compliance Audit Report")
    st.caption(
        "Information-only export for union representatives or department heads. "
        "Discloses Break-Glass overrides and confirms CBA seniority and rest-window alignment."
    )
    if union_report.overall_legal_alignment:
        st.success(
            "All active shifts align with statutory rest-window rules. "
            "CBA seniority events are documented below."
        )
    else:
        st.warning(
            f"{union_report.rest_window_error_count} rest-window error(s) require review "
            "before handing this report to a union rep."
        )

    c1, c2, c3 = st.columns(3)
    c1.metric("Total shifts managed", union_report.total_shifts_managed)
    c2.metric("Break-Glass overrides", len(union_report.break_glass_events))
    c3.metric("Seniority bypass events", union_report.seniority_bypass_count)

    file_stamp = union_report.generated_at_utc[:10].replace("-", "")
    st.download_button(
        label="Generate Compliance Report",
        data=union_html.encode("utf-8"),
        file_name=(
            f"union-compliance-report_{tenant_meta.slug}_{period.id}_{file_stamp}.html"
        ),
        mime="text/html",
        type="primary",
        width="stretch",
        help=(
            "Download a printable HTML summary. Open in a browser and use "
            "Print → Save as PDF for a PDF copy."
        ),
    )
    st.caption(
        f"Report ID `{union_report.report_id}` · "
        f"integrity hash `{union_report.content_hash[:16]}…` · "
        "No billing, licensing, or SaaS lock."
    )


def _render_audit_export_status(
    audit_summary: ComplianceAuditSummary,
    *,
    gates: Optional[FeatureGates] = None,
    manager_mode: bool = False,
) -> None:
    if manager_mode:
        return
    if gates is not None and gates.is_trial_tier:
        return
    coverage = audit_summary.coverage
    if coverage.is_empty:
        st.info(
            "No shifts are scheduled for this period. The export will document zero coverage, "
            "baseline FTE contracts, and jurisdiction rules evaluated."
        )
    elif coverage.is_partial:
        st.warning(
            f"Schedule is **{coverage.coverage_pct:.1f}%** filled "
            f"({coverage.filled_slots}/{coverage.total_shift_slots} shift slots). "
            "The export includes coverage gaps and deflected-violation estimates."
        )
    else:
        st.success(
            f"Schedule coverage complete ({coverage.filled_slots}/{coverage.total_shift_slots} slots). "
            "Compliance audit export is ready."
        )


def _render_schedule_status_bar(
    *,
    tenant_id: str,
    gates: FeatureGates,
    audit_summary: ComplianceAuditSummary,
    gap_count: int,
    period: TenantPeriod,
    posting_readiness: Optional[SchedulePostingReadiness] = None,
) -> None:
    if st.session_state.get("compliance_error"):
        st.markdown(
            _html_micro_banner(
                st.session_state["compliance_error"],
                "danger",
                title="Schedule blocked",
                escape_message=False,
            ),
            unsafe_allow_html=True,
        )
        return

    coverage = audit_summary.coverage
    fill_pct = coverage.coverage_pct if not coverage.is_empty else 0.0
    trial_weeks = gates.trial_week_cap or TRIAL_MAX_WEEKS

    if gates.is_premium and posting_readiness is not None:
        if posting_readiness.is_ready:
            title = "Schedule ready"
            kind = "success"
            primary = "Coverage, contract hours, and night tallies passed for this block."
        else:
            title = "Schedule needs attention"
            kind = "warn"
            bullet_html = "<br>".join(
                f"• {html_lib.escape(item)}" for item in posting_readiness.attention_bullets
            )
            primary = (
                f"**{fill_pct:.0f}%** filled · **{gap_count}** open gap(s)."
                + (f"<br>{bullet_html}" if bullet_html else "")
            )
    else:
        needs_attention = coverage.is_partial or gap_count > 0

        if gates.is_premium and needs_attention:
            title = "Schedule needs attention"
            kind = "warn"
            primary = (
                f"**{fill_pct:.0f}%** filled · **{gap_count}** open gap(s) · "
                "review gaps before breakroom posting."
            )
        elif not needs_attention:
            title = "Schedule ready"
            kind = "success"
            primary = (
                "Coverage and compliance checks passed for this block."
                if gates.is_premium
                else "Your trial preview is ready to review."
            )
        elif gap_count > 0 or coverage.is_partial:
            title = "Trial preview with gaps"
            kind = "warn"
            primary = (
                f"Preview filled **{fill_pct:.0f}%** of shift slots · "
                f"**{gap_count}** open gap(s) · **{trial_weeks}-week** trial window."
            )
        else:
            title = "Schedule status"
            kind = "info"
            primary = (
                f"**{fill_pct:.0f}%** filled · **{gap_count}** gap(s) · "
                f"**{trial_weeks}-week** trial window."
            )

    st.markdown(
        _html_micro_banner(primary, kind, title=title, escape_message=False),
        unsafe_allow_html=True,
    )

    if _is_self_serve_trial(tenant_id, gates):
        action_cols = st.columns([1, 1, 2])
        if action_cols[0].button(
            "View schedule grid",
            key=f"status_view_grid_{period.id}",
            width="stretch",
        ):
            st.session_state["scroll_to_schedule_grid"] = period.id
            st.rerun()
        if action_cols[1].button(
            f"Upgrade — {PREMIUM_PRICE_DISPLAY}",
            key=f"status_upgrade_{period.id}",
            width="stretch",
        ):
            st.session_state[schedule_sess.billing_checkout_trigger_key(tenant_id)] = True
            st.rerun()


def _render_audit_export_sidebar_button(
    audit_summary: ComplianceAuditSummary,
    html_doc: str,
    tenant: TenantMetadata,
    period: TenantPeriod,
    gates: FeatureGates,
) -> None:
    st.sidebar.markdown("---")
    st.sidebar.markdown("##### Compliance audit")
    st.sidebar.caption(
        "Generate an unalterable, printable audit log for union and director review."
    )
    if not gates.can_export_compliance_report:
        st.sidebar.markdown(
            """
            <div class="lab-premium-lock">
              <strong>Premium export</strong><br>
              Upgrade to Premium to export union compliance audits for director attestation.
            </div>
            """,
            unsafe_allow_html=True,
        )
        if _is_self_serve_trial(tenant.id, gates):
            st.sidebar.caption("Premium unlocks audit export.")
        else:
            st.sidebar.caption(
                f"{audit_summary.deflected.total_deflected} deflected · "
                f"{audit_summary.active_error_count} error(s) · "
                f"{audit_summary.active_warning_count} warning(s) · export locked on trial"
            )
        return

    file_stamp = audit_summary.generated_at_utc[:10].replace("-", "")
    st.sidebar.download_button(
        label="📄 Export Union Compliance Report",
        data=html_doc.encode("utf-8"),
        file_name=f"union-compliance-audit_{tenant.slug}_{period.id}_{file_stamp}.html",
        mime="text/html",
        width="stretch",
        help=(
            "Download a printable, system-generated audit log with jurisdiction rules, "
            "deflected violations, FTE reconciliation, and director attestation."
        ),
    )
    st.sidebar.caption(
        f"{audit_summary.deflected.total_deflected} deflected · "
        f"{audit_summary.active_error_count} error(s) · "
        f"{audit_summary.active_warning_count} warning(s)"
    )


def _render_live_compliance_alerts(
    report: ComplianceReport,
    rules: JurisdictionRules,
    *,
    max_visible: int = 3,
    show_details: bool = True,
) -> None:
    if not report.violations:
        st.markdown(
            _html_micro_banner(
                f"No statutory scheduling violations under {rules.display_name} rules for this period.",
                "success",
                title="Compliance clear",
            ),
            unsafe_allow_html=True,
        )
        return

    errors = sum(1 for v in report.violations if v.severity == "error")
    warnings = len(report.violations) - errors
    summary_bits = [f"{len(report.violations)} alert(s)"]
    if errors:
        summary_bits.append(f"{errors} blocking")
    if warnings:
        summary_bits.append(f"{warnings} warning(s)")

    st.markdown(
        _html_micro_banner(
            "Statutory engine flags — separate from database qualification blocks.",
            "warn",
            title=f"{rules.display_name} · {' · '.join(summary_bits)}",
        ),
        unsafe_allow_html=True,
    )

    if not show_details:
        return

    banners: List[str] = []
    for v in report.violations[:max_visible]:
        variant = "error" if v.severity == "error" else "warn"
        badge = _html_badge(v.severity.upper(), "danger" if variant == "error" else "warn")
        msg = html_lib.escape(f"{v.employee_name}: {v.message}")
        banners.append(
            f'<div class="lab-micro-banner lab-micro-{variant}" style="margin:4px 0;">'
            f'<span style="margin-right:8px;">{badge}</span>{msg}</div>'
        )
    st.markdown("".join(banners), unsafe_allow_html=True)

    if len(report.violations) > max_visible:
        with st.expander(
            f"Show {len(report.violations) - max_visible} more alert(s)",
            expanded=False,
        ):
            extra_rows = []
            for v in report.violations[max_visible:]:
                extra_rows.append(
                    {
                        "Severity": v.severity,
                        "Employee": v.employee_name,
                        "Message": v.message,
                    }
                )
            st.dataframe(extra_rows, width="stretch", hide_index=True)


def _build_schedule_csv_bytes(
    employees: List[Dict],
    dates: List[date],
    assignments: List[Dict],
    templates: Dict[str, Dict],
    blocked_map: Optional[Dict[str, Dict[date, str]]] = None,
) -> bytes:
    frame = _build_schedule_dataframe(
        employees,
        dates,
        assignments,
        templates,
        blocked_map=blocked_map,
        include_daily_tallies=True,
    )
    return frame.to_csv(index=False).encode("utf-8")


def _shift_hours_by_editor_token(templates: Dict[str, Dict]) -> Dict[str, float]:
    hours_by_token: Dict[str, float] = {}
    for token in ("D", "E", "N"):
        template_id = _template_id_from_short(templates, token)
        if template_id is None:
            continue
        hours_by_token[token] = float(templates[template_id]["duration_minutes"]) / 60.0
    return hours_by_token


def _contract_hours_deficit(
    schedule_frame: pd.DataFrame,
    *,
    employees: List[Dict],
    dates: List[date],
    templates: Dict[str, Dict],
    employee_target_hours: Dict[str, float],
    schedule_archetype: str = "STANDARD",
) -> Tuple[float, float, float]:
    """Return (contractual_needed, scheduled_actual, net_delta). Delta negative = under-scheduled."""

    contractual_needed = sum(
        float(employee_target_hours.get(employee["id"], 0.0)) for employee in employees
    )
    hours_by_token = _shift_hours_by_editor_token(templates)
    paid_hours = paid_hours_per_shift(schedule_archetype=schedule_archetype)
    date_keys = [day.isoformat() for day in dates]
    is_twelve_hour = schedule_archetype == ScheduleArchetype.TWELVE_HOUR.value
    scheduled_actual = 0.0
    for _, row in schedule_frame.iterrows():
        employee_id = row.get("employee_id")
        if is_daily_tally_employee_id(employee_id):
            continue
        if is_twelve_hour:
            # Mirror the breakroom contract-tracking row: worked 12-hour tours are paid at
            # the 11.625h coefficient, and a single "T" top-up token closes the structural
            # FTE deficit to exactly the line's contract target.
            worked = 0
            has_topup = False
            for day_key in date_keys:
                token = str(row.get(day_key, "") or "").strip().upper()
                if token in WORKED_SHIFT_TOKENS:
                    worked += 1
                elif token == FTE_TOPUP_TOKEN:
                    has_topup = True
            line_actual = worked * paid_hours
            line_target = float(employee_target_hours.get(employee_id, 0.0))
            if has_topup and line_actual < line_target:
                line_actual = line_target
            scheduled_actual += line_actual
        else:
            for day_key in date_keys:
                token = normalize_grid_shift_token(row.get(day_key, ""))
                if token in hours_by_token:
                    scheduled_actual += hours_by_token[token]
    return contractual_needed, scheduled_actual, scheduled_actual - contractual_needed


def _count_open_shift_gaps_for_view(
    draft_frame: pd.DataFrame,
    *,
    view_dates: Sequence[date],
    employees: List[Dict],
    dates: List[date],
    db_templates: Dict[str, Dict],
    shift_templates: Dict[str, ShiftTemplateInfo],
    schedule_archetype: str,
) -> int:
    """Facility gaps for the visible week block only (used for live ribbon updates)."""

    if not view_dates:
        return 0
    from lab_scheduler.engine.manager_dashboard import count_open_shift_gaps

    view_set = set(view_dates)
    scheduled = assignments_from_schedule_frame(
        draft_frame,
        employees=employees,
        dates=dates,
        templates=db_templates,
    )
    filtered = [
        shift for shift in scheduled if shift.assignment_date in view_set
    ]
    return count_open_shift_gaps(
        period_start=min(view_dates),
        period_end=max(view_dates),
        shift_templates=shift_templates,
        assignments=filtered,
        schedule_archetype=schedule_archetype,
    )


def _ops_metrics_config_json(
    *,
    contract_target_total: float,
    full_gap_count: int,
    visible_gap_count: int,
) -> str:
    return json.dumps(
        {
            "contractTargetTotal": contract_target_total,
            "fullGapCount": full_gap_count,
            "visibleGapBaseline": visible_gap_count,
        }
    )


def _process_inline_metadata_edits(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    roster_by_id: Dict[str, Dict],
    baseline: pd.DataFrame,
    edited: pd.DataFrame,
) -> bool:
    """Apply inline contract/FTE edits and run lifecycle vacate cascades."""

    manager_id = st.session_state.get("account_id", st.session_state.get("username", "manager"))
    standard_hours = rules.standard_hours_per_week_at_1_0_fte
    weekly_hour_options = list(bulk_target_weekly_hours_options(standard_hours))
    any_applied = False

    for row_idx in range(len(edited)):
        employee_id = str(edited.at[row_idx, "employee_id"])
        if is_daily_tally_employee_id(employee_id):
            continue
        old_contract = str(baseline.at[row_idx, "contract_line_type"] or "").strip()
        new_contract = str(edited.at[row_idx, "contract_line_type"] or "").strip()
        old_fte = float(baseline.at[row_idx, "fte"] or 0.0)
        new_fte = float(edited.at[row_idx, "fte"] or 0.0)
        if old_contract == new_contract and abs(old_fte - new_fte) < 0.001:
            continue
        if new_contract not in CONTRACT_LINE_TYPES:
            st.toast(f"Invalid contract line for row {row_idx + 1}.", icon="⚠️")
            continue

        roster_row = roster_by_id.get(employee_id, {})
        target_weekly = min(
            weekly_hour_options,
            key=lambda hours: abs(hours - new_fte * standard_hours),
        )
        try:
            result = update_employee_roster_line(
                conn,
                tenant_id=tenant_id,
                employee_id=employee_id,
                manager_id=str(manager_id),
                seniority_hours=float(roster_row.get("seniority_hours", 0.0)),
                contract_line_type=new_contract,
                target_weekly_hours=float(target_weekly),
                standard_weekly_hours=standard_hours,
            )
        except StaffLifecycleError as exc:
            st.session_state["roster_error"] = str(exc)
            st.toast(str(exc), icon="⚠️")
            continue

        vacated_note = ""
        if result.shifts_vacated_count:
            vacated_note = (
                f" · {result.shifts_vacated_count} shift(s) vacated after contract change"
            )
        st.toast(
            f"Updated {result.employee_name}: {result.new_contract_line}, "
            f"{result.fte:.1f} FTE{vacated_note}",
            icon="✅",
        )
        any_applied = True

    if any_applied:
        st.session_state[f"schedule_sync_{period.id}"] = True
    return any_applied




def _provisional_override_resolved(system_note: str) -> bool:
    return approved_stretch_from_system_note(
        system_note
    ) or is_approved_contract_line_exception_note(system_note)






def _render_breakroom_pdf_controls(
    *,
    facility_name: str,
    period: TenantPeriod,
    employees: List[Dict],
    dates: List[date],
    assignments: List[Dict],
    templates: Dict[str, Dict],
    blocked_map: Optional[Dict[str, Dict[date, str]]] = None,
    export_allowed: bool = True,
    rules: Optional[JurisdictionRules] = None,
    qual_codes: Optional[Dict[str, str]] = None,
    qual_ids_by_employee: Optional[Dict[str, Set[str]]] = None,
) -> None:
    if not export_allowed:
        st.info("Breakroom export blocked until tallies and contract-hour checks pass.")
        return
    breakroom_html = _build_breakroom_print_document(
        facility_name=facility_name,
        period=period,
        employees=employees,
        dates=dates,
        assignments=assignments,
        templates=templates,
        blocked_map=blocked_map,
        rules=rules,
        qual_codes=qual_codes,
        qual_ids_by_employee=qual_ids_by_employee,
    )
    st.markdown('<div class="lab-breakroom-toolbar lab-no-print">', unsafe_allow_html=True)
    st.download_button(
        label="Generate Breakroom PDF",
        data=breakroom_html.encode("utf-8"),
        file_name=f"breakroom_schedule_{period.id}.html",
        mime="text/html",
        width="stretch",
        key=f"breakroom_pdf_{period.id}",
        help="Download a high-contrast Legal/Ledger landscape layout optimized for hospital breakroom posting.",
    )
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown(
        f'<div class="lab-breakroom-print-host">{breakroom_html}</div>',
        unsafe_allow_html=True,
    )


def _refresh_workspace_ops_ribbon(
    ribbon_slot: Any,
    *,
    conn: sqlite3.Connection,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    gates: FeatureGates,
    facility_name: str,
    export_employees: List[Dict],
    dates: List[date],
    assignments: List[Dict],
    blocked_map: Dict[str, Dict[date, str]],
    blocked_sets: Dict[str, Set[date]],
    draft_frame: pd.DataFrame,
    employee_target_hours: Dict[str, float],
    display_target_hours: Dict[str, float],
    schedule_archetype: str,
    qual_codes: Dict[str, str],
    emp_quals: Dict[str, Set[str]],
    manager_mode: bool,
    publish_state: Optional[Mapping[str, object]],
    posting_readiness: Optional[SchedulePostingReadiness],
    template_info: Dict[str, ShiftTemplateInfo],
) -> None:
    """Render ops ribbon metrics from the current draft (includes unsaved grid edits)."""

    live_policy_view = live_policy_view_from_draft(
        draft_frame,
        employees=employees,
        dates=dates,
        week_count=period.week_count,
        pending_mutations=_load_pending_mutations(period.id),
        cell_errors=st.session_state.get(_schedule_errors_key(period.id), {}),
        schedule_archetype=schedule_archetype,
        contract_target_hours=display_target_hours,
    )
    live_gap_count = live_gap_count_from_draft(
        draft_frame,
        employees=employees,
        dates=dates,
        templates=templates,
        template_info=template_info,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        schedule_archetype=schedule_archetype,
    )
    refresh_ops_ribbon_slot(
        ribbon_slot,
        draft_frame=draft_frame,
        render_ribbon=_render_operational_ribbon,
        gap_count=live_gap_count,
        policy_view=live_policy_view,
        ribbon_kwargs={
            "conn": conn,
            "tenant_id": tenant_id,
            "period": period,
            "rules": rules,
            "employees": employees,
            "templates": templates,
            "gates": gates,
            "facility_name": facility_name,
            "export_employees": export_employees,
            "dates": dates,
            "assignments": assignments,
            "blocked_map": blocked_map,
            "employee_target_hours": employee_target_hours,
            "availability_blocked": blocked_sets,
            "schedule_archetype": schedule_archetype,
            "qual_codes": qual_codes,
            "qual_ids_by_employee": emp_quals,
            "contract_target_hours": display_target_hours,
            "manager_mode": manager_mode,
            "publish_state": publish_state,
            "focus_mode": False,
            "posting_readiness": posting_readiness,
        },
    )


def _refresh_ops_ribbon_slot_from_draft(
    ribbon_slot: Any,
    *,
    draft_key: str,
    dates: List[date],
    refresh_kwargs: Mapping[str, Any],
) -> None:
    if ribbon_slot is None or draft_key not in st.session_state:
        return
    draft_frame = _sanitize_schedule_dataframe(
        st.session_state[draft_key].copy(),
        dates,
    )
    _refresh_workspace_ops_ribbon(
        ribbon_slot,
        draft_frame=draft_frame,
        **dict(refresh_kwargs),
    )


def _ops_ribbon_deficit_display(hours_delta: float) -> Tuple[str, str]:
    if abs(hours_delta) < 0.5:
        return "Balanced", "lab-ops-metric-value lab-ops-metric-ok"
    return f"{hours_delta:+.0f}h", "lab-ops-metric-value lab-ops-metric-warn"


def _ops_ribbon_metrics_html(
    *,
    hours_delta: float,
    gap_count: int,
    draft_badge: str = "",
) -> str:
    deficit_label, deficit_class = _ops_ribbon_deficit_display(hours_delta)
    return f"""
            <div class="lab-ops-ribbon">
              <div class="lab-ops-metric">
                <div class="lab-ops-metric-label">Contract Hours Deficit{draft_badge}</div>
                <div id="lab-ops-hours-deficit" class="{deficit_class}">{html_lib.escape(deficit_label)}</div>
              </div>
              <div class="lab-ops-metric">
                <div class="lab-ops-metric-label">Total Unfilled Gaps</div>
                <div id="lab-ops-gap-count" class="lab-ops-metric-value">{gap_count}</div>
              </div>
            </div>
            """


def _seed_ops_ribbon_metrics_slot(
    ribbon_slot: Any,
    *,
    hours_delta: float,
    gap_count: int,
    pending_mutations: int = 0,
) -> None:
    draft_badge = ""
    if pending_mutations > 0:
        draft_badge = (
            f'<span class="lab-draft-badge">Draft · {pending_mutations} unsaved edit'
            f"{'' if pending_mutations == 1 else 's'}</span>"
        )
    with ribbon_slot.container():
        st.markdown(
            _ops_ribbon_metrics_html(
                hours_delta=hours_delta,
                gap_count=gap_count,
                draft_badge=draft_badge,
            ),
            unsafe_allow_html=True,
        )


def _render_operational_ribbon(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    gates: FeatureGates,
    facility_name: str,
    export_employees: List[Dict],
    dates: List[date],
    assignments: List[Dict],
    blocked_map: Dict[str, Dict[date, str]],
    gap_count: int,
    schedule_frame: pd.DataFrame,
    employee_target_hours: Dict[str, float],
    policy_view: Optional[PolicyViewModel] = None,
    availability_blocked: Optional[Dict[str, Set[date]]] = None,
    schedule_archetype: str = "STANDARD",
    qual_codes: Optional[Dict[str, str]] = None,
    qual_ids_by_employee: Optional[Dict[str, Set[str]]] = None,
    contract_target_hours: Optional[Dict[str, float]] = None,
    manager_mode: bool = False,
    publish_state: Optional[Mapping[str, object]] = None,
    focus_mode: bool = False,
    posting_readiness: Optional[SchedulePostingReadiness] = None,
) -> None:
    hours_targets = (
        contract_target_hours
        if contract_target_hours is not None
        else employee_target_hours
    )
    _needed, _scheduled, hours_delta = _contract_hours_deficit(
        schedule_frame,
        employees=employees,
        dates=dates,
        templates=templates,
        employee_target_hours=hours_targets,
        schedule_archetype=schedule_archetype,
    )
    draft_badge = ""
    if policy_view and policy_view.has_unpublished_changes:
        draft_count = len(policy_view.pending_mutations)
        draft_badge = (
            f'<span class="lab-draft-badge">Draft · {draft_count} unsaved edit'
            f"{'' if draft_count == 1 else 's'}</span>"
        )

    csv_bytes = _build_schedule_csv_bytes(
        export_employees,
        dates,
        assignments,
        templates,
        blocked_map=blocked_map,
    )
    pdf_html = _build_breakroom_print_document(
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
        posting_context=breakroom_posting_context_from_publish_state(
            publish_state,
            is_premium=gates.is_premium,
        ),
    )

    if not focus_mode:
        st.markdown(
            _ops_ribbon_metrics_html(
                hours_delta=hours_delta,
                gap_count=gap_count,
                draft_badge=draft_badge,
            ),
            unsafe_allow_html=True,
        )

    action_cols = st.columns(
        [1.35, 1.0] if focus_mode else ([1.5, 1.0, 1.0] if not manager_mode else [1.5, 1.5])
    )

    pending_count = len(_load_pending_mutations(period.id))
    save_label = _save_button_label(period.id)

    if action_cols[0].button(
        save_label,
        type="primary",
        width="stretch",
        key=f"ribbon_save_{period.id}",
    ):
        handle_save_button_click(st.session_state, period.id, rerun=st.rerun)
        st.rerun()

    if focus_mode and action_cols[1].button(
        "Exit fullscreen",
        width="stretch",
        key=f"ribbon_exit_focus_{period.id}",
    ):
        st.session_state[_schedule_focus_key(period.id)] = False
        st.rerun()

    if manager_mode:
        publish_notice = st.session_state.pop(_workspace_publish_notice_key(period.id), None)
        if publish_notice:
            st.success(publish_notice)
        return

    if focus_mode:
        return

    export_blocked = posting_readiness is not None and not posting_readiness.is_ready
    with action_cols[1].popover("Export PDF/CSV", width="stretch"):
        st.download_button(
            "Download CSV",
            data=csv_bytes,
            file_name=f"schedule_{period.id}.csv",
            mime="text/csv",
            width="stretch",
            key=f"ribbon_csv_{period.id}",
        )
        if gates.can_export_breakroom:
            if export_blocked and posting_readiness is not None:
                bullet_lines = "; ".join(posting_readiness.attention_bullets)
                st.warning(
                    f"Breakroom export blocked until schedule issues are resolved: {bullet_lines}"
                )
            st.download_button(
                "Download PDF (breakroom HTML)",
                data=pdf_html.encode("utf-8"),
                file_name=f"breakroom_schedule_{period.id}.html",
                mime="text/html",
                width="stretch",
                key=f"ribbon_pdf_{period.id}",
                disabled=export_blocked,
                help=(
                    "Breakroom-ready Legal/Ledger landscape HTML for posting/print."
                    + (
                        " Disabled until contract hours, night tallies, and persist checks pass."
                        if export_blocked
                        else ""
                    )
                ),
            )
        else:
            st.markdown(
                f"""
                <div class="lab-premium-lock" style="margin-top:8px;">
                  <strong>Breakroom export — Premium</strong><br>
                  {html_lib.escape(PREMIUM_UPSELL_SHORT)}
                </div>
                """,
                unsafe_allow_html=True,
            )
            preview_dates = dates[:7]
            if preview_dates:
                preview_html = _build_watermarked_breakroom_preview(
                    _build_breakroom_print_document(
                        facility_name=facility_name,
                        period=period,
                        employees=export_employees,
                        dates=preview_dates,
                        assignments=assignments,
                        templates=templates,
                        blocked_map=blocked_map,
                        schedule_archetype=schedule_archetype,
                        rules=rules,
                        qual_codes=qual_codes,
                        qual_ids_by_employee=qual_ids_by_employee,
                        posting_context=breakroom_posting_context_from_publish_state(
                            publish_state,
                            is_premium=gates.is_premium,
                        ),
                    )
                )
                with st.expander("Preview breakroom schedule (watermarked)", expanded=False):
                    st.iframe(preview_html, height=420, width="stretch")
                if st.button(
                    f"Upgrade for breakroom download — {PREMIUM_PRICE_DISPLAY}",
                    key=f"ribbon_breakroom_upgrade_{period.id}",
                    width="stretch",
                ):
                    st.session_state[schedule_sess.billing_checkout_trigger_key(tenant_id)] = True
                    st.rerun()

    with action_cols[2]:
        with st.popover("Roster Tools", width="stretch"):
            qual_catalog = _fetch_qualifications_by_code(conn, tenant_id)
            _render_roster_import_panel(
                conn,
                tenant_id=tenant_id,
                rules=rules,
                qual_catalog=qual_catalog,
            )
            st.markdown("---")
            _render_add_vacant_line_panel(
                conn,
                tenant_id=tenant_id,
                rules=rules,
                qual_catalog=qual_catalog,
            )

    publish_notice = st.session_state.pop(_workspace_publish_notice_key(period.id), None)
    if publish_notice:
        st.success(publish_notice)


def _render_employee_hour_balance_panel(
    *,
    employee: Dict,
    compliance_report: ComplianceReport,
    target_hours: Dict[str, float],
    period: TenantPeriod,
    contract_row: Optional[object] = None,
    biweekly_ot_risk: bool = False,
    embedded: bool = False,
) -> None:
    summary = next(
        (row for row in compliance_report.labor_summaries if row.employee_id == employee["id"]),
        None,
    )
    target = float(target_hours.get(employee["id"], 0.0))
    if contract_row is not None:
        scheduled = float(contract_row.actual_hours)
        target = float(contract_row.target_hours)
        risk_class = str(contract_row.status_class)
        status_label = str(contract_row.status_label)
    else:
        scheduled = float(summary.scheduled_hours if summary else 0.0)
        risk_class = "contract-ok"
        status_label = ""
    delta = scheduled - target
    if not embedded:
        st.markdown(
            '<div class="lab-drawer-title">Employee Hour Balance</div>',
            unsafe_allow_html=True,
        )
    st.markdown(
        f"**{html_lib.escape(employee['full_name'])}** · "
        f"{html_lib.escape(str(employee.get('contract_line_type', '—')))} · "
        f"{employee.get('fte', 0.0):.1f} FTE"
    )
    st.markdown(
        f'<div class="lab-hour-balance {risk_class}">{scheduled:.0f}h / {target:.0f}h</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="lab-hour-balance-sub {risk_class}">'
        f"{period.week_count}-week block · {html_lib.escape(period.name)}<br>"
        f"Delta {delta:+.0f}h vs target"
        f"</div>",
        unsafe_allow_html=True,
    )
    if status_label:
        st.caption(status_label)
    if biweekly_ot_risk:
        st.warning("Bi-weekly normal-hours threshold exceeded in a rolling 14-day window.")
    if summary and summary.statutory_overtime_hours > 0:
        st.warning(
            f"Statutory OT exposure: {summary.statutory_overtime_hours:.1f}h this period."
        )








def _render_unified_workspace(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    assignments: List[Dict],
    compliance_report: ComplianceReport,
    gates: FeatureGates,
    facility_name: str,
    audit_summary: Optional[ComplianceAuditSummary] = None,
    manager_mode: bool = False,
) -> None:
    _process_roster_management_actions(
        conn,
        tenant_id=tenant_id,
        period=period,
        rules=rules,
    )

    # Seed the session archetype from the durable per-tenant default before any
    # downstream consumer (gap count, contract deficit, policy view, export) reads
    # it, so a 12-hour tenant never renders STANDARD math on first paint.
    _archetype_session_key = _schedule_archetype_session_key(period.id)
    if _archetype_session_key not in st.session_state:
        st.session_state[_archetype_session_key] = get_tenant_schedule_archetype(
            conn, tenant_id=tenant_id, default=ScheduleArchetype.STANDARD
        )

    total_employees = len(employees)
    export_employees = list(employees)
    period, employees = _effective_period_and_employees(
        period, employees, gates, manager_mode=manager_mode
    )
    prioritize = _get_prioritize_fill(period.id)
    if prioritize:
        focus_id = prioritize["employee_id"]
        employees = sorted(employees, key=lambda row: 0 if row["id"] == focus_id else 1)
        st.session_state[_focused_employee_key(period.id)] = focus_id

    dates = list(_daterange(period.period_start, period.period_end_inclusive))
    _apply_external_schedule_import_if_needed(period.id)
    assignments = _resolve_assignments_for_grid(
        conn,
        tenant_id=tenant_id,
        period=period,
        gates=gates,
    )
    blocked_map, blocked_sets, target_hours = _availability_context(
        conn,
        tenant_id=tenant_id,
        period=period,
        rules=rules,
        employees=employees,
    )
    emp_quals = _fetch_employee_qualification_ids(conn, tenant_id)
    display_target_hours = _fte_contract_target_hours(
        rules=rules,
        period=period,
        employees=employees,
        target_hours=target_hours,
        emp_quals=emp_quals,
    )
    roster_rows = _fetch_roster_rows(
        conn,
        tenant_id,
        rules=rules,
        weeks_in_period=period.week_count,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
    )
    roster_by_id = {row["id"]: row for row in roster_rows}

    template_info = _shift_templates_for_compliance(templates)
    qual_codes = _qual_code_map(emp_quals)
    if gates.is_trial_tier and total_employees > (gates.trial_employee_cap or TRIAL_MAX_EMPLOYEES):
        if not _schedule_focus_active(period.id):
            st.caption(
                f"Trial workspace: showing {len(employees)} of {total_employees} roster lines."
            )

    if not _schedule_focus_active(period.id):
        _render_trial_limits_banner(gates, total_employees=total_employees)

    schedule_archetype_value = st.session_state.get(
        _schedule_archetype_session_key(period.id),
        ScheduleArchetype.STANDARD.value,
    )
    agency_path = _resolve_agency_request_path(period.id)

    if st.session_state.get("roster_error"):
        st.error(st.session_state["roster_error"])
        st.session_state.pop("roster_error", None)
    if st.session_state.get("roster_success"):
        st.success(st.session_state["roster_success"])
        st.session_state.pop("roster_success", None)


    baseline_key = _schedule_baseline_key(period.id)
    draft_key = _schedule_draft_key(period.id)
    editor_key = f"schedule_editor_{period.id}"
    agency_path = _resolve_agency_request_path(period.id)
    baseline_from_db = _build_schedule_dataframe(
        employees, dates, assignments, templates, blocked_map=blocked_map,
        agency_request_path=agency_path,
    )

    browser_store_scrub_key = f"schedule_browser_store_scrubbed_{period.id}"
    if not st.session_state.get(browser_store_scrub_key):
        if not _load_pending_mutations(period.id):
            _clear_accumulated_grid_changes(period.id)
            _reset_grid_bridge_run_cache(period.id)
            _clear_grid_session_storage_bridge(period.id)
        st.session_state[browser_store_scrub_key] = True

    sync_from_db = (
        draft_key not in st.session_state
        or st.session_state.pop(f"schedule_sync_{period.id}", False)
        or (
            draft_key in st.session_state
            and _schedule_frame_needs_display_resync(st.session_state[draft_key], dates)
        )
        or (
            prioritize is not None
            and st.session_state.get(f"prioritize_grid_rev_{period.id}")
            != prioritize["employee_id"]
        )
    )
    if sync_from_db and schedule_sess.peek_save_requested(st.session_state, period.id):
        sync_from_db = False
    if sync_from_db:
        if _load_pending_mutations(period.id):
            st.warning("Unpublished grid edits were discarded after schedule refresh.")
        sanitized_baseline = _sanitize_schedule_dataframe(baseline_from_db, dates)
        st.session_state[draft_key] = sanitized_baseline
        st.session_state[baseline_key] = sanitized_baseline
        _save_pending_mutations(period.id, [])
        _bump_schedule_grid_staging_revision(period.id)
        _invalidate_schedule_matrix_view_cache(period.id)
        st.session_state[_schedule_ignore_grid_echo_key(period.id)] = True
        st.session_state.pop(editor_key, None)
        st.session_state.pop(f"schedule_metadata_editor_{period.id}", None)
        if prioritize is not None:
            st.session_state[f"prioritize_grid_rev_{period.id}"] = prioritize["employee_id"]

    draft = _sanitize_schedule_dataframe(st.session_state[draft_key].copy(), dates)
    draft = _resync_empty_draft_from_assignments(
        period_id=period.id,
        draft_key=draft_key,
        baseline_key=baseline_key,
        draft=draft,
        baseline_from_db=baseline_from_db,
        dates=dates,
        employees=employees,
        templates=templates,
        assignments=assignments,
    )
    _discard_stale_pending_when_draft_matches_db(
        period_id=period.id,
        draft=draft,
        baseline_from_db=baseline_from_db,
        dates=dates,
    )
    policy_engine = SchedulePolicyEngine()
    policy_view = policy_engine.derive_view_model(
        draft,
        employees=employees,
        dates=dates,
        week_count=period.week_count,
        pending_mutations=_load_pending_mutations(period.id),
        cell_errors=st.session_state.get(_schedule_errors_key(period.id), {}),
        schedule_archetype=st.session_state.get(
            _schedule_archetype_session_key(period.id),
            ScheduleArchetype.STANDARD.value,
        ),
        contract_target_hours=display_target_hours,
    )
    display_draft = _enrich_employee_column_labels(
        draft,
        employees=employees,
        contract_rows=policy_view.contract_rows,
    )

    live_gap_count = open_shift_gaps_from_frame(
        draft,
        employees=employees,
        dates=dates,
        db_templates=templates,
        shift_templates=template_info,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        schedule_archetype=schedule_archetype_value,
    )

    _needed, _scheduled, readiness_hours_delta = _contract_hours_deficit(
        draft,
        employees=employees,
        dates=dates,
        templates=templates,
        employee_target_hours=display_target_hours,
        schedule_archetype=schedule_archetype_value,
    )
    posting_readiness = _evaluate_schedule_posting_readiness(
        assignments=assignments,
        employees=employees,
        period=period,
        template_info=template_info,
        hours_delta=readiness_hours_delta,
        pending_mutations=len(policy_view.pending_mutations),
        check_portage_tallies=is_portage_roster(employees),
        schedule_frame=draft,
        templates=templates,
        dates=dates,
    )

    if audit_summary is not None and not manager_mode and not _schedule_focus_active(period.id):
        _render_schedule_status_bar(
            tenant_id=tenant_id,
            gates=gates,
            audit_summary=audit_summary,
            gap_count=live_gap_count,
            period=period,
            posting_readiness=posting_readiness,
        )
    elif st.session_state.get("compliance_error"):
        pass

    publish_state: Dict[str, object] = (
        _workspace_publish_state(
            period_id=period.id,
            audit_summary=audit_summary,
        )
        if audit_summary is not None
        else {}
    )

    if manager_mode:
        manager_view = st.radio(
            "Manager workspace",
            ["Schedule", "Analytics", "Print"],
            horizontal=True,
            key=_manager_workspace_tab_key(period.id),
            label_visibility="collapsed",
        )
        if ensure_schedule_tab_for_pending_save(
            st.session_state,
            period.id,
            current_tab=manager_view,
            toast=st.toast,
            rerun=st.rerun,
        ):
            return
        if manager_view == "Analytics":
            _render_manager_analytics_tab(
                conn,
                tenant_id=tenant_id,
                period=period,
                rules=rules,
                employees=employees,
                templates=templates,
                assignments=assignments,
                compliance_report=compliance_report,
                gates=gates,
                target_hours=target_hours,
                display_target_hours=display_target_hours,
                policy_view=policy_view,
                display_draft=display_draft,
                draft=draft,
                dates=dates,
                emp_quals=emp_quals,
                qual_codes=qual_codes,
                roster_by_id=roster_by_id,
            )
            return
        if manager_view == "Print":
            _render_manager_print_tab(
                conn,
                tenant_id=tenant_id,
                period=period,
                facility_name=facility_name,
                export_employees=export_employees,
                dates=dates,
                assignments=assignments,
                templates=templates,
                blocked_map=blocked_map,
                gates=gates,
                rules=rules,
                qual_codes=qual_codes,
                qual_ids_by_employee=emp_quals,
                contract_target_hours=display_target_hours,
                publish_state=publish_state,
                schedule_archetype=schedule_archetype_value,
                posting_readiness=posting_readiness,
            )
            return

    focus_on = _schedule_focus_active(period.id)
    _handle_focus_exit_query_param(period_id=period.id)

    ops_ribbon_slot = st.empty() if not focus_on else None
    if ops_ribbon_slot is not None:
        _inject_ops_ribbon_live_metrics_listener()
        _seed_ops_ribbon_metrics_slot(
            ops_ribbon_slot,
            hours_delta=readiness_hours_delta,
            gap_count=live_gap_count,
            pending_mutations=len(policy_view.pending_mutations),
        )

    health_snapshot: Optional[ScheduleHealthSnapshot] = None
    if not focus_on and is_portage_roster(employees):
        health_snapshot = build_schedule_health_snapshot(
            schedule_frame=draft,
            employees=employees,
            dates=dates,
            templates=templates,
            template_info=template_info,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
            qual_codes=qual_codes,
            pending_mutations=len(policy_view.pending_mutations),
            hours_delta=readiness_hours_delta,
            rules=rules,
            weeks_in_period=period.week_count,
            employee_target_hours=display_target_hours,
            emp_quals=emp_quals,
        )

    if health_snapshot is not None and not focus_on:
        _render_schedule_health_panel(
            period=period,
            snapshot=health_snapshot,
            dates=dates,
        )

    if focus_on:
        _inject_focus_layout_css()

    grid_col = st.container()

    with grid_col:
        _reset_grid_bridge_run_cache(period.id)
        if not focus_on:
            st.markdown(
                f'<div id="{SCHEDULE_GRID_ANCHOR}"></div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<p><strong>{html_lib.escape(period.name)}</strong> · Master Schedule</p>',
                unsafe_allow_html=True,
            )
        view_dates, chunk_index, max_chunk_index, chunk_labels = _resolve_schedule_view_dates(
            dates,
            period=period,
        )
        if chunk_labels and not focus_on:
            if len(chunk_labels) == 2:
                week_a, week_b, week_info = st.columns([1, 1, 2])
                if week_a.button(
                    chunk_labels[0],
                    width="stretch",
                    key=f"schedule_view_chunk_0_{period.id}",
                    type="primary" if chunk_index == 0 else "secondary",
                ):
                    if chunk_index != 0:
                        st.session_state[_schedule_view_chunk_key(period.id)] = 0
                        _invalidate_schedule_matrix_view_cache(period.id)
                        st.rerun()
                if week_b.button(
                    chunk_labels[1],
                    width="stretch",
                    key=f"schedule_view_chunk_1_{period.id}",
                    type="primary" if chunk_index == 1 else "secondary",
                ):
                    if chunk_index != 1:
                        st.session_state[_schedule_view_chunk_key(period.id)] = 1
                        _invalidate_schedule_matrix_view_cache(period.id)
                        st.rerun()
                week_info.caption(
                    f"Viewing **{chunk_labels[chunk_index]}** of {period.week_count}. "
                    "Solver, exports, and compliance audits use the full cycle. "
                    "Shift edits are staged until **Save** (all weeks)."
                )
            else:
                nav_prev, nav_info, nav_next = st.columns([1, 2, 1])
                if nav_prev.button(
                    f"◀ {chunk_labels[chunk_index - 1]}",
                    width="stretch",
                    key=f"schedule_view_prev_{period.id}",
                    disabled=chunk_index <= 0,
                ):
                    st.session_state[_schedule_view_chunk_key(period.id)] = chunk_index - 1
                    _invalidate_schedule_matrix_view_cache(period.id)
                    st.rerun()
                nav_info.caption(
                    f"Viewing **{chunk_labels[chunk_index]}** of {period.week_count}. "
                    "Solver, exports, and compliance audits use the full cycle. "
                    "Shift edits are staged until Publish."
                )
                if nav_next.button(
                    f"{chunk_labels[chunk_index + 1]} ▶",
                    width="stretch",
                    key=f"schedule_view_next_{period.id}",
                    disabled=chunk_index >= max_chunk_index,
                ):
                    st.session_state[_schedule_view_chunk_key(period.id)] = chunk_index + 1
                    _invalidate_schedule_matrix_view_cache(period.id)
                    st.rerun()
        else:
            if not focus_on:
                st.caption(
                    f"Full **{period.week_count}-week** cycle — scroll **inside the grid** (↔ and ↕) for all weeks. "
                    "Click **Save** when you are ready to commit manual edits."
                )

        view_baseline = _slice_schedule_frame_for_view(display_draft, view_dates)
        matrix_frame = _matrix_frame_for_view(
            period.id,
            display_draft,
            view_dates,
            employees,
        )
        metadata_editor_key = f"schedule_metadata_editor_{period.id}"

        if focus_on:
            focus_active = True
        else:
            legend_col, grid_mode_col = st.columns([3, 2])
            with legend_col:
                _render_shift_legend()
            with grid_mode_col:
                focus_active = _render_focus_controls(
                    period_id=period.id,
                    health_snapshot=health_snapshot,
                )
        if focus_active:
            edit_mode = True
            st.session_state[_schedule_edit_mode_key(period.id)] = True
        elif not focus_active:
            _inject_schedule_grid_layout_css()

        if not focus_active:
            edit_mode = st.toggle(
                "Edit schedule",
                value=bool(st.session_state.get(_schedule_edit_mode_key(period.id), True)),
                key=f"schedule_edit_toggle_{period.id}",
                help="Show shift dropdowns in each cell. Changes stay staged until Save.",
            )
            st.session_state[_schedule_edit_mode_key(period.id)] = edit_mode
        if edit_mode and not focus_active:
            pending_count = len(_load_pending_mutations(period.id))
            pending_note = (
                f" **{pending_count} unsaved change(s)** — click Save to commit."
                if pending_count
                else ""
            )
            st.caption(
                "Each cell is a dropdown: pick **D** (day), **E** (evening), or **N** (night). "
                "D/E contract lines only offer D and E; D/N lines offer D and N. "
                "Right-click any day to lock/unlock that **Mon–Sun week** for the row. "
                "Locked worked shifts cannot be edited and are skipped when drag-filling. "
                "Footer tallies and row colors (yellow under target, red over target) update live."
                + pending_note
            )
        elif not edit_mode and not focus_active:
            st.caption("View-only roster. Turn on **Edit schedule** to change shifts.")

        meta_columns = ["Employee", "employee_id", "fte", "contract_line_type"]
        meta_view, meta_column_config, meta_columns = _build_roster_meta_view(view_baseline)

        if not focus_active:
            st.markdown('<div class="lab-schedule-fs-anchor"></div>', unsafe_allow_html=True)
        from lab_scheduler.engine.constraints import portage_employee_target_hours
        from lab_scheduler.scheduling.contract_payroll import apply_catalog_targets_for_vacant_master_lines
        from lab_scheduler.scheduling.profiles import EmployeeProfile

        grid_profiles = [
            EmployeeProfile(
                id=str(employee["id"]),
                full_name=str(employee.get("full_name") or ""),
                fte=float(employee.get("fte") or 1.0),
                qualification_ids=emp_quals.get(employee["id"], set()),
                seniority_hours=float(employee.get("seniority_hours") or 0.0),
                base_hourly_rate=float(employee.get("base_hourly_rate") or 40.0),
                contract_line_type=employee.get("contract_line_type"),
            )
            for employee in employees
        ]
        grid_base_targets = portage_employee_target_hours(
            grid_profiles,
            weeks_in_period=period.week_count,
            rules=rules,
        )
        grid_catalog_targets = apply_catalog_targets_for_vacant_master_lines(
            grid_profiles,
            grid_base_targets,
            rules=rules,
            weeks_in_period=period.week_count,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
        )
        full_employee_matrix = _employee_schedule_matrix(display_draft)
        equity_drift_by_employee = _build_portage_equity_drift_map_for_grid(
            employees,
            full_employee_matrix,
            [day.isoformat() for day in dates],
            period=period,
            catalog_targets=grid_catalog_targets,
            qual_codes=qual_codes,
        )
        shift_cell_locks = fetch_shift_cell_locks(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=period.id,
        )
        if st.session_state.pop(_schedule_distribute_alt_pending_key(period.id), False):
            from lab_scheduler.scheduling.employee_scheduling_profile import (
                build_portage_scheduling_profiles,
            )
            from lab_scheduler.scheduling.preference_fill import fill_schedule_by_preferences
            from lab_scheduler.scheduling.preference_policy import (
                FillMode,
                load_tenant_preference_policy,
            )

            policy = load_tenant_preference_policy(conn, tenant_id)
            profiles = build_portage_scheduling_profiles(
                draft,
                [
                    EmployeeProfile(
                        id=str(employee["id"]),
                        full_name=str(employee.get("full_name") or employee["id"]),
                        fte=float(employee.get("fte") or 1.0),
                        qualification_ids=set(emp_quals.get(str(employee["id"]), set())),
                        contract_line_type=employee.get("contract_line_type"),
                    )
                    for employee in employees
                ],
                employee_target_hours=grid_catalog_targets,
                qual_codes=qual_codes,
            )
            redistributed, distribute_result = fill_schedule_by_preferences(
                draft,
                employees=employees,
                dates=dates,
                period_start=period.period_start,
                period_end=period.period_end_inclusive,
                weeks_in_period=period.week_count,
                rules=rules,
                templates=templates,
                shift_templates=template_info,
                locked_cells=shift_cell_locks,
                blocked_map=blocked_map,
                emp_quals=emp_quals,
                qual_codes=qual_codes,
                employee_target_hours=grid_catalog_targets,
                policy=policy,
                profiles=profiles,
                availability_blocked=blocked_sets,
                mode=FillMode.WEEKEND_STAGGER_SLICE,
            )
            if distribute_result.cells_changed > 0:
                policy_view, any_applied = _process_staged_grid_edits(
                    conn,
                    tenant_id=tenant_id,
                    period=period,
                    rules=rules,
                    employees=employees,
                    templates=templates,
                    draft_frame=draft,
                    edited=redistributed,
                    dates=dates,
                    blocked_map=blocked_map,
                    employee_target_hours=target_hours,
                    availability_blocked=blocked_sets,
                    locked_cells=shift_cell_locks,
                )
                st.session_state[draft_key] = _sanitize_schedule_dataframe(
                    policy_view.draft_frame,
                    dates,
                )
                st.session_state[baseline_key] = st.session_state[draft_key]
                if any_applied:
                    _bump_schedule_grid_staging_revision(period.id)
                _clear_accumulated_grid_changes(period.id)
                _clear_grid_component_echo(period.id)
                _clear_grid_session_storage_bridge(period.id)
                _invalidate_schedule_matrix_view_cache(period.id)
                st.session_state[_schedule_ignore_grid_echo_key(period.id)] = True
                st.toast(
                    f"Filled {distribute_result.cells_changed} cell(s) (weekend stagger applied).",
                    icon="⚖️",
                )
            else:
                st.toast("No weekend shift changes were needed.", icon="ℹ️")
            st.rerun()
        if st.session_state.pop(_schedule_alternate_fill_pending_key(period.id), False):
            from lab_scheduler.scheduling.employee_scheduling_profile import (
                build_portage_scheduling_profiles,
            )
            from lab_scheduler.scheduling.preference_fill import fill_schedule_by_preferences
            from lab_scheduler.scheduling.preference_policy import (
                FillMode,
                load_tenant_preference_policy,
            )

            policy = load_tenant_preference_policy(conn, tenant_id)
            profiles = build_portage_scheduling_profiles(
                draft,
                [
                    EmployeeProfile(
                        id=str(employee["id"]),
                        full_name=str(employee.get("full_name") or employee["id"]),
                        fte=float(employee.get("fte") or 1.0),
                        qualification_ids=set(emp_quals.get(str(employee["id"]), set())),
                        contract_line_type=employee.get("contract_line_type"),
                    )
                    for employee in employees
                ],
                employee_target_hours=grid_catalog_targets,
                qual_codes=qual_codes,
            )
            filled, fill_result = fill_schedule_by_preferences(
                draft,
                employees=employees,
                dates=dates,
                period_start=period.period_start,
                period_end=period.period_end_inclusive,
                weeks_in_period=period.week_count,
                rules=rules,
                templates=templates,
                shift_templates=template_info,
                locked_cells=shift_cell_locks,
                blocked_map=blocked_map,
                emp_quals=emp_quals,
                qual_codes=qual_codes,
                employee_target_hours=grid_catalog_targets,
                policy=policy,
                profiles=profiles,
                availability_blocked=blocked_sets,
                mode=FillMode.ALTERNATE_SHIFTS,
            )
            if fill_result.cells_changed > 0:
                policy_view, any_applied = _process_staged_grid_edits(
                    conn,
                    tenant_id=tenant_id,
                    period=period,
                    rules=rules,
                    employees=employees,
                    templates=templates,
                    draft_frame=draft,
                    edited=filled,
                    dates=dates,
                    blocked_map=blocked_map,
                    employee_target_hours=target_hours,
                    availability_blocked=blocked_sets,
                    locked_cells=shift_cell_locks,
                )
                st.session_state[draft_key] = _sanitize_schedule_dataframe(
                    policy_view.draft_frame,
                    dates,
                )
                st.session_state[baseline_key] = st.session_state[draft_key]
                if any_applied:
                    _bump_schedule_grid_staging_revision(period.id)
                _clear_accumulated_grid_changes(period.id)
                _clear_grid_component_echo(period.id)
                _clear_grid_session_storage_bridge(period.id)
                _invalidate_schedule_matrix_view_cache(period.id)
                st.session_state[_schedule_ignore_grid_echo_key(period.id)] = True
                summary = f"Filled {fill_result.cells_changed} alternate shift cell(s)"
                if fill_result.stagger_applied:
                    summary += " (weekend stagger applied)"
                st.toast(summary + ".", icon="✅")
            else:
                st.toast("No alternate shift changes were needed.", icon="ℹ️")
            st.rerun()
        if st.session_state.pop(_schedule_clear_pending_key(period.id), False):
            cleared_draft, cleared_count = _build_cleared_schedule_draft(
                draft,
                employees=employees,
                dates=dates,
                locked_cells=shift_cell_locks,
                blocked_map=blocked_map,
            )
            if cleared_count > 0:
                policy_view, any_applied = _process_staged_grid_edits(
                    conn,
                    tenant_id=tenant_id,
                    period=period,
                    rules=rules,
                    employees=employees,
                    templates=templates,
                    draft_frame=draft,
                    edited=cleared_draft,
                    dates=dates,
                    blocked_map=blocked_map,
                    employee_target_hours=target_hours,
                    availability_blocked=blocked_sets,
                    locked_cells=shift_cell_locks,
                )
                st.session_state[draft_key] = _sanitize_schedule_dataframe(
                    policy_view.draft_frame,
                    dates,
                )
                st.session_state[baseline_key] = st.session_state[draft_key]
                if any_applied:
                    _bump_schedule_grid_staging_revision(period.id)
                _clear_accumulated_grid_changes(period.id)
                _clear_grid_component_echo(period.id)
                _clear_grid_session_storage_bridge(period.id)
                _invalidate_schedule_matrix_view_cache(period.id)
                st.session_state[_schedule_ignore_grid_echo_key(period.id)] = True
                st.session_state[_schedule_intentional_clear_save_key(period.id)] = True
                st.toast(
                    f"Cleared {cleared_count} shift(s). Locked weeks kept.",
                    icon="🧹",
                )
            else:
                st.toast("No editable shifts to clear.", icon="ℹ️")
            st.rerun()
        focus_view_dates = dates if focus_active else view_dates
        focus_matrix_frame = (
            _matrix_frame_for_view(period.id, display_draft, focus_view_dates, employees)
            if focus_active
            else matrix_frame
        )
        focus_chunk_index = 0 if focus_active else chunk_index
        ribbon_draft = _sanitize_schedule_dataframe(
            st.session_state.get(draft_key, draft).copy(),
            dates,
        )
        ribbon_gap_count = count_open_shift_gaps_from_frame(
            ribbon_draft,
            employees=employees,
            dates=dates,
            db_templates=templates,
            shift_templates=template_info,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
            schedule_archetype=schedule_archetype_value,
        )
        visible_gap_count = _count_open_shift_gaps_for_view(
            ribbon_draft,
            view_dates=focus_view_dates,
            employees=employees,
            dates=dates,
            db_templates=templates,
            shift_templates=template_info,
            schedule_archetype=schedule_archetype_value,
        )
        grid_kwargs = dict(
            period_id=period.id,
            view_chunk_index=focus_chunk_index,
            staging_revision=_schedule_grid_staging_revision(period.id),
            contract_rows=policy_view.contract_rows,
            full_employee_matrix=full_employee_matrix,
            all_date_keys=[day.isoformat() for day in dates],
            hours_per_shift=paid_hours_per_shift(
                schedule_archetype=st.session_state.get(
                    _schedule_archetype_session_key(period.id),
                    ScheduleArchetype.STANDARD.value,
                )
            ),
            equity_drift_by_employee=equity_drift_by_employee,
            locked_cells=shift_cell_locks,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
            ops_metrics_json=_ops_metrics_config_json(
                contract_target_total=sum(float(display_target_hours.get(employee["id"], 0.0)) for employee in employees),
                full_gap_count=ribbon_gap_count,
                visible_gap_count=visible_gap_count,
            ),
            health_focus_date=_schedule_health_focus_date(period.id),
        )
        ops_ribbon_refresh_kwargs = dict(
            conn=conn,
            tenant_id=tenant_id,
            period=period,
            rules=rules,
            employees=employees,
            templates=templates,
            gates=gates,
            facility_name=facility_name,
            export_employees=export_employees,
            dates=dates,
            assignments=assignments,
            blocked_map=blocked_map,
            blocked_sets=blocked_sets,
            employee_target_hours=target_hours,
            display_target_hours=display_target_hours,
            schedule_archetype=schedule_archetype_value,
            qual_codes=qual_codes,
            emp_quals=emp_quals,
            manager_mode=manager_mode,
            publish_state=publish_state if audit_summary is not None else None,
            posting_readiness=posting_readiness,
            template_info=template_info,
        )
        if focus_active:
            st.markdown(
                '<div id="lab-focus-grid-anchor"></div>',
                unsafe_allow_html=True,
            )
            cell_change = _render_master_schedule_shift_grid(
                focus_matrix_frame,
                focus_view_dates,
                edit_mode=True,
                fullscreen=True,
                focus_fit=True,
                **grid_kwargs,
            )
            st.markdown(_focus_grid_sizer_javascript(), unsafe_allow_html=True)
        else:
            cell_change = _render_master_schedule_shift_grid(
                matrix_frame,
                view_dates,
                edit_mode=edit_mode,
                fullscreen=False,
                **grid_kwargs,
            )
        save_pending = schedule_sess.peek_save_requested(st.session_state, period.id)
        ignore_grid_echo = st.session_state.pop(
            _schedule_ignore_grid_echo_key(period.id), False
        )
        if ignore_grid_echo:
            bridge_payload = None
            bridge_changes: List[Dict[str, str]] = []
        else:
            bridge_payload = _drain_grid_session_storage_bridge(
                period.id,
                clear=False,
                read_pending=save_pending,
            )
            bridge_changes = _bridge_changes_for_draft_apply(
                bridge_payload,
                save_pending=save_pending,
            )
        bridge_lock_toggles = _grid_component_lock_toggles(bridge_payload)
        if bridge_lock_toggles:
            _apply_shift_cell_lock_toggles(
                conn,
                tenant_id=tenant_id,
                period_id=period.id,
                toggles=bridge_lock_toggles,
                actor=st.session_state.get("username", "manager"),
                period_start=period.period_start,
                period_end=period.period_end_inclusive,
            )
            st.rerun()
        lock_toggles = _grid_component_lock_toggles(cell_change)
        if lock_toggles:
            _apply_shift_cell_lock_toggles(
                conn,
                tenant_id=tenant_id,
                period_id=period.id,
                toggles=lock_toggles,
                actor=st.session_state.get("username", "manager"),
                period_start=period.period_start,
                period_end=period.period_end_inclusive,
            )
            st.rerun()

        if save_pending:
            if _finish_pending_workspace_save(
                conn,
                tenant_id=tenant_id,
                period=period,
                rules=rules,
                employees=employees,
                templates=templates,
                draft_key=draft_key,
                baseline_key=baseline_key,
                dates=dates,
                assignments=assignments,
                blocked_map=blocked_map,
                target_hours=target_hours,
                blocked_sets=blocked_sets,
                only_intentional_clear=True,
            ):
                return

        if not focus_active and not manager_mode:
            st.markdown("##### Roster lines")
            st.caption("FTE and contract type for each schedule row.")
            edited_meta = st.data_editor(
                meta_view,
                column_config=meta_column_config,
                hide_index=True,
                num_rows="fixed",
                width="stretch",
                key=metadata_editor_key,
                column_order=["#", "Employee", "fte", "contract_line_type"],
            )
            if "#" in edited_meta.columns:
                edited_meta = edited_meta.drop(columns=["#"])
        elif not focus_active:
            edited_meta = draft[
                [column for column in meta_columns if column in draft.columns]
            ].copy()
        else:
            edited_meta = meta_view.drop(columns=["#"], errors="ignore")

        grid_echo = _capture_grid_component_echo(period.id, cell_change)
        iframe_changes = (
            []
            if ignore_grid_echo
            else _filter_grid_changes_against_draft(
                draft,
                employees,
                _grid_component_cell_changes(grid_echo),
            )
        )
        filtered_bridge_changes = (
            []
            if ignore_grid_echo
            else _filter_grid_changes_against_draft(
                draft,
                employees,
                bridge_changes,
            )
        )
        grid_cell_changes = _merge_grid_cell_changes(
            filtered_bridge_changes,
            iframe_changes,
        )
        if grid_cell_changes:
            _accumulate_grid_cell_changes(period.id, grid_cell_changes)

        if grid_cell_changes:
            last_change = grid_cell_changes[-1]
            refresh_dates = focus_view_dates if focus_active else view_dates
            matrix_frame = _matrix_frame_for_view(
                period.id,
                display_draft,
                refresh_dates,
                employees,
                cell_change=last_change,
            )

        merged_edits = draft.copy()
        for col in meta_columns:
            if col in edited_meta.columns:
                merged_edits[col] = edited_meta[col].values

        if grid_cell_changes:
            draft_row_index = schedule_frame_row_index_by_employee_id(merged_edits)
            for change_item in grid_cell_changes:
                row_idx = draft_row_index.get(change_item["employee_id"])
                if row_idx is not None:
                    merged_edits.at[row_idx, change_item["date"]] = _display_shift_cell(
                        change_item["token"]
                    )

        if grid_cell_changes:
            policy_view, any_applied = _process_staged_grid_edits(
                conn,
                tenant_id=tenant_id,
                period=period,
                rules=rules,
                employees=employees,
                templates=templates,
                draft_frame=draft,
                edited=merged_edits,
                dates=dates,
                blocked_map=blocked_map,
                employee_target_hours=target_hours,
                availability_blocked=blocked_sets,
                locked_cells=shift_cell_locks,
            )
            st.session_state[draft_key] = _sanitize_schedule_dataframe(
                policy_view.draft_frame,
                dates,
            )
            st.session_state[baseline_key] = st.session_state[draft_key]
            if any_applied:
                if not save_pending:
                    _clear_grid_component_echo(period.id)
                    _clear_grid_session_storage_bridge(period.id)
                _refresh_ops_ribbon_slot_from_draft(
                    ops_ribbon_slot,
                    draft_key=draft_key,
                    dates=dates,
                    refresh_kwargs=ops_ribbon_refresh_kwargs,
                )
                _bump_schedule_grid_staging_revision(period.id)

        if save_pending:
            if _finish_pending_workspace_save(
                conn,
                tenant_id=tenant_id,
                period=period,
                rules=rules,
                employees=employees,
                templates=templates,
                draft_key=draft_key,
                baseline_key=baseline_key,
                dates=dates,
                assignments=assignments,
                blocked_map=blocked_map,
                target_hours=target_hours,
                blocked_sets=blocked_sets,
                grid_cell_changes_count=len(grid_cell_changes),
            ):
                return
        elif not merged_edits.equals(draft):
            if _process_inline_metadata_edits(
                conn,
                tenant_id=tenant_id,
                period=period,
                rules=rules,
                roster_by_id=roster_by_id,
                baseline=draft,
                edited=merged_edits,
            ):
                st.session_state.pop(metadata_editor_key, None)
                if not save_pending:
                    st.session_state[f"schedule_sync_{period.id}"] = True
                    st.rerun()

            if not _schedule_shift_cells_equal(
                merged_edits,
                draft,
                employees=employees,
                dates=dates,
            ):
                policy_view, any_applied = _process_staged_grid_edits(
                    conn,
                    tenant_id=tenant_id,
                    period=period,
                    rules=rules,
                    employees=employees,
                    templates=templates,
                    draft_frame=draft,
                    edited=merged_edits,
                    dates=dates,
                    blocked_map=blocked_map,
                    employee_target_hours=target_hours,
                    availability_blocked=blocked_sets,
                    locked_cells=shift_cell_locks,
                )
                st.session_state[draft_key] = _sanitize_schedule_dataframe(
                    policy_view.draft_frame,
                    dates,
                )
                st.session_state[baseline_key] = st.session_state[draft_key]
                if any_applied:
                    if not save_pending:
                        _clear_grid_component_echo(period.id)
                        _clear_grid_session_storage_bridge(period.id)
                    _refresh_ops_ribbon_slot_from_draft(
                        ops_ribbon_slot,
                        draft_key=draft_key,
                        dates=dates,
                        refresh_kwargs=ops_ribbon_refresh_kwargs,
                    )
                    _bump_schedule_grid_staging_revision(period.id)
                    if not save_pending:
                        st.rerun()


        if ops_ribbon_slot is not None:
            current_draft = _sanitize_schedule_dataframe(
                st.session_state[draft_key].copy(),
                dates,
            )
            _refresh_workspace_ops_ribbon(
                ops_ribbon_slot,
                conn=conn,
                tenant_id=tenant_id,
                period=period,
                rules=rules,
                employees=employees,
                templates=templates,
                gates=gates,
                facility_name=facility_name,
                export_employees=export_employees,
                dates=dates,
                assignments=assignments,
                blocked_map=blocked_map,
                blocked_sets=blocked_sets,
                draft_frame=current_draft,
                employee_target_hours=target_hours,
                display_target_hours=display_target_hours,
                schedule_archetype=schedule_archetype_value,
                qual_codes=qual_codes,
                emp_quals=emp_quals,
                manager_mode=manager_mode,
                publish_state=publish_state if audit_summary is not None else None,
                posting_readiness=posting_readiness,
                template_info=template_info,
            )

        _render_cell_error_notices(period.id)

    if not manager_mode and not focus_on:
        st.markdown("---")
        _render_breakroom_pdf_controls(
            facility_name=facility_name,
            period=period,
            employees=export_employees,
            dates=dates,
            assignments=assignments,
            templates=templates,
            blocked_map=blocked_map,
            export_allowed=(
                posting_readiness is None or posting_readiness.is_ready
            ),
            rules=rules,
            qual_codes=qual_codes,
            qual_ids_by_employee=emp_quals,
        )
        _render_suggested_compliance_overrides_expander(
            conn,
            tenant_id=tenant_id,
            period=period,
            assignments=assignments,
            templates=templates,
            employees=export_employees,
        )


def _render_schedule_audit_history(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
) -> None:
    entries = fetch_audit_logs(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=period.id,
    )

    with st.expander("Schedule Change History (Audit Log)", expanded=False):
        st.caption(
            "Immutable record of manual edits and auto-generate events for this period. "
            "Latest changes appear first."
        )
        if not entries:
            st.info("No schedule changes logged yet for this period.")
            return

        table_rows = []
        for entry in entries:
            if entry.change_type == "auto_generation":
                employee_disp = "— (full period)"
                date_disp = "—"
                prev_disp = format_shift_code_display(entry.previous_shift_code)
                new_disp = entry.new_shift_code or "—"
            elif entry.change_type == "constraint_violation":
                employee_disp = "— (unassigned slot)"
                date_disp = entry.shift_date.isoformat() if entry.shift_date else "—"
                prev_disp = format_shift_code_display(entry.previous_shift_code)
                new_disp = entry.seniority_bypass_justification or format_shift_code_display(
                    entry.new_shift_code
                )
            else:
                employee_disp = entry.employee_name or entry.employee_id or "—"
                date_disp = entry.shift_date.isoformat() if entry.shift_date else "—"
                prev_disp = format_shift_code_display(entry.previous_shift_code)
                new_disp = format_shift_code_display(entry.new_shift_code)

            bypass_disp = "Yes" if entry.seniority_bypass_flag else "—"
            if entry.seniority_bypass_flag and entry.seniority_bypass_justification:
                bypass_disp = f"Yes — {entry.seniority_bypass_justification}"

            table_rows.append(
                {
                    "ID": entry.id,
                    "When (UTC)": entry.recorded_at_utc.replace("T", " ").replace("Z", ""),
                    "Actor": entry.actor,
                    "Employee": employee_disp,
                    "Date": date_disp,
                    "Previous": prev_disp,
                    "New": new_disp,
                    "Type": entry.change_type.replace("_", " "),
                    "Seniority Bypass": bypass_disp,
                }
            )

        st.dataframe(table_rows, width="stretch", hide_index=True)




def _process_roster_management_actions(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
) -> None:
    pending_commit = st.session_state.pop(_roster_pending_commit_key(tenant_id), None)
    if pending_commit:
        manager_id = st.session_state.get("account_id", st.session_state.get("username", "manager"))
        try:
            result = update_employee_roster_line(
                conn,
                tenant_id=tenant_id,
                employee_id=str(pending_commit["employee_id"]),
                manager_id=str(manager_id),
                seniority_hours=float(pending_commit["seniority_hours"]),
                contract_line_type=str(pending_commit["contract_line_type"]),
                target_weekly_hours=float(pending_commit["target_weekly_hours"]),
                standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
            )
        except StaffLifecycleError as exc:
            st.session_state["roster_error"] = str(exc)
        else:
            _clear_roster_row_widgets(tenant_id, result.employee_id)
            vacated_note = ""
            if result.shifts_vacated_count:
                vacated_note = (
                    f" {result.shifts_vacated_count} future shift(s) vacated due to "
                    f"contract line change ({result.previous_contract_line} → "
                    f"{result.new_contract_line})."
                )
            st.session_state["roster_success"] = (
                f"Updated **{result.employee_name}** — "
                f"{result.seniority_hours:g} seniority hrs, "
                f"{result.new_contract_line}, {result.target_weekly_hours:g}h/week "
                f"({result.fte:.1f} FTE).{vacated_note}"
            )
            st.session_state[f"schedule_sync_{period.id}"] = True
        st.rerun()

    confirmed_key = f"_staff_deactivate_confirmed_{tenant_id}"
    if confirmed_key in st.session_state:
        employee_id = st.session_state.pop(confirmed_key)
        pending_key = _staff_deactivate_pending_key(tenant_id)
        st.session_state.pop(pending_key, None)
        manager_id = st.session_state.get("account_id", st.session_state.get("username", "manager"))
        try:
            result = deactivate_employee(
                conn,
                tenant_id=tenant_id,
                employee_id=employee_id,
                manager_id=str(manager_id),
            )
        except StaffLifecycleError as exc:
            st.session_state["roster_error"] = str(exc)
            st.rerun()
        else:
            st.session_state["roster_success"] = (
                f"Archived **{result.employee_name}**. "
                f"{result.shifts_vacated_count} future shift(s) marked unassigned."
            )
            st.session_state[f"schedule_sync_{period.id}"] = True
            st.rerun()


def _clear_workspace_session_keys() -> None:
    preserve = {"FormSubmitter", "login_form", "signup_form"}
    for key in list(st.session_state.keys()):
        if key in preserve:
            continue
        if key.startswith("schedule_") or key.startswith("FormSubmitter:schedule_"):
            del st.session_state[key]
        if key.startswith("staff_fairness_"):
            del st.session_state[key]
    for key in (
        "active_period_id",
        "jurisdiction",
        "compliance_error",
        "compliance_success",
        "auto_gen_summary",
        "auto_pilot_shift_equity_metrics",
        "roster_error",
        "roster_success",
        "login_error",
        "signup_error",
    ):
        st.session_state.pop(key, None)


def _clear_roster_widget_session_keys(tenant_id: str) -> None:
    """Drop per-row roster editor widgets so stale employee ids cannot linger."""

    suffix = f"_{tenant_id}"
    for key in list(st.session_state.keys()):
        if not key.startswith("roster_"):
            continue
        if key.endswith(suffix) or f"{suffix}_" in key:
            st.session_state.pop(key, None)


def _apply_roster_sync_reset(tenant_id: str) -> None:
    """
    When sync_portage_live_db.py rewrites the roster, invalidate cached UI state.
    """

    marker_path = ROOT / ".roster_reset_epoch"
    if not marker_path.is_file():
        return
    epoch = marker_path.read_text(encoding="utf-8").strip()
    if not epoch:
        return
    seen_key = f"roster_sync_epoch_{tenant_id}"
    if st.session_state.get(seen_key) == epoch:
        return
    _clear_roster_widget_session_keys(tenant_id)
    for key in (
        "auto_gen_summary",
        "auto_pilot_shift_equity_metrics",
        "roster_error",
        "roster_success",
        _roster_import_preview_key(tenant_id),
        _roster_pending_commit_key(tenant_id),
    ):
        st.session_state.pop(key, None)
    st.session_state[seen_key] = epoch


def _sign_out() -> None:
    for key in (
        "authenticated",
        "tenant_id",
        "tenant_name",
        "tenant_slug",
        "username",
        "display_name",
        "account_id",
    ):
        st.session_state.pop(key, None)
    _clear_workspace_session_keys()


def _establish_auth_session(session: AuthenticatedSession) -> None:
    _sign_out()
    st.session_state["authenticated"] = True
    st.session_state["account_id"] = session.account_id
    st.session_state["username"] = session.username
    st.session_state["display_name"] = session.display_name
    st.session_state["tenant_id"] = session.tenant_id
    st.session_state["tenant_name"] = session.tenant_name
    st.session_state["tenant_slug"] = session.tenant_slug


def _normalize_login_credential(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().replace("\ufeff", "").replace("\u200b", "")


def _attempt_login(conn: sqlite3.Connection, *, username: str, password: str) -> bool:
    username = _normalize_login_credential(username)
    password = _normalize_login_credential(password)
    if not username or not password:
        st.session_state["login_error"] = "Enter both username and password."
        return False

    ensure_demo_account_credentials(conn)
    session = authenticate_user(conn, username=username, password=password)
    if session is None:
        row = conn.execute(
            """
            SELECT 1
            FROM tenant_user_accounts
            WHERE username = ? COLLATE NOCASE AND is_active = 1
            """,
            (username,),
        ).fetchone()
        if row is None:
            suffix = (
                ""
                if _is_production_runtime()
                else " Try the quick demo buttons below."
            )
            st.session_state["login_error"] = (
                f"No active account found for `{username}`.{suffix}"
            )
        else:
            suffix = (
                ""
                if _is_production_runtime()
                else " Use your account password or the demo quick-login buttons."
            )
            st.session_state["login_error"] = f"Password incorrect.{suffix}"
        return False
    st.session_state.pop("login_error", None)
    _establish_auth_session(session)
    return True



def _placeholder_period() -> TenantPeriod:
    start = workweek_for(date.today()).start
    return TenantPeriod(
        id="__placeholder__",
        name="Not yet configured",
        period_start=start,
        week_count=8,
        period_end_inclusive=start + timedelta(days=(8 * 7 - 1)),
    )


def _run_scheduling_dashboard(conn: sqlite3.Connection, tenant_id: str) -> None:
    _apply_roster_sync_reset(tenant_id)
    tenant_name = st.session_state.get("tenant_name", tenant_id)
    username = st.session_state.get("username", "manager")
    tenant_meta = _fetch_tenant_metadata(conn, tenant_id)
    billing = fetch_tenant_billing(conn, tenant_id)
    gates = feature_gates_for_billing(billing)
    manager_mode = _is_manager_mode(conn, tenant_id)
    process_billing_checkout_trigger(
        conn,
        st.session_state,
        tenant_id=tenant_id,
        tenant_slug=tenant_meta.slug,
        customer_email=st.session_state.get("username"),
        gates=gates,
        link_button=st.link_button,
        info=st.info,
        error=st.error,
    )
    stored_jurisdiction = _load_tenant_jurisdiction(conn, tenant_id=tenant_id)
    st.session_state.setdefault("jurisdiction", stored_jurisdiction)

    self_serve_trial = _is_self_serve_trial(tenant_id, gates)

    if not _any_schedule_focus_active():
        if manager_mode or self_serve_trial:
            st.title(tenant_name)
            if self_serve_trial:
                st.caption(
                    f"Trial preview ({TRIAL_MAX_WEEKS} weeks) · signed in as {username}"
                )
            else:
                st.caption(f"Signed in as {username}")
        else:
            st.title("Lab Staffing Scheduler")
            st.caption(
                f"**{tenant_name}** · signed in as `{username}` · tenant `{tenant_id}`"
            )

    st.sidebar.header("Control Panel" if not manager_mode else "Menu")
    st.sidebar.markdown(f"**Facility:** {tenant_name}")
    if not manager_mode and not self_serve_trial:
        st.sidebar.caption(f"Tenant ID: `{tenant_id}`")
    if not manager_mode:
        _render_account_subscription_sidebar(
            conn,
            tenant_id=tenant_id,
            tenant_slug=tenant_meta.slug,
            billing=billing,
            gates=gates,
        )
    if st.sidebar.button("Sign Out", width="stretch"):
        _sign_out()
        st.rerun()

    allow_business = _business_console_allowed(tenant_id, gates)
    if manager_mode and allow_business:
        _render_business_access_hint(manager_entry=_manager_entry_requested())
        if not _manager_entry_requested():
            _render_operator_console_switch()

    if not is_onboarding_complete(conn, tenant_id=tenant_id):
        mark_onboarding_complete(conn, tenant_id=tenant_id)

    periods = _fetch_tenant_periods(conn, tenant_id)
    if not periods:
        st.info(
            "This facility workspace is provisioned but has **no schedule periods, employees, or "
            "assignments** yet. Data is fully isolated from other tenants."
        )
        rules = get_jurisdiction(
            st.session_state.get("jurisdiction", DEFAULT_JURISDICTION_NAME)
            if st.session_state.get("jurisdiction", DEFAULT_JURISDICTION_NAME) in JURISDICTIONS
            else DEFAULT_JURISDICTION_NAME
        )
        placeholder = _placeholder_period()
        st.info(
            "Import roster lines with **Roster Tools** once employees are provisioned, "
            "then create a schedule period to open the unified workspace."
        )
        qual_catalog = _fetch_qualifications_by_code(conn, tenant_id)
        with st.expander("Roster Tools", expanded=True):
            _render_roster_import_panel(
                conn,
                tenant_id=tenant_id,
                rules=rules,
                qual_catalog=qual_catalog,
            )
            st.markdown("---")
            _render_add_vacant_line_panel(
                conn,
                tenant_id=tenant_id,
                rules=rules,
                qual_catalog=qual_catalog,
            )
        return

    period_ids = [p.id for p in periods]
    default_index = (
        period_ids.index(DEFAULT_NORTHSTAR_PERIOD_ID)
        if DEFAULT_NORTHSTAR_PERIOD_ID in period_ids
        else 0
    )

    if manager_mode:
        rules = get_jurisdiction(stored_jurisdiction)
    else:
        jurisdiction_names = list(JURISDICTIONS.keys())
        default_jurisdiction = _load_tenant_jurisdiction(conn, tenant_id=tenant_id)
        selected_jurisdiction = st.sidebar.selectbox(
            "Jurisdiction",
            jurisdiction_names,
            index=jurisdiction_names.index(default_jurisdiction)
            if default_jurisdiction in jurisdiction_names
            else 0,
            help="Swap provincial employment-standards rules instantly.",
        )
        st.session_state["jurisdiction"] = selected_jurisdiction
        rules = get_jurisdiction(selected_jurisdiction)
        if not self_serve_trial:
            _render_jurisdiction_banner(rules)

    period_by_id = {item.id: item for item in periods}
    if self_serve_trial:
        selected_period_id = st.sidebar.selectbox(
            "Schedule period",
            period_ids,
            index=default_index,
            format_func=lambda pid: _friendly_period_label(period_by_id[pid]),
        )
    else:
        selected_period_id = st.sidebar.selectbox(
            "Schedule period",
            period_ids,
            index=default_index,
        )
    period = period_by_id[selected_period_id]

    if st.session_state.get("active_period_id") != period.id:
        _clear_workspace_session_keys()
        st.session_state["active_period_id"] = period.id

    ww = workweek_for(period.period_start)
    if ww.start != period.period_start:
        st.error("Period start violates Monday-start rule.")
        return

    employees = _fetch_employees(conn, tenant_id)
    templates = _fetch_shift_templates(conn, tenant_id)
    assignments = _resolve_assignments_for_grid(
        conn,
        tenant_id=tenant_id,
        period=period,
        gates=gates,
    )
    scoped_period, scoped_employees, scoped_assignments = _prepare_workspace_scope(
        period,
        employees,
        assignments,
        gates,
    )
    _blocked_map, _blocked_sets, target_hours = _availability_context(
        conn,
        tenant_id=tenant_id,
        period=scoped_period,
        rules=rules,
        employees=scoped_employees,
    )
    compliance_report = _build_compliance_report(
        rules,
        employees=scoped_employees,
        assignments=scoped_assignments,
        templates=templates,
        period=scoped_period,
        employee_target_hours=target_hours,
    )
    audit_summary, audit_html, tenant_meta = _build_audit_export_bundle(
        conn,
        tenant_id=tenant_id,
        period=scoped_period,
        rules=rules,
        employees=scoped_employees,
        assignments=scoped_assignments,
        templates=templates,
        compliance_report=compliance_report,
    )

    st.sidebar.markdown("---")
    if not manager_mode:
        _render_audit_export_sidebar_button(audit_summary, audit_html, tenant_meta, scoped_period, gates)

    _render_manager_sidebar_save_panel(
        conn,
        tenant_id=tenant_id,
        period=scoped_period,
        rules=rules,
        employees=scoped_employees,
        templates=templates,
        employee_target_hours=target_hours,
        availability_blocked=_blocked_sets,
        manager_mode=manager_mode,
    )

    _render_admin_panel(
        conn,
        tenant_id,
        gates=gates,
        period=scoped_period,
        rules=rules,
        employees=scoped_employees,
        templates=templates,
        assignments=scoped_assignments,
        compliance_report=compliance_report,
        manager_mode=manager_mode,
    )

    if st.session_state.get("compliance_error"):
        _render_compliance_flash(
            "Schedule could not be saved" if manager_mode else "COMPLIANCE ENGINE BLOCKED THIS ASSIGNMENT",
            st.session_state["compliance_error"],
        )
        if not manager_mode and st.session_state.get("compliance_error_break_glass"):
            st.session_state.pop("compliance_error_break_glass", None)

    if st.session_state.get("db_restored_message"):
        st.success(st.session_state.pop("db_restored_message"))

    if not manager_mode and not _schedule_focus_active(period.id):
        _render_audit_export_status(
            audit_summary,
            gates=gates,
            manager_mode=manager_mode,
        )

    _render_unified_workspace(
        conn,
        tenant_id=tenant_id,
        period=period,
        rules=rules,
        employees=employees,
        templates=templates,
        assignments=scoped_assignments,
        compliance_report=compliance_report,
        audit_summary=audit_summary,
        gates=gates,
        facility_name=tenant_name,
        manager_mode=manager_mode,
    )




def _render_restore_tab(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    period: Optional[TenantPeriod] = None,
    manager_mode: bool = False,
) -> None:
    if period is not None:
        st.markdown("**Schedule file (JSON)**")
        st.caption(
            "Named JSON exports for extra backups. The main **Save** button always refreshes "
            f"`{period.id}-manual.json`."
        )
        save_name = st.text_input(
            "Save as",
            value=f"{period.id}-manual",
            key=f"schedule_archive_name_{period.id}",
        )
        save_cols = st.columns(2)
        if save_cols[0].button(
            "Save a copy",
            key=f"schedule_archive_save_{period.id}",
            width="stretch",
        ):
            try:
                payload = export_period_schedule(
                    conn,
                    tenant_id=tenant_id,
                    period_id=period.id,
                    name=save_name,
                    description=f"Saved from manager workspace on {period.name}",
                )
                archive_path = save_named_archive(
                    payload,
                    name=save_name,
                    saved_dir=SAVED_SCHEDULES_DIR,
                )
                st.success(f"Saved schedule to `{archive_path.name}`.")
            except ScheduleArchiveError as exc:
                st.error(str(exc))

        try:
            payload = export_period_schedule(
                conn,
                tenant_id=tenant_id,
                period_id=period.id,
                name=save_name,
            )
            st.download_button(
                "Download JSON",
                data=json.dumps(payload, indent=2),
                file_name=f"{save_name or period.id}.json",
                mime="application/json",
                key=f"schedule_archive_download_{period.id}",
                width="stretch",
            )
        except ScheduleArchiveError as exc:
            st.caption(str(exc))

        uploaded = st.file_uploader(
            "Load schedule from file",
            type=["json"],
            key=f"schedule_archive_upload_{period.id}",
        )
        if uploaded is not None:
            if st.button(
                "Apply uploaded schedule",
                key=f"schedule_archive_apply_upload_{period.id}",
                type="primary",
                width="stretch",
            ):
                try:
                    payload = json.loads(uploaded.getvalue().decode("utf-8"))
                    _create_system_snapshot(f"pre-schedule-load-{period.id}")
                    import_period_schedule(
                        conn,
                        payload,
                        tenant_id=tenant_id,
                        period_id=period.id,
                    )
                    touch_schedule_reload_stamp(ROOT)
                    st.session_state[f"schedule_sync_{period.id}"] = True
                    _clear_workspace_session_keys()
                    st.session_state[f"schedule_sync_{period.id}"] = True
                    st.session_state["db_restored_message"] = (
                        "Loaded schedule from uploaded JSON. Reloading grid…"
                    )
                    st.rerun()
                except (ScheduleArchiveError, json.JSONDecodeError) as exc:
                    st.error(str(exc))

        saved_archives = list_named_archives(SAVED_SCHEDULES_DIR)
        if saved_archives:
            st.markdown("**Saved on this computer**")
            for archive_path in saved_archives[:5]:
                label = archive_path.stem
                try:
                    meta = load_named_archive(archive_path)
                    label = str(meta.get("name") or label)
                    exported_at = str(meta.get("exported_at") or "")
                except ScheduleArchiveError:
                    exported_at = ""
                st.caption(f"`{archive_path.name}`" + (f" · {exported_at}" if exported_at else ""))
                if st.button(
                    f"Load {label}",
                    key=f"schedule_archive_load_{period.id}_{archive_path.name}",
                    width="stretch",
                ):
                    try:
                        payload = load_named_archive(archive_path)
                        _create_system_snapshot(f"pre-schedule-load-{period.id}")
                        import_period_schedule(
                            conn,
                            payload,
                            tenant_id=tenant_id,
                            period_id=period.id,
                        )
                        touch_schedule_reload_stamp(ROOT)
                        st.session_state[f"schedule_sync_{period.id}"] = True
                        _clear_workspace_session_keys()
                        st.session_state[f"schedule_sync_{period.id}"] = True
                        st.session_state["db_restored_message"] = (
                            f"Loaded saved schedule `{label}`. Reloading grid…"
                        )
                        st.rerun()
                    except ScheduleArchiveError as exc:
                        st.error(str(exc))
        else:
            st.caption("No saved JSON schedules yet.")

    if not manager_mode:
        st.divider()

    snapshot_expander = st.expander("Full database restore (advanced)", expanded=False)
    with snapshot_expander:
        st.caption(
            "Restores the entire demo database (roster, settings, and all periods). "
            "Prefer **Load schedule** above when you only need to swap the grid."
        )
        snapshots = list_recent_snapshots(SNAPSHOTS_DIR, limit=3)
        if not snapshots:
            st.info("No snapshots yet. Snapshots are created before schedule imports and publishes.")
            return

        for snapshot in snapshots:
            st.markdown(
                f"**{html_lib.escape(snapshot.label)}**  \n"
                f"`{snapshot.recorded_at_utc}` · "
                f"{snapshot.size_bytes // 1024} KB"
            )
            if st.button(
                "Restore database",
                key=f"restore_snapshot_{snapshot.filename}",
                width="stretch",
            ):
                try:
                    restore_snapshot(DB_PATH, snapshot.path, snapshots_dir=SNAPSHOTS_DIR)
                except SnapshotError as exc:
                    st.error(str(exc))
                else:
                    manager = st.session_state.get("username", "manager")
                    log_snapshot_restore(
                        conn,
                        tenant_id=tenant_id,
                        manager_id=manager,
                        snapshot_filename=snapshot.filename,
                    )
                    _clear_workspace_session_keys()
                    st.session_state["db_restored_message"] = (
                        f"Restored database from snapshot `{snapshot.label}`. Reloading application state…"
                    )
                    st.rerun()


def _render_health_metric_card(
    label: str,
    value: str,
    status: str,
    subtitle: str,
) -> str:
    css = "lab-health-card-healthy" if status == "healthy" else "lab-health-card-warn"
    return f"""
    <div class="lab-health-card {css}">
      <div class="lab-health-label">{html_lib.escape(label)}</div>
      <div class="lab-health-value">{html_lib.escape(value)}</div>
      <div class="lab-health-sub">{html_lib.escape(subtitle)}</div>
    </div>
    """


def _render_manager_health_summary(snapshot: ManagerHealthSnapshot) -> None:
    cols = st.columns(3)
    metrics = (
        ("Compliance Health", f"{snapshot.compliance_health_pct:.0f}%", snapshot.compliance_status),
        ("Coverage Success", f"{snapshot.coverage_success_pct:.0f}%", snapshot.coverage_status),
        ("Gap Alert", str(snapshot.gap_alert_count), snapshot.gap_status),
    )
    subtitles = (
        "Statutory labor rules on active shifts",
        "Portage FTE pool targets (MLA · M / MLT · E·N)",
        "Shift slots missing staff right now",
    )
    for col, (label, value, status), subtitle in zip(cols, metrics, subtitles):
        with col:
            st.metric(label, value)
            if status != "healthy":
                st.caption(f"{subtitle} · needs review")
            else:
                st.caption(subtitle)


def _render_manager_health_tab(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period: TenantPeriod,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    assignments: List[Dict],
    compliance_report: ComplianceReport,
    gates: Optional[FeatureGates] = None,
    draft_frame: Optional[pd.DataFrame] = None,
) -> None:
    if gates is not None and _is_self_serve_trial(tenant_id, gates):
        st.caption("Full manager health analytics unlock with Premium.")

    emp_quals = _fetch_employee_qualification_ids(conn, tenant_id)
    qual_map = _qual_code_map(emp_quals)
    template_info = _shift_templates_for_compliance(templates)
    scheduled = _assignments_to_scheduled(assignments, employees)
    profiles = _profiles_from_db(employees, emp_quals)
    profiles_by_id = {profile.id: profile for profile in profiles}
    schedule_archetype = st.session_state.get(
        _schedule_archetype_session_key(period.id),
        ScheduleArchetype.STANDARD.value,
    )
    period_dates = list(_daterange(period.period_start, period.period_end_inclusive))

    load_summary: Optional[LoadTestSummary] = st.session_state.get("portage_load_test_summary")
    if load_summary is not None:
        coverage_pct = load_summary.coverage_success_rate_pct
    else:
        coverage_pct, _ = evaluate_period_coverage(
            rules=rules,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
            weeks_in_period=period.week_count,
            employees=profiles,
            assignments=scheduled,
            shift_templates=template_info,
            qual_codes=qual_map,
        )

    if draft_frame is not None:
        gap_count = count_open_shift_gaps_from_frame(
            draft_frame,
            employees=employees,
            dates=period_dates,
            db_templates=templates,
            shift_templates=template_info,
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
            schedule_archetype=schedule_archetype,
        )
    else:
        gap_count = count_open_shift_gaps(
            period_start=period.period_start,
            period_end=period.period_end_inclusive,
            shift_templates=template_info,
            assignments=scheduled,
            schedule_archetype=schedule_archetype,
        )
    snapshot = build_manager_health_snapshot(
        compliance_error_count=compliance_report.error_count,
        coverage_success_pct=coverage_pct,
        gap_alert_count=gap_count,
    )
    _render_manager_health_summary(snapshot)

    _, tier_results = evaluate_period_coverage(
        rules=rules,
        period_start=period.period_start,
        period_end=period.period_end_inclusive,
        weeks_in_period=period.week_count,
        employees=profiles,
        assignments=scheduled,
        shift_templates=template_info,
        qual_codes=qual_map,
    )
    under_target = build_under_target_roster(
        tier_results,
        employees_by_id=profiles_by_id,
        qual_codes=qual_map,
    )

    if under_target:
        st.markdown("##### Under-Target Roster")
        st.caption(
            "Employees below their Portage pool FTE share. "
            "Prioritize one to highlight them in the scheduling grid and Reactive Swap."
        )
        for row in under_target[:5]:
            col_info, col_action = st.columns([4, 1])
            with col_info:
                st.markdown(
                    f"""
                    <div class="lab-under-target-row">
                      <strong>{html_lib.escape(row.full_name)}</strong>
                      · {html_lib.escape(row.role)}
                      · seniority {row.seniority_hours:,.0f}h
                      · <span style="color:#b45309;font-weight:700;">{row.fte_deficit:+.2f} FTE</span>
                      <div style="font-size:12px;color:#64748b;margin-top:4px;">
                        Scheduled {row.scheduled_hours:.1f}h / pool target {row.period_target_hours:.1f}h
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with col_action:
                if gates is None or not _is_self_serve_trial(tenant_id, gates):
                    if st.button(
                        "Prioritize for Next Fill",
                        key=f"manager_prioritize_{period.id}_{row.employee_id}",
                        width="stretch",
                    ):
                        _set_prioritize_fill(
                            period.id,
                            employee_id=row.employee_id,
                            employee_name=row.full_name,
                            role=row.role,
                        )
                        st.toast(f"Prioritized {row.full_name} for next fill")
                        st.rerun()
        remaining = len(under_target) - 5
        if remaining > 0:
            upgrade_note = (
                f"…and {remaining} more under-target line(s) — upgrade for full 8-week block."
                if gates is not None and _is_self_serve_trial(tenant_id, gates)
                else f"…and {remaining} more under-target line(s)."
            )
            st.caption(upgrade_note)
        if gap_count > 0 and (gates is None or _is_self_serve_trial(tenant_id, gates)):
            st.caption(
                f"{gap_count} open coverage gap(s) — fill manually on the Schedule grid, then publish."
            )
    elif gap_count == 0 and snapshot.coverage_status == "healthy":
        pass
    elif not under_target and gap_count == 0:
        st.caption("No under-target employees for the active period.")



def _render_admin_panel(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    gates: Optional[FeatureGates] = None,
    period: Optional[TenantPeriod] = None,
    rules: Optional[JurisdictionRules] = None,
    employees: Optional[List[Dict]] = None,
    templates: Optional[Dict[str, Dict]] = None,
    assignments: Optional[List[Dict]] = None,
    compliance_report: Optional[ComplianceReport] = None,
    manager_mode: bool = False,
) -> None:
    if gates is not None and _is_self_serve_trial(tenant_id, gates):
        with st.sidebar.expander("Workspace backup", expanded=False):
            _render_restore_tab(conn, tenant_id, period=period)
        return

    if manager_mode:
        focus_active = period is not None and _schedule_focus_active(period.id)
        with st.sidebar.expander("Save / load schedule", expanded=focus_active):
            _render_restore_tab(conn, tenant_id, period=period, manager_mode=True)
        return

    with st.sidebar.expander("Admin Panel · Safety & Ops", expanded=False):
        admin_tab = st.radio(
            "Admin sections",
            ["Manager Health", "Restore"],
            horizontal=True,
            label_visibility="collapsed",
            key="admin_panel_tab",
        )
        if admin_tab == "Manager Health":
            if (
                period is None
                or rules is None
                or employees is None
                or templates is None
                or assignments is None
                or compliance_report is None
            ):
                st.caption("Load a schedule period to view manager health metrics.")
            else:
                _render_manager_health_tab(
                    conn,
                    tenant_id=tenant_id,
                    period=period,
                    rules=rules,
                    employees=employees,
                    templates=templates,
                    assignments=assignments,
                    compliance_report=compliance_report,
                    gates=gates,
                )
        elif admin_tab == "Restore":
            _render_restore_tab(conn, tenant_id)



def _open_revenue_pipeline() -> None:
    """Switch from manager workspace to the in-app Business operator console."""

    st.session_state["force_ops_console"] = True
    request_app_section(st.session_state, "Business")
    request_business_tab(st.session_state, "Pipeline")
    st.session_state.pop("manager_mode", None)


def _return_to_manager_workspace() -> None:
    st.session_state.pop("force_ops_console", None)
    st.session_state["manager_mode"] = True
    request_app_section(st.session_state, "Scheduling")
    st.rerun()


def _render_revenue_pipeline_sidebar(*, manager_entry: bool) -> None:
    """Actionable sidebar entry to Business — no terminal commands required."""

    st.sidebar.markdown("---")
    st.sidebar.markdown("##### Revenue pipeline")
    st.sidebar.caption(
        "North star: **$2,000 CAD/mo** · managed block ($800) + Pro ($299/mo)."
    )

    if st.sidebar.button(
        "Open Revenue Pipeline",
        type="primary",
        width="stretch",
        help="Gather prospects, preview outreach, and onboard paying clients.",
    ):
        _open_revenue_pipeline()
        st.rerun()

    st.sidebar.markdown(
        """
        **Your 3-step path**
        1. Gather prospects — Manitoba hospital labs
        2. Preview email — managed-first pitch
        3. Proceed with client — tenant + onboarding
        """
    )

    with st.sidebar.expander("How this hits $2,000/mo", expanded=False):
        st.caption(
            "Week 1: gather and preview top 3 MB targets. "
            "Week 3–4: close first **$800 managed block**. "
            "Month 2: stack **$299/mo Pro** seats to reach $2,000 MRR."
        )
        if manager_entry:
            st.caption(
                "You stay signed in — the button above opens Business inside this app. "
                "Use **Back to manager workspace** in Business to return to Schedule."
            )


def _render_business_access_hint(*, manager_entry: bool) -> None:
    """Legacy alias — manager sidebar uses actionable revenue pipeline entry."""

    _render_revenue_pipeline_sidebar(manager_entry=manager_entry)


def _render_operator_console_switch() -> None:
    """On app.py, escape manager workspace when tenant config forces manager mode."""

    if st.sidebar.button(
        "Open Revenue Pipeline",
        help="Jump straight to Business: prospects, email preview, client onboarding.",
        width="stretch",
        type="primary",
    ):
        _open_revenue_pipeline()
        st.rerun()

    if st.sidebar.button(
        "Switch to operator console",
        help="Show Scheduling | Business at the top (demo tenants use this by default).",
        width="stretch",
    ):
        st.session_state["force_ops_console"] = True
        st.session_state.pop("manager_mode", None)
        st.rerun()


def _render_operator_section_nav() -> str:
    """Top-level operator nav: Scheduling vs Business (ops console only)."""

    apply_pending_app_section(st.session_state)
    st.markdown("##### Operator console")
    if _manager_entry_requested():
        if st.button("← Back to manager workspace", help="Return to Schedule · Analytics · Print"):
            _return_to_manager_workspace()
    return st.radio(
        "Section",
        options=["Scheduling", "Business"],
        horizontal=True,
        key="app_section",
    )


def _render_business_operator_shell(conn: sqlite3.Connection, tenant_id: str) -> None:
    tenant_name = st.session_state.get("tenant_name", tenant_id)
    username = st.session_state.get("username", "operator")

    st.sidebar.header("Control Panel")
    st.sidebar.markdown(f"**Operator:** `{username}`")
    st.sidebar.caption(f"Tenant context: {tenant_name}")
    if _manager_entry_requested():
        if st.sidebar.button(
            "← Back to manager workspace",
            width="stretch",
            help="Return to Schedule · Analytics · Print without signing out.",
        ):
            _return_to_manager_workspace()
    if st.sidebar.button("Sign Out", width="stretch"):
        _sign_out()
        st.rerun()

    render_business_section(conn)


def _render_production_auth_gate(conn: sqlite3.Connection) -> None:
    st.markdown(
        """
        <div class="lab-login-wrap">
          <p class="lab-login-title">Lab Staffing Scheduler</p>
          <p class="lab-login-sub">
            Sign in to your facility workspace or create a new trial account.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    sign_in_tab, sign_up_tab = st.tabs(["Sign in", "Create account"])

    with sign_in_tab:
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Email or username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
            if submitted and _attempt_login(conn, username=username, password=password):
                st.session_state.pop("signup_error", None)
                st.rerun()

    with sign_up_tab:
        with st.form("signup_form", clear_on_submit=False):
            facility_name = st.text_input("Facility / lab name")
            email = st.text_input("Work email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button(
                "Create trial workspace",
                type="primary",
                width="stretch",
            )
            if submitted:
                st.session_state.pop("login_error", None)
                try:
                    session = register_tenant(
                        conn,
                        facility_name=facility_name,
                        email=email,
                        password=password,
                    )
                except SignupError as exc:
                    st.session_state["signup_error"] = str(exc)
                else:
                    st.session_state.pop("signup_error", None)
                    _establish_auth_session(session)
                    st.rerun()

    login_error = st.session_state.get("login_error")
    if login_error:
        st.error(login_error)
    signup_error = st.session_state.get("signup_error")
    if signup_error:
        st.error(signup_error)


def _run_application_routing(conn: sqlite3.Connection) -> None:
    if not _is_production_runtime():
        ensure_demo_account_credentials(conn)
    apply_pending_app_section(st.session_state)

    if _production_requires_login():
        _render_production_auth_gate(conn)
        return

    tenant_id = _resolve_local_tenant_id(conn)
    if not tenant_id:
        _render_production_auth_gate(conn)
        return

    billing = fetch_tenant_billing(conn, tenant_id)
    gates = feature_gates_for_billing(billing)
    manager_mode = _is_manager_mode(conn, tenant_id)

    if not manager_mode:
        section = _render_operator_section_nav()
        if section == "Business":
            if not _business_console_allowed(tenant_id, gates):
                st.session_state.pop("force_ops_console", None)
                st.session_state["manager_mode"] = True
                request_app_section(st.session_state, "Scheduling")
                st.warning(
                    "Revenue pipeline is for operator accounts. "
                    "Trial workspaces can use Schedule preview only."
                )
                st.rerun()
                return
            _render_business_operator_shell(conn, tenant_id)
            return

    _run_scheduling_dashboard(conn, tenant_id)


def main() -> None:
    page_config: Dict[str, object] = {
        "page_title": "Lab Staffing Scheduler",
        "layout": "wide",
    }
    if _manager_entry_requested():
        st.session_state["manager_mode"] = True
        page_config["initial_sidebar_state"] = "expanded"
    st.set_page_config(**page_config)
    _inject_global_ui_styles()
    _inject_ops_ribbon_live_metrics_listener()

    _ensure_demo_db(DB_PATH)
    quarantine_notice = st.session_state.pop("lab_db_quarantine_notice", None)
    if quarantine_notice:
        st.warning(quarantine_notice)
    roster_notice = st.session_state.pop("lab_roster_restore_notice", None)
    if roster_notice:
        st.success(roster_notice)

    conn = _connect_app_db()
    try:
        _run_application_routing(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
