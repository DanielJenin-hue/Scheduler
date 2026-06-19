"""Lean, flat availability export for an external LLM constraint engine.

This module is **additive**: it reads the existing scheduling data types and
emits a flat, JSON-serializable payload. It does not modify or replace the
engine, and the engine does not depend on it.

The payload is deliberately denormalized into list-of-record tables so a
downstream constraint solver (or LLM) can consume it without traversing
nested object graphs:

    {
      "schema": "lab_scheduler.flat_availability.v1",
      "period":      {"start", "end", "days"},
      "employees":   [{id, tier, fte, qualification_ids, target_hours}],
      "dates":       ["YYYY-MM-DD", ...],
      "shift_types": [{id, code, name, start, end, duration_minutes, crosses_midnight}],
      "availability":[{employee_id, date, status, shift_code, shift_template_id, reason}],
      "demand":      [{date, shift_code, required}],
      "constraints": [{kind, scope, value, unit, source}],
    }

The ``constraints`` table is the "rules as data" surface: regulatory limits are
emitted as declarative records sourced from the existing engine constants rather
than re-implemented as branching logic here.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Union

from lab_scheduler.compliance.compliance_rules import UNION_MIN_TURNAROUND_HOURS
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.demand import (
    CLINICAL_FLOOR,
    PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
    PORTAGE_MIN_INTER_BLOCK_REST_DAYS,
    WEEKEND_CLINICAL_MAX_PER_QUAL,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile

SCHEMA_VERSION = "lab_scheduler.flat_availability.v1"

# A blocked-availability map may carry just the blocked dates, or dates mapped
# to a reason code.
BlockedMap = Mapping[str, Union[Set[date], Mapping[date, str]]]


def _attr(obj: Any, key: str) -> Any:
    """Read ``key`` from a dataclass-like object or a mapping; else ``None``."""
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def default_compliance_constraints() -> List[Dict[str, object]]:
    """Regulatory guardrails emitted as declarative data (not branching logic).

    Values are sourced from the existing engine constants so this stays in sync
    with the live rules without duplicating their enforcement logic.
    """
    return [
        {
            "kind": "max_consecutive_work_days",
            "scope": "employee",
            "value": PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
            "unit": "days",
            "source": "PORTAGE_MAX_CONSECUTIVE_WORK_DAYS",
        },
        {
            "kind": "min_rest_between_shifts",
            "scope": "employee",
            "value": UNION_MIN_TURNAROUND_HOURS,
            "unit": "hours",
            "source": "UNION_MIN_TURNAROUND_HOURS",
        },
        {
            "kind": "min_inter_block_rest",
            "scope": "employee",
            "value": PORTAGE_MIN_INTER_BLOCK_REST_DAYS,
            "unit": "days",
            "source": "PORTAGE_MIN_INTER_BLOCK_REST_DAYS",
        },
        {
            "kind": "clinical_floor",
            "scope": "shift_band",
            "value": dict(CLINICAL_FLOOR),
            "unit": "seats_per_day",
            "source": "CLINICAL_FLOOR",
        },
        {
            "kind": "weekend_qualification_cap",
            "scope": "weekend",
            "value": dict(WEEKEND_CLINICAL_MAX_PER_QUAL),
            "unit": "assignments_per_qual",
            "source": "WEEKEND_CLINICAL_MAX_PER_QUAL",
        },
    ]


def _blocked_lookup(blocked_for_employee: Any, day: date) -> tuple[bool, Optional[str]]:
    """Return ``(is_blocked, reason)`` for ``day`` given one employee's block entry."""
    if blocked_for_employee is None:
        return False, None
    if isinstance(blocked_for_employee, Mapping):
        if day in blocked_for_employee:
            return True, blocked_for_employee.get(day)
        return False, None
    # Set / iterable of dates.
    return day in blocked_for_employee, None


def build_llm_constraint_payload(
    *,
    employees: Sequence[EmployeeProfile],
    dates: Sequence[date],
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
    assignments: Sequence[Any] = (),
    availability_blocked: Optional[BlockedMap] = None,
    target_hours: Optional[Mapping[str, float]] = None,
    daily_demand: Optional[Mapping[date, Mapping[str, int]]] = None,
    include_constraints: bool = True,
) -> Dict[str, object]:
    """Build a flat, JSON-serializable payload for an external constraint engine.

    Parameters mirror what the generator already has on hand:

    - ``employees``           roster as ``EmployeeProfile`` records
    - ``dates``               the period's calendar days (columns)
    - ``shift_templates``     id -> ``ShiftTemplateInfo`` (for shift_types + tokens)
    - ``assignments``         existing assignments (``PlannedAssignment`` / mappings)
    - ``availability_blocked``employee_id -> blocked dates (set) or date->reason map
    - ``target_hours``        employee_id -> contract target hours
    - ``daily_demand``        date -> {shift_code: required} coverage targets
    - ``include_constraints`` emit the regulatory guardrail records

    Every cell in the ``availability`` table is one of ``assigned`` / ``blocked``
    / ``available`` so the downstream solver never has to infer state.
    """
    shift_templates = shift_templates or {}
    availability_blocked = availability_blocked or {}
    target_hours = target_hours or {}

    sorted_dates = sorted(dates)

    # employee_id -> {date -> (shift_code, shift_template_id)}
    assigned_index: Dict[str, Dict[date, tuple[Optional[str], Optional[str]]]] = {}
    for assignment in assignments:
        emp_id = _attr(assignment, "employee_id")
        day = _attr(assignment, "assignment_date")
        template_id = _attr(assignment, "shift_template_id")
        if emp_id is None or not isinstance(day, date):
            continue
        template = shift_templates.get(template_id) if template_id is not None else None
        shift_code = _attr(template, "code") if template is not None else None
        assigned_index.setdefault(str(emp_id), {})[day] = (shift_code, template_id)

    employees_table: List[Dict[str, object]] = []
    availability_table: List[Dict[str, object]] = []

    for profile in employees:
        emp_id = str(_attr(profile, "id"))
        quals = _attr(profile, "qualification_ids") or ()
        employees_table.append(
            {
                "id": emp_id,
                "tier": _attr(profile, "contract_line_type"),
                "fte": _attr(profile, "fte"),
                "qualification_ids": sorted(str(q) for q in quals),
                "target_hours": target_hours.get(emp_id),
            }
        )

        emp_assigned = assigned_index.get(emp_id, {})
        emp_blocked = availability_blocked.get(emp_id)
        for day in sorted_dates:
            if day in emp_assigned:
                shift_code, template_id = emp_assigned[day]
                availability_table.append(
                    {
                        "employee_id": emp_id,
                        "date": day.isoformat(),
                        "status": "assigned",
                        "shift_code": shift_code,
                        "shift_template_id": template_id,
                        "reason": None,
                    }
                )
                continue
            is_blocked, reason = _blocked_lookup(emp_blocked, day)
            if is_blocked:
                availability_table.append(
                    {
                        "employee_id": emp_id,
                        "date": day.isoformat(),
                        "status": "blocked",
                        "shift_code": None,
                        "shift_template_id": None,
                        "reason": reason,
                    }
                )
                continue
            availability_table.append(
                {
                    "employee_id": emp_id,
                    "date": day.isoformat(),
                    "status": "available",
                    "shift_code": None,
                    "shift_template_id": None,
                    "reason": None,
                }
            )

    shift_types_table = [
        {
            "id": _attr(template, "id"),
            "code": _attr(template, "code"),
            "name": _attr(template, "name"),
            "start": _attr(template, "start_time"),
            "end": _attr(template, "end_time"),
            "duration_minutes": _attr(template, "duration_minutes"),
            "crosses_midnight": _attr(template, "crosses_midnight"),
        }
        for template in shift_templates.values()
    ]

    demand_table: List[Dict[str, object]] = []
    if daily_demand:
        for day in sorted(daily_demand):
            for shift_code, required in daily_demand[day].items():
                demand_table.append(
                    {
                        "date": day.isoformat(),
                        "shift_code": shift_code,
                        "required": required,
                    }
                )

    payload: Dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "period": {
            "start": sorted_dates[0].isoformat() if sorted_dates else None,
            "end": sorted_dates[-1].isoformat() if sorted_dates else None,
            "days": len(sorted_dates),
        },
        "employees": employees_table,
        "dates": [day.isoformat() for day in sorted_dates],
        "shift_types": shift_types_table,
        "availability": availability_table,
        "demand": demand_table,
        "constraints": default_compliance_constraints() if include_constraints else [],
    }
    return payload
