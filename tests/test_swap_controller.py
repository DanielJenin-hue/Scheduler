from datetime import date

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.compliance.engine import ScheduledShift
from lab_scheduler.engine.swap_controller import (
    ScheduleState,
    get_eligible_swap_candidates,
)
from lab_scheduler.scheduling.auto_generate import EmployeeProfile


def _templates() -> dict[str, ShiftTemplateInfo]:
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


def _required() -> dict[str, set[str]]:
    return {
        "shift-morning": {"qual-mlt", "qual-mla"},
        "shift-evening": {"qual-mlt", "qual-mla"},
        "shift-night": {"qual-mlt", "qual-mla"},
    }


def test_get_eligible_swap_candidates_filters_contract_line_and_ranks_deficit() -> None:
    """D/E workers cannot take Night; under-target staff rank above filled lines."""

    employees = [
        EmployeeProfile(
            "portage-mlt-01",
            "Vacant MLT D/N - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        ),
        EmployeeProfile(
            "portage-mlt-02",
            "Vacant MLT D/N - Line 02",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        ),
        EmployeeProfile(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "portage-mlt-11",
            "Vacant MLT D/E - Line 07",
            0.7,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
    ]
    assignments = [
        ScheduledShift("portage-mlt-01", "Vacant MLT D/N - Line 01", date(2026, 6, 1), "shift-night"),
        ScheduledShift("portage-mlt-02", "Vacant MLT D/N - Line 02", date(2026, 6, 2), "shift-morning"),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 2),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 3),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 4),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 5),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 8),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 9),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 10),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 11),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 12),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 15),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 16),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 17),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 18),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-05",
            "Vacant MLT D/E - Line 01",
            date(2026, 6, 19),
            "shift-evening",
        ),
        ScheduledShift(
            "portage-mlt-11",
            "Vacant MLT D/E - Line 07",
            date(2026, 6, 2),
            "shift-evening",
        ),
    ]
    target_hours = {
        "portage-mlt-01": 160.0,
        "portage-mlt-02": 160.0,
        "portage-mlt-05": 160.0,
        "portage-mlt-11": 112.0,
    }
    state = ScheduleState(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employees=employees,
        assignments=assignments,
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
        employee_target_hours=target_hours,
    )

    candidates = get_eligible_swap_candidates(
        state,
        target_employee_id="portage-mlt-01",
        target_date=date(2026, 6, 6),
        target_shift_type="NIGHT",
        include_ineligible=True,
    )

    eligible_ids = [candidate.employee_id for candidate in candidates if candidate.is_eligible]
    blocked_de = next(
        candidate for candidate in candidates if candidate.employee_id == "portage-mlt-05"
    )

    assert "portage-mlt-05" not in eligible_ids
    assert blocked_de.is_eligible is False
    assert blocked_de.block_reason is not None
    assert "CRITICAL" in blocked_de.block_reason or "ineligible" in blocked_de.block_reason.lower()

    assert eligible_ids[0] == "portage-mlt-02"
    top = candidates[0]
    assert top.is_eligible is True
    assert top.hour_deficit > 0.0
