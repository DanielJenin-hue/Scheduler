"""Tests for the date_utils dedup and the flat LLM-availability export."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.date_utils import daterange
from lab_scheduler.scheduling.flat_availability import (
    SCHEMA_VERSION,
    build_llm_constraint_payload,
    default_compliance_constraints,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


# --------------------------------------------------------------------------- #
# date_utils.daterange — equivalence with the legacy inline implementation     #
# --------------------------------------------------------------------------- #
def _legacy_daterange(start: date, end_inclusive: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end_inclusive:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def test_daterange_matches_legacy_behavior():
    start = date(2026, 6, 1)
    for span in range(0, 40):
        end = start + timedelta(days=span)
        assert daterange(start, end) == _legacy_daterange(start, end)


def test_daterange_inclusive_bounds():
    days = daterange(date(2026, 6, 1), date(2026, 6, 3))
    assert days == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]


def test_daterange_single_day():
    assert daterange(date(2026, 6, 1), date(2026, 6, 1)) == [date(2026, 6, 1)]


def test_daterange_reversed_is_empty():
    assert daterange(date(2026, 6, 3), date(2026, 6, 1)) == []


@pytest.mark.legacy
def test_all_scheduling_modules_share_one_daterange():
    """The 6 former _daterange copies must now be the same shared function."""
    from lab_scheduler.scheduling import (
        auto_generate,
        clinical_seats,
        load_balancing,
        night_streak_corrector,
        streak_validator,
    )
    from lab_scheduler.scheduling.strategies import twelve_hour_7on7off_strategy

    for module in (
        auto_generate,
        clinical_seats,
        load_balancing,
        night_streak_corrector,
        streak_validator,
        twelve_hour_7on7off_strategy,
    ):
        assert module._daterange is daterange


# --------------------------------------------------------------------------- #
# flat_availability.build_llm_constraint_payload                               #
# --------------------------------------------------------------------------- #
def _sample_inputs():
    employees = [
        EmployeeProfile(
            id="e1",
            full_name="Line 01",
            fte=1.0,
            qualification_ids={"MLT"},
            contract_line_type="D/N",
        ),
        EmployeeProfile(
            id="e2",
            full_name="Line 02",
            fte=0.5,
            qualification_ids={"MLA"},
            contract_line_type="M-F",
        ),
    ]
    dates = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    templates = {
        "day": ShiftTemplateInfo("day", "MORNING", "Day", "07:00", "19:00", 720, False),
        "night": ShiftTemplateInfo("night", "NIGHT", "Night", "19:00", "07:00", 720, True),
    }
    assignments = [
        PlannedAssignment(employee_id="e1", shift_template_id="day", assignment_date=date(2026, 6, 1)),
        PlannedAssignment(employee_id="e1", shift_template_id="night", assignment_date=date(2026, 6, 2)),
    ]
    blocked = {"e2": {date(2026, 6, 3): "VACATION"}}
    return employees, dates, templates, assignments, blocked


def test_payload_shape_and_schema():
    employees, dates, templates, assignments, blocked = _sample_inputs()
    payload = build_llm_constraint_payload(
        employees=employees,
        dates=dates,
        shift_templates=templates,
        assignments=assignments,
        availability_blocked=blocked,
        target_hours={"e1": 320.0},
    )
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["period"] == {"start": "2026-06-01", "end": "2026-06-03", "days": 3}
    assert payload["dates"] == ["2026-06-01", "2026-06-02", "2026-06-03"]

    emp_by_id = {row["id"]: row for row in payload["employees"]}
    assert emp_by_id["e1"]["tier"] == "D/N"
    assert emp_by_id["e1"]["target_hours"] == 320.0
    assert emp_by_id["e2"]["target_hours"] is None
    assert emp_by_id["e1"]["qualification_ids"] == ["MLT"]


def test_availability_is_complete_grid_with_correct_statuses():
    employees, dates, templates, assignments, blocked = _sample_inputs()
    payload = build_llm_constraint_payload(
        employees=employees,
        dates=dates,
        shift_templates=templates,
        assignments=assignments,
        availability_blocked=blocked,
    )
    rows = payload["availability"]
    # One row per employee x date.
    assert len(rows) == len(employees) * len(dates)

    cells = {(r["employee_id"], r["date"]): r for r in rows}
    assigned = cells[("e1", "2026-06-01")]
    assert assigned["status"] == "assigned"
    assert assigned["shift_code"] == "MORNING"
    assert assigned["shift_template_id"] == "day"

    assert cells[("e1", "2026-06-02")]["shift_code"] == "NIGHT"

    blocked_cell = cells[("e2", "2026-06-03")]
    assert blocked_cell["status"] == "blocked"
    assert blocked_cell["reason"] == "VACATION"

    free = cells[("e2", "2026-06-01")]
    assert free["status"] == "available"
    assert free["shift_code"] is None


def test_blocked_accepts_plain_date_set():
    employees, dates, templates, assignments, _ = _sample_inputs()
    payload = build_llm_constraint_payload(
        employees=employees,
        dates=dates,
        shift_templates=templates,
        assignments=assignments,
        availability_blocked={"e2": {date(2026, 6, 2)}},
    )
    cells = {(r["employee_id"], r["date"]): r for r in payload["availability"]}
    cell = cells[("e2", "2026-06-02")]
    assert cell["status"] == "blocked"
    assert cell["reason"] is None


def test_shift_types_and_demand_tables():
    employees, dates, templates, assignments, blocked = _sample_inputs()
    payload = build_llm_constraint_payload(
        employees=employees,
        dates=dates,
        shift_templates=templates,
        assignments=assignments,
        availability_blocked=blocked,
        daily_demand={date(2026, 6, 1): {"MORNING": 2, "NIGHT": 2}},
    )
    codes = {row["code"] for row in payload["shift_types"]}
    assert codes == {"MORNING", "NIGHT"}

    demand = payload["demand"]
    assert {"date": "2026-06-01", "shift_code": "MORNING", "required": 2} in demand
    assert len(demand) == 2


def test_constraints_emitted_as_data():
    constraints = default_compliance_constraints()
    kinds = {c["kind"] for c in constraints}
    assert "max_consecutive_work_days" in kinds
    assert "min_rest_between_shifts" in kinds
    assert "clinical_floor" in kinds
    # Each constraint is a declarative record, not branching logic.
    for c in constraints:
        assert {"kind", "value"} <= set(c.keys())

    # Can be suppressed.
    employees, dates, templates, assignments, blocked = _sample_inputs()
    bare = build_llm_constraint_payload(
        employees=employees,
        dates=dates,
        shift_templates=templates,
        assignments=assignments,
        availability_blocked=blocked,
        include_constraints=False,
    )
    assert bare["constraints"] == []


def test_payload_is_json_serializable():
    employees, dates, templates, assignments, blocked = _sample_inputs()
    payload = build_llm_constraint_payload(
        employees=employees,
        dates=dates,
        shift_templates=templates,
        assignments=assignments,
        availability_blocked=blocked,
        target_hours={"e1": 320.0},
        daily_demand={date(2026, 6, 1): {"MORNING": 2}},
    )
    # Round-trips cleanly => fully flat / primitive payload.
    encoded = json.dumps(payload)
    assert json.loads(encoded) == payload


def test_empty_period_is_safe():
    payload = build_llm_constraint_payload(employees=[], dates=[])
    assert payload["period"] == {"start": None, "end": None, "days": 0}
    assert payload["availability"] == []
    assert payload["employees"] == []
