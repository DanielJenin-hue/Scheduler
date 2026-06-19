"""Shared Portage Auto-Pilot entry point for Streamlit UI and CLI harness."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.constraints import portage_coverage_targets
from lab_scheduler.scheduling.adaptive_auto_pilot import run_adaptive_auto_pilot_ladder
from lab_scheduler.scheduling.auto_pilot import run_auto_pilot_full_block
from lab_scheduler.scheduling.portage_equity_policy import (
    PortageSchedulingPolicy,
    resolve_portage_scheduling_policy,
)
from lab_scheduler.scheduling.equitability_score import FairnessWeights
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_families import ScheduleFamily, resolve_schedule_family
from lab_scheduler.scheduling.strategies import ScheduleArchetype

# Bump when generator / persist gate behavior changes (shown in UI for support).
PORTAGE_GENERATOR_BUILD = "2026-06-10-dn-exclusive-pool-v14"


@dataclass(frozen=True, slots=True)
class PortageAutoPilotRunConfig:
    rules: JurisdictionRules
    period_start: date
    period_end: date
    weeks_in_period: int
    employees: Sequence[EmployeeProfile]
    shift_templates: Mapping[str, ShiftTemplateInfo]
    shift_required_qualifications: Mapping[str, Set[str]]
    employee_target_hours: Mapping[str, float]
    availability_blocked: Mapping[str, Set[date]]
    coverage_targets: Sequence
    scheduling_policy: PortageSchedulingPolicy
    archetype: str = ScheduleArchetype.STANDARD.value
    bypass_compliance_rules: bool = False
    emit_triage: bool = True
    is_self_serve_trial: bool = False
    conn: Optional[sqlite3.Connection] = None
    tenant_id: Optional[str] = None
    schedule_period_id: Optional[str] = None
    project_root: Optional[Path] = None
    progress_callback: Optional[Callable[[str], None]] = None
    fairness_weights: FairnessWeights | None = None


def run_portage_auto_pilot_ladder(
    config: PortageAutoPilotRunConfig,
):
    """Run adaptive Auto-Pilot with identical settings for UI and CLI."""

    family_ctx = resolve_schedule_family(
        archetype=config.archetype,
        has_portage_coverage_targets=bool(config.coverage_targets),
        is_self_serve_trial=config.is_self_serve_trial,
    )
    defer_fairness_rerun = family_ctx.family is ScheduleFamily.PORTAGE_STANDARD
    return run_adaptive_auto_pilot_ladder(
        run_auto_pilot_full_block,
        allow_preview_tier=family_ctx.allow_preview_tier,
        require_complete_for_success=family_ctx.require_complete_for_success,
        family=family_ctx.family,
        rules=config.rules,
        period_start=config.period_start,
        period_end=config.period_end,
        weeks_in_period=config.weeks_in_period,
        employees=config.employees,
        shift_templates=dict(config.shift_templates),
        shift_required_qualifications=dict(config.shift_required_qualifications),
        employee_target_hours=config.employee_target_hours,
        availability_blocked=config.availability_blocked,
        bypass_compliance_rules=config.bypass_compliance_rules,
        coverage_targets=config.coverage_targets,
        emit_triage=config.emit_triage,
        conn=config.conn,
        tenant_id=config.tenant_id,
        schedule_period_id=config.schedule_period_id,
        project_root=config.project_root,
        clear_provisional_state=False,
        archetype=config.archetype,
        progress_callback=config.progress_callback,
        enable_fairness_rerun=not defer_fairness_rerun,
        portage_scheduling_policy=config.scheduling_policy,
        fairness_weights=config.fairness_weights,
    )


def build_portage_coverage_targets(profiles: Sequence[EmployeeProfile]):
    return portage_coverage_targets(profiles)


def default_scheduling_policy(policy_id: Optional[str] = None) -> PortageSchedulingPolicy:
    return resolve_portage_scheduling_policy(policy_id)
