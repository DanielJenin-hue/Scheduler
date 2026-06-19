
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

import pytest

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import EmployeeProfile, auto_generate_schedule
from lab_scheduler.scheduling.auto_pilot import (
    AutoPilotError,
    assert_monday_block_start,
    build_auto_pilot_proof,
    persist_auto_pilot_schedule,
    run_auto_pilot_full_block,
)
from lab_scheduler.scheduling.strategies import ScheduleArchetype, schedule_archetype_display_label
from lab_scheduler.scheduling.breakroom_print import (
    generate_breakroom_print_html,
    normalize_breakroom_cell,
)
from portage_fixtures import portage_generate_kwargs


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


def _employees() -> list[EmployeeProfile]:
    return [
        EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-b1", "Jordan Patel", 0.8, {"qual-mlt"}),
        EmployeeProfile("emp-c1", "Riley Chen", 0.6, {"qual-mla"}),
    ]


def _required() -> dict[str, set[str]]:
    return {
        "shift-morning": {"qual-mlt", "qual-mla"},
        "shift-evening": {"qual-mlt"},
        "shift-night": {"qual-mlt"},
    }


def test_assert_monday_block_start_accepts_monday() -> None:
    monday = date(2026, 6, 1)
    assert assert_monday_block_start(monday) == monday


def test_assert_monday_block_start_rejects_non_monday() -> None:
    with pytest.raises(AutoPilotError):
        assert_monday_block_start(date(2026, 6, 2))


def test_run_auto_pilot_full_block_returns_proof() -> None:
    kwargs = portage_generate_kwargs(
        strict_complete_block=False,
        coverage_aggressor_mode=True,
    )
    result = run_auto_pilot_full_block(**kwargs)
    assert result.proof.week_count == kwargs["weeks_in_period"]
    assert result.proof.slots_filled > 0


def test_run_auto_pilot_twelve_hour_archetype_completes() -> None:
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
        strict_complete_block=False,
    )
    result = run_auto_pilot_full_block(**kwargs, archetype=ScheduleArchetype.TWELVE_HOUR.value)
    assert result.generate.deterministic_status == "GENERATED"
    assert result.generate.assignments
    assert result.proof.compliance_error_count == 0


def test_schedule_archetype_display_labels() -> None:
    assert schedule_archetype_display_label("STANDARD") == "Regular"
    assert schedule_archetype_display_label("TWELVE_HOUR") == "7-on/7-off"


def test_build_auto_pilot_proof_success_message_zero_ot() -> None:
    kwargs = portage_generate_kwargs(coverage_aggressor_mode=True)
    generate = auto_generate_schedule(**kwargs)
    proof = build_auto_pilot_proof(
        generate=generate,
        rules=kwargs["rules"],
        employees=kwargs["employees"],
        shift_templates=kwargs["shift_templates"],
        period_start=kwargs["period_start"],
        period_end=kwargs["period_end"],
        weeks_in_period=kwargs["weeks_in_period"],
    )
    assert proof.total_statutory_ot_hours >= 0.0
    assert "4-Week Block Generated" in proof.success_message()


def test_persist_auto_pilot_schedule_batch_transaction() -> None:
    import sqlite3

    from lab_scheduler.scheduling.auto_generate import PlannedAssignment

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE shift_assignments (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          assignment_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (tenant_id, employee_id, assignment_date)
        );
        """
    )
    inserted = persist_auto_pilot_schedule(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-a",
        assignments=[
            PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 1)),
            PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 2)),
        ],
    )
    assert inserted == 2
    count = conn.execute("SELECT COUNT(*) FROM shift_assignments").fetchone()[0]
    assert count == 2


def test_persist_auto_pilot_dedupes_duplicate_employee_day() -> None:
    import sqlite3

    from lab_scheduler.scheduling.auto_generate import PlannedAssignment

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE schedule_periods (
          id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          period_start TEXT NOT NULL,
          period_end_inclusive TEXT NOT NULL,
          PRIMARY KEY (tenant_id, id)
        );
        INSERT INTO schedule_periods (id, tenant_id, period_start, period_end_inclusive)
        VALUES ('period-a', 'tenant-a', '2026-06-01', '2026-06-28');
        CREATE TABLE shift_assignments (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          assignment_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (tenant_id, employee_id, assignment_date)
        );
        """
    )
    inserted = persist_auto_pilot_schedule(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-a",
        assignments=[
            PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 1)),
            PlannedAssignment("emp-a1", "shift-evening", date(2026, 6, 1)),
        ],
    )
    assert inserted == 1
    count = conn.execute("SELECT COUNT(*) FROM shift_assignments").fetchone()[0]
    assert count == 1


def test_normalize_breakroom_cell_maps_shift_tokens() -> None:
    assert normalize_breakroom_cell("m") == "D"
    assert normalize_breakroom_cell("M") == "D"
    assert normalize_breakroom_cell("I") == "I"
    assert normalize_breakroom_cell("") == ""


def test_generate_breakroom_print_html_contains_print_rules() -> None:
    html = generate_breakroom_print_html(
        facility_name="Northstar Lab",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        week_count=4,
        employees=[
            {
                "id": "emp-a",
                "full_name": "Avery Miller",
                "fte": 1.0,
                "contract_line_type": "D/E",
            }
        ],
        dates=[date(2026, 6, 1), date(2026, 6, 2)],
        schedule_rows=[
            {
                "Employee": "Avery Miller",
                "employee_id": "emp-a",
                "2026-06-01": "D",
                "2026-06-02": "E",
            },
        ],
    )
    assert "legal landscape" in html
    assert "Northstar Lab" in html
    assert "Shift legend:" in html
    assert "D = Day" in html
    assert "Contract Tracking" in html
    assert "160h target" in html
    assert "contract-ok" in html
    assert "Compliance Verified: Manitoba Labor Standards" in html
    assert "print-token" in html
    assert "print-token-d" in html
    assert "print-token-e" in html
    assert "print-color-adjust: exact" in html
    assert "@media print" in html
