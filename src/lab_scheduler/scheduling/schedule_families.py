"""Schedule family registry, detection, and per-family persist validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Mapping, Optional, Sequence

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.auto_pilot import AutoPilotRunResult
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.persist_validation import (
    collect_live_clinical_gap_messages,
    find_core_persist_violations,
    format_core_persist_blocked_message,
    log_core_persist_violations,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import (
    find_portage_operational_tally_violations,
    format_portage_tally_violation_summary,
)
from lab_scheduler.scheduling.strategies import ScheduleArchetype, normalize_archetype
from lab_scheduler.engine.constraints import CoverageTierResult


class ScheduleFamily(str, Enum):
    PORTAGE_STANDARD = "PORTAGE_STANDARD"
    TWELVE_HOUR_7ON7OFF = "TWELVE_HOUR_7ON7OFF"
    GENERIC_LEGACY = "GENERIC_LEGACY"


@dataclass(frozen=True, slots=True)
class ScheduleFamilyContext:
    family: ScheduleFamily
    allow_preview_tier: bool
    require_complete_for_success: bool

    @property
    def display_name(self) -> str:
        if self.family is ScheduleFamily.PORTAGE_STANDARD:
            return "Portage master rotation (M/E/N)"
        if self.family is ScheduleFamily.TWELVE_HOUR_7ON7OFF:
            return "7-on/7-off (12-hour)"
        return "Generic hourly balancing"


@dataclass(frozen=True, slots=True)
class PersistValidationResult:
    ok: bool
    error_message: str = ""
    infeasibility_notes: tuple[str, ...] = ()


def is_portage_roster(employees: Sequence[object]) -> bool:
    """True when roster looks like the Portage vacant-line blueprint."""

    for employee in employees:
        if isinstance(employee, Mapping):
            name = str(employee.get("full_name", ""))
            emp_id = str(employee.get("id", ""))
        else:
            name = str(getattr(employee, "full_name", ""))
            emp_id = str(getattr(employee, "id", ""))
        if "Vacant MLT" in name or "Vacant MLA" in name:
            return True
        if emp_id.startswith("portage-"):
            return True
    return False


def resolve_schedule_family(
    *,
    archetype: str,
    has_portage_coverage_targets: bool,
    is_self_serve_trial: bool,
) -> ScheduleFamilyContext:
    normalized = normalize_archetype(archetype)
    if normalized is ScheduleArchetype.TWELVE_HOUR:
        return ScheduleFamilyContext(
            family=ScheduleFamily.TWELVE_HOUR_7ON7OFF,
            allow_preview_tier=is_self_serve_trial,
            require_complete_for_success=not is_self_serve_trial,
        )
    if has_portage_coverage_targets:
        return ScheduleFamilyContext(
            family=ScheduleFamily.PORTAGE_STANDARD,
            allow_preview_tier=is_self_serve_trial,
            require_complete_for_success=not is_self_serve_trial,
        )
    return ScheduleFamilyContext(
        family=ScheduleFamily.GENERIC_LEGACY,
        allow_preview_tier=is_self_serve_trial,
        require_complete_for_success=not is_self_serve_trial,
    )


def build_infeasibility_notes(
    pilot: AutoPilotRunResult,
    *,
    tier_results: Optional[Sequence[CoverageTierResult]] = None,
) -> tuple[str, ...]:
    """Actionable hints when Auto-Pilot cannot produce a postable schedule."""

    notes: list[str] = []
    tiers = tier_results or pilot.generate.coverage_tier_results
    for tier in tiers:
        if tier.is_impossible:
            notes.append(
                f"Coverage tier {tier.label!r} is mathematically impossible "
                f"(target {tier.target_fte:.2f} FTE, roster supplies {tier.actual_fte:.2f} FTE)."
            )
        elif not tier.meets_target and tier.gap_fte > 0.01:
            notes.append(
                f"{tier.label}: short {tier.gap_fte:.2f} FTE vs target "
                f"({tier.actual_fte:.2f}/{tier.target_fte:.2f})."
            )

    gap_reports = getattr(pilot.generate, "clinical_gap_reports", ()) or ()
    for gap in gap_reports[:4]:
        notes.append(
            f"Clinical gap {gap.assignment_date.isoformat()} {gap.shift_code}: "
            f"{getattr(gap, 'reason', 'unfilled seat')}."
        )

    critical = getattr(pilot.generate, "critical_clinical_gaps", ()) or ()
    for gap in critical[:4]:
        notes.append(
            f"Critical clinical {gap.assignment_date.isoformat()} "
            f"{gap.shift_code} {gap.seat_label}: {gap.reason}."
        )

    if pilot.proof.coverage_gap_count > 0:
        notes.append(
            f"{pilot.proof.coverage_gap_count} required demand seat(s) unfilled in the "
            f"generated attempt ({pilot.proof.coverage_success_rate_pct:.0f}% tier coverage). "
            "This counts Portage demand-matrix seats, not necessarily open cells in the "
            "saved grid."
        )

    return tuple(dict.fromkeys(notes))


def format_persist_blocked_message(
    *,
    detail: str,
    infeasibility_notes: Sequence[str],
) -> str:
    """User-facing blocked copy: generated schedule rejected, DB grid unchanged."""

    body = detail.strip()
    if not body and infeasibility_notes:
        body = " ".join(infeasibility_notes[:3])
    return (
        "**Auto-Pilot blocked:** Your saved schedule was **not** replaced. "
        "A postable block requires full Portage coverage (including 2 Evening and "
        "2 Night seats per day). "
        f"{body} "
        "Adjust roster, availability, or templates and re-run Auto-Pilot."
    )


def validate_persist_gate(
    pilot: AutoPilotRunResult,
    *,
    family: ScheduleFamily,
    assignments: Sequence[PlannedAssignment],
    period_start: date,
    period_end: date,
    template_id_to_band: Mapping[str, str],
    allow_partial_persist: bool,
    employees: Optional[Sequence[EmployeeProfile]] = None,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
    rules: Optional[JurisdictionRules] = None,
    weeks_in_period: int = 8,
    qual_codes: Optional[Mapping[str, str]] = None,
    compliance_first: bool = False,
) -> PersistValidationResult:
    """Return ok=False with a user-facing message when persist must be blocked."""

    if allow_partial_persist:
        return PersistValidationResult(ok=True)

    infeasibility = build_infeasibility_notes(pilot)
    compliance_first = compliance_first or bool(
        getattr(pilot.generate, "compliance_first", False)
    )

    if family is ScheduleFamily.PORTAGE_STANDARD and employees and shift_templates and rules:
        live_gap_messages = collect_live_clinical_gap_messages(
            assignments=assignments,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes or {},
            period_start=period_start,
            period_end=period_end,
        )
        generate_gap_count = int(getattr(pilot.generate, "coverage_gap_count", 0) or 0)
        required_total = getattr(pilot.generate, "required_slots_total", None)
        required_filled = getattr(pilot.generate, "required_slots_filled", None)
        if required_total is not None and required_filled is not None:
            coverage_gap_count = max(0, int(required_total) - int(required_filled))
            coverage_complete = coverage_gap_count == 0
        else:
            coverage_gap_count = generate_gap_count or int(pilot.proof.coverage_gap_count or 0)
            coverage_complete = pilot.proof.coverage_complete and coverage_gap_count == 0
        core_violations = find_core_persist_violations(
            assignments=assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            rules=rules,
            qual_codes=qual_codes or {},
            template_id_to_band=template_id_to_band,
            coverage_complete=coverage_complete,
            coverage_gap_count=coverage_gap_count,
            clinical_gap_messages=live_gap_messages,
            compliance_first=compliance_first,
            recompute_clinical_gaps=True,
        )
        if core_violations:
            log_core_persist_violations(core_violations)
            return PersistValidationResult(
                ok=False,
                error_message=format_core_persist_blocked_message(core_violations),
                infeasibility_notes=infeasibility,
            )
        return PersistValidationResult(ok=True)

    if not pilot.proof.coverage_complete:
        detail = " ".join(infeasibility[:4]) if infeasibility else (
            f"{pilot.proof.coverage_gap_count} required demand seat(s) remain after adaptive fill."
        )
        return PersistValidationResult(
            ok=False,
            error_message=format_persist_blocked_message(
                detail=detail,
                infeasibility_notes=infeasibility,
            ),
            infeasibility_notes=infeasibility,
        )

    if family is ScheduleFamily.PORTAGE_STANDARD:
        gap_reports = getattr(pilot.generate, "clinical_gap_reports", ()) or ()
        if gap_reports:
            sample = "; ".join(
                f"{g.assignment_date.isoformat()} {g.shift_code}"
                for g in gap_reports[:3]
            )
            return PersistValidationResult(
                ok=False,
                error_message=(
                    "**Auto-Pilot blocked:** Portage clinical floor gaps remain "
                    f"({sample}). Evening and Night require exactly 2 seats per day "
                    "(1 MLT + 1 MLA)."
                ),
                infeasibility_notes=infeasibility,
            )

        tally_violations = find_portage_operational_tally_violations(
            assignments,
            period_start=period_start,
            period_end=period_end,
            template_id_to_band=template_id_to_band,
        )
        if tally_violations:
            summary = format_portage_tally_violation_summary(tally_violations)
            return PersistValidationResult(
                ok=False,
                error_message=(
                    "**Auto-Pilot blocked:** Portage evening/night tallies must be "
                    "**exactly 2 per day** (1 MLT + 1 MLA on the clinical floor). "
                    f"{summary}"
                ),
                infeasibility_notes=infeasibility,
            )

    return PersistValidationResult(ok=True)


def auto_pilot_family_help_copy(family: ScheduleFamily, *, is_trial: bool) -> str:
    if family is ScheduleFamily.PORTAGE_STANDARD:
        base = (
            "Portage master rotation: clinical floor **2 Evening + 2 Night** every day, "
            "contract-line D/E and D/N patterns, union caps. "
        )
    elif family is ScheduleFamily.TWELVE_HOUR_7ON7OFF:
        base = (
            "7-on/7-off mode: staggered 12-hour master array for full-time lines only. "
        )
    else:
        base = (
            "Generic mode: tier-based coverage balancing (configure demand targets for "
            "Portage-grade validation). "
        )
    if is_trial:
        return base + "Trial runs a preview ladder and may save partial grids."
    return base + "Premium saves only complete, postable schedules or blocks with a clear reason."


def auto_pilot_status_label(family_ctx: ScheduleFamilyContext) -> str:
    """Initial Streamlit status copy with honest runtime expectations."""

    if family_ctx.family is ScheduleFamily.PORTAGE_STANDARD:
        return "Auto-Pilot: Portage fill (target ~60–90s)…"
    if family_ctx.family is ScheduleFamily.TWELVE_HOUR_7ON7OFF:
        return "Auto-Pilot: 7-on/7-off stamp (usually under 1 min)…"
    return "Auto-Pilot: filling vacant lines (solver ~1–2 min)…"
