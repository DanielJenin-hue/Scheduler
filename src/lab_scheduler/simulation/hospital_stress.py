from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Set

from lab_scheduler.availability.exceptions import (
    AvailabilityException,
    blocked_dates_by_employee,
    compute_employee_target_hours,
    expand_blocked_dates,
)
from lab_scheduler.compliance import MANITOBA, ScheduledShift, ShiftTemplateInfo, evaluate_schedule
from lab_scheduler.scheduling.auto_generate import EmployeeProfile, auto_generate_schedule

SIM_TENANT_ID = "tenant-stress-sim"
PERIOD_START = date(2026, 6, 1)
PERIOD_END = date(2026, 6, 28)
WEEKS_IN_PERIOD = 4

QUAL_MLT = "qual-mlt"
QUAL_MLA = "qual-mla"

MLT_FTE_TIERS: tuple[tuple[int, float], ...] = (
    (10, 1.0),
    (6, 0.8),
    (4, 0.6),
    (2, 0.4),
)
MLA_FTE_TIERS: tuple[tuple[int, float], ...] = (
    (4, 1.0),
    (3, 0.8),
    (4, 0.6),
    (2, 0.2),
)


@dataclass(frozen=True, slots=True)
class StressSimResult:
    execution_seconds: float
    slots_total: int
    slots_filled: int
    fill_rate_pct: float
    total_statutory_ot_hours: float
    unfilled_slots: int
    roster_size: int
    blocked_day_count: int
    exception_occurred: bool
    exception_message: str


def shift_templates() -> Dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
        "shift-evening": ShiftTemplateInfo(
            "shift-evening", "EVENING", "Evening", "15:00", "23:00", 480, False
        ),
        "shift-night": ShiftTemplateInfo(
            "shift-night", "NIGHT", "Night", "23:00", "07:00", 480, True
        ),
    }


def shift_required_qualifications() -> Dict[str, Set[str]]:
    """All bands accept both MLT and MLA so D/E and D/N lines can rotate across contract sets."""

    return {
        "shift-morning": {QUAL_MLT, QUAL_MLA},
        "shift-evening": {QUAL_MLT, QUAL_MLA},
        "shift-night": {QUAL_MLT, QUAL_MLA},
    }


def build_hospital_roster() -> List[EmployeeProfile]:
    roster: List[EmployeeProfile] = []
    seq = 1
    for count, fte in MLT_FTE_TIERS:
        for _ in range(count):
            roster.append(
                EmployeeProfile(
                    id=f"emp-mlt-{seq:02d}",
                    full_name=f"MLT Tech {seq:02d}",
                    fte=fte,
                    qualification_ids={QUAL_MLT},
                )
            )
            seq += 1

    seq = 1
    for count, fte in MLA_FTE_TIERS:
        for _ in range(count):
            roster.append(
                EmployeeProfile(
                    id=f"emp-mla-{seq:02d}",
                    full_name=f"MLA Assistant {seq:02d}",
                    fte=fte,
                    qualification_ids={QUAL_MLA},
                )
            )
            seq += 1

    return roster


def build_crisis_availability(employees: List[EmployeeProfile]) -> List[AvailabilityException]:
    mlt_ids = [e.id for e in employees if QUAL_MLT in e.qualification_ids]
    mla_ids = [e.id for e in employees if QUAL_MLA in e.qualification_ids]

    vacation_ids = [mlt_ids[0], mlt_ids[1], mla_ids[0], mla_ids[1]]
    sick_ids = [mlt_ids[2], mlt_ids[3], mla_ids[2]]

    exceptions: List[AvailabilityException] = []
    for idx, emp_id in enumerate(vacation_ids):
        exceptions.append(
            AvailabilityException(
                id=f"avail-vac-{idx + 1}",
                tenant_id=SIM_TENANT_ID,
                employee_id=emp_id,
                start_date=date(2026, 6, 8),
                end_date=date(2026, 6, 21),
                reason="Vacation",
            )
        )

    sick_windows = (
        (date(2026, 6, 3), date(2026, 6, 5)),
        (date(2026, 6, 10), date(2026, 6, 12)),
        (date(2026, 6, 17), date(2026, 6, 19)),
    )
    for idx, (emp_id, (start, end)) in enumerate(zip(sick_ids, sick_windows)):
        exceptions.append(
            AvailabilityException(
                id=f"avail-sick-{idx + 1}",
                tenant_id=SIM_TENANT_ID,
                employee_id=emp_id,
                start_date=start,
                end_date=end,
                reason="Sick Leave",
            )
        )

    return exceptions


def run_hospital_stress_simulation() -> StressSimResult:
    exception_occurred = False
    exception_message = ""
    execution_seconds = 0.0
    fill_rate_pct = 0.0
    slots_total = 0
    slots_filled = 0
    unfilled_slots = 0
    total_ot = 0.0
    blocked_day_count = 0

    t0 = time.perf_counter()
    try:
        employees = build_hospital_roster()
        templates = shift_templates()
        shift_quals = shift_required_qualifications()
        availability = build_crisis_availability(employees)

        blocked_map = expand_blocked_dates(
            availability,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        blocked_sets = blocked_dates_by_employee(blocked_map)
        blocked_day_count = sum(len(days) for days in blocked_map.values())

        employee_dicts = [{"id": e.id, "full_name": e.full_name, "fte": e.fte} for e in employees]
        target_hours = compute_employee_target_hours(
            rules=MANITOBA,
            employees=employee_dicts,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            weeks_in_period=WEEKS_IN_PERIOD,
            blocked_map=blocked_map,
        )

        gen = auto_generate_schedule(
            rules=MANITOBA,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            weeks_in_period=WEEKS_IN_PERIOD,
            employees=employees,
            shift_templates=templates,
            shift_required_qualifications=shift_quals,
            employee_target_hours=target_hours,
            availability_blocked=blocked_sets,
        )

        names = {e.id: e.full_name for e in employees}
        scheduled = [
            ScheduledShift(
                employee_id=a.employee_id,
                employee_name=names.get(a.employee_id, a.employee_id),
                assignment_date=a.assignment_date,
                shift_template_id=a.shift_template_id,
            )
            for a in gen.assignments
        ]
        report = evaluate_schedule(
            MANITOBA,
            employees=employee_dicts,
            assignments=scheduled,
            shift_templates=templates,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            weeks_in_period=WEEKS_IN_PERIOD,
            employee_target_hours=target_hours,
        )

        slots_total = gen.slots_total
        slots_filled = gen.slots_filled
        fill_rate_pct = gen.fill_rate_pct
        unfilled_slots = len(gen.unfilled)
        total_ot = round(
            sum(s.statutory_overtime_hours for s in report.labor_summaries),
            2,
        )
    except Exception as exc:
        exception_occurred = True
        exception_message = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        execution_seconds = time.perf_counter() - t0

    return StressSimResult(
        execution_seconds=round(execution_seconds, 3),
        slots_total=slots_total,
        slots_filled=slots_filled,
        fill_rate_pct=round(fill_rate_pct, 2),
        total_statutory_ot_hours=total_ot,
        unfilled_slots=unfilled_slots,
        roster_size=35,
        blocked_day_count=blocked_day_count,
        exception_occurred=exception_occurred,
        exception_message=exception_message,
    )
