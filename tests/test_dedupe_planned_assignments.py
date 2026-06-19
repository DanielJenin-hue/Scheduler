from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


from datetime import date

from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments


def test_dedupe_prefers_evening_over_day_for_same_employee_date() -> None:
    day = date(2026, 6, 1)
    assignments = [
        PlannedAssignment(
            employee_id="emp-1",
            shift_template_id="shift-evening",
            assignment_date=day,
        ),
        PlannedAssignment(
            employee_id="emp-1",
            shift_template_id="shift-morning",
            assignment_date=day,
        ),
    ]
    bands = {
        "shift-evening": "E",
        "shift-morning": "D",
    }
    deduped = dedupe_planned_assignments(assignments, template_id_to_band=bands)
    assert len(deduped) == 1
    assert deduped[0].shift_template_id == "shift-evening"


def test_dedupe_resolves_evening_night_conflict_for_day_tally() -> None:
    day = date(2026, 7, 8)
    bands = {
        "shift-evening": "E",
        "shift-night": "N",
        "shift-morning": "D",
    }
    assignments = [
        PlannedAssignment(
            employee_id="emp-mlt",
            shift_template_id="shift-evening",
            assignment_date=day,
        ),
        PlannedAssignment(
            employee_id="emp-mla",
            shift_template_id="shift-evening",
            assignment_date=day,
        ),
        PlannedAssignment(
            employee_id="emp-dn",
            shift_template_id="shift-evening",
            assignment_date=day,
        ),
        PlannedAssignment(
            employee_id="emp-dn",
            shift_template_id="shift-night",
            assignment_date=day,
        ),
        PlannedAssignment(
            employee_id="emp-dn",
            shift_template_id="shift-morning",
            assignment_date=day,
        ),
    ]
    deduped = dedupe_planned_assignments(assignments, template_id_to_band=bands)
    by_emp = {item.employee_id: item.shift_template_id for item in deduped}
    assert by_emp["emp-mlt"] == "shift-evening"
    assert by_emp["emp-mla"] == "shift-evening"
    assert by_emp["emp-dn"] == "shift-night"
