from datetime import date

import pandas as pd

from lab_scheduler.scheduling.schedule_tallies import (
    DAILY_TALLY_ROW_NAMES,
    calculate_daily_shift_tallies,
    count_shift_band_in_column,
    is_daily_tally_row,
)
from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows
from lab_scheduler.simulation.portage_blueprint import PORTAGE_ROSTER_SIZE, build_portage_blueprint_roster


def _templates() -> dict[str, dict[str, str]]:
    return {
        "shift-morning": {"code": "MORNING", "short": "D"},
        "shift-evening": {"code": "EVENING", "short": "E"},
        "shift-night": {"code": "NIGHT", "short": "N"},
    }


def test_count_shift_band_in_column_ignores_embedded_tally_rows() -> None:
    frame = pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01",
                "employee_id": "portage-mlt-01",
                "2026-06-01": "E",
                "2026-06-02": "—",
            },
            {
                "Employee": "Vacant MLT D/N - Line 02",
                "employee_id": "portage-mlt-02",
                "2026-06-01": "E",
                "2026-06-02": "N",
            },
            {
                "Employee": "Total Evenings",
                "employee_id": "__tally_total_evenings__",
                "2026-06-01": 2,
                "2026-06-02": 2,
            },
        ]
    )

    assert count_shift_band_in_column(frame, date_key="2026-06-01", band="E") == 2
    assert count_shift_band_in_column(frame, date_key="2026-06-02", band="E") == 0
    assert count_shift_band_in_column(frame, date_key="2026-06-02", band="N") == 1


def test_calculate_daily_shift_tallies_counts_tokens_per_date() -> None:
    dates = ["2026-06-01", "2026-06-02"]
    frame = pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01",
                "employee_id": "portage-mlt-05",
                "fte": 1.0,
                "contract_line_type": "D/E",
                "2026-06-01": "D",
                "2026-06-02": "E",
            },
            {
                "Employee": "Vacant MLA D/N - Line 01",
                "employee_id": "portage-mla-06",
                "fte": 1.0,
                "contract_line_type": "D/N",
                "2026-06-01": "N",
                "2026-06-02": "D",
            },
        ]
    )

    tallies = calculate_daily_shift_tallies(frame, dates=dates)

    assert tallies.days["2026-06-01"] == 1
    assert tallies.days["2026-06-02"] == 1
    assert tallies.evenings["2026-06-01"] == 0
    assert tallies.evenings["2026-06-02"] == 1
    assert tallies.nights["2026-06-01"] == 1
    assert tallies.nights["2026-06-02"] == 0


def test_tally_footer_counts_ignore_embedded_target_rows() -> None:
    frame = pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01",
                "employee_id": "portage-mlt-01",
                "2026-06-01": "D",
                "2026-06-02": "E",
            },
            {
                "Employee": "Total Evenings",
                "employee_id": "__tally_total_evenings__",
                "2026-06-01": 2,
                "2026-06-02": 2,
            },
        ]
    )
    tallies = calculate_daily_shift_tallies(frame, dates=["2026-06-01", "2026-06-02"])
    assert tallies.evenings["2026-06-01"] == 0
    assert tallies.evenings["2026-06-02"] == 1
    assert count_shift_band_in_column(frame, date_key="2026-06-02", band="E") == 1


def test_export_tallies_do_not_mutate_employee_rows() -> None:
    roster = build_portage_blueprint_roster()
    assert len(roster) == PORTAGE_ROSTER_SIZE

    employees = [
        {
            "id": employee.id,
            "full_name": employee.full_name,
            "fte": employee.fte,
            "contract_line_type": employee.contract_line_type,
        }
        for employee in roster
    ]
    dates = [date(2026, 6, 1), date(2026, 6, 2)]
    templates = _templates()
    assignments = [
        {
            "employee_id": "portage-mlt-01",
            "assignment_date": date(2026, 6, 1),
            "shift_template_id": "shift-morning",
        },
        {
            "employee_id": "portage-mla-01",
            "assignment_date": date(2026, 6, 1),
            "shift_template_id": "shift-evening",
        },
        {
            "employee_id": "portage-mlt-02",
            "assignment_date": date(2026, 6, 2),
            "shift_template_id": "shift-night",
        },
    ]

    employee_rows = build_schedule_export_rows(
        employees,
        dates,
        assignments,
        templates,
        include_daily_tallies=False,
    )
    assert len(employee_rows) == PORTAGE_ROSTER_SIZE
    first_before = dict(employee_rows[0])

    export_rows = build_schedule_export_rows(
        employees,
        dates,
        assignments,
        templates,
        include_daily_tallies=True,
    )
    assert len(export_rows) == PORTAGE_ROSTER_SIZE + 3
    assert export_rows[-3]["Employee"] == DAILY_TALLY_ROW_NAMES[0]
    assert export_rows[-2]["Employee"] == DAILY_TALLY_ROW_NAMES[1]
    assert export_rows[-1]["Employee"] == DAILY_TALLY_ROW_NAMES[2]
    assert is_daily_tally_row(export_rows[-1])

    assert employee_rows[0] == first_before
    assert export_rows[0]["employee_id"] == employee_rows[0]["employee_id"]
    assert export_rows[0]["2026-06-01"] == employee_rows[0]["2026-06-01"]

    tallies = calculate_daily_shift_tallies(pd.DataFrame(employee_rows), dates=[d.isoformat() for d in dates])
    assert tallies.days["2026-06-01"] == 1
    assert tallies.evenings["2026-06-01"] == 1
    assert tallies.nights["2026-06-02"] == 1
