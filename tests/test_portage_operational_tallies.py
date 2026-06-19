from datetime import date

from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.schedule_tallies import (
    find_portage_operational_tally_violations,
    format_portage_tally_violation_summary,
    shift_band_from_template_code,
)


def test_shift_band_from_template_code() -> None:
    assert shift_band_from_template_code("MORNING") == "D"
    assert shift_band_from_template_code("EVENING") == "E"
    assert shift_band_from_template_code("NIGHT") == "N"


def test_find_portage_operational_tally_violations_evening_and_night() -> None:
    template_bands = {
        "shift-evening": "E",
        "shift-night": "N",
    }
    assignments = [
        PlannedAssignment("emp-1", "shift-evening", date(2026, 6, 1)),
        PlannedAssignment("emp-2", "shift-evening", date(2026, 6, 1)),
        PlannedAssignment("emp-3", "shift-night", date(2026, 6, 1)),
    ]
    violations = find_portage_operational_tally_violations(
        assignments,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 1),
        template_id_to_band=template_bands,
    )
    assert len(violations) == 1
    assert violations[0].band == "N"
    assert violations[0].actual == 1
    assert violations[0].target == 2


def test_format_portage_tally_violation_summary() -> None:
    from lab_scheduler.scheduling.schedule_tallies import PortageOperationalTallyViolation

    text = format_portage_tally_violation_summary(
        [
            PortageOperationalTallyViolation(
                assignment_date=date(2026, 6, 22),
                band="E",
                actual=0,
                target=2,
            )
        ]
    )
    assert "2026-06-22 E 0/2" in text
