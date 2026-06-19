
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.auto_generate import auto_generate_schedule
from lab_scheduler.scheduling.coverage_aggressor import (
    AggressiveFillFlag,
    format_aggressive_fill_flags_csv_rows,
    format_aggressive_fill_flags_html,
)
from lab_scheduler.scheduling.schedule_export import (
    is_aggressive_fill_flag_row,
    prepend_aggressive_fill_flags_to_export_rows,
)
from lab_scheduler.simulation.hospital_stress import shift_required_qualifications, shift_templates
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_coverage_targets,
)


def _portage_generate(*, coverage_aggressor_mode: bool = False):
    employees = build_portage_roster()
    return auto_generate_schedule(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employees=employees,
        shift_templates=shift_templates(),
        shift_required_qualifications=shift_required_qualifications(),
        coverage_targets=portage_coverage_targets(employees),
        require_master_compliance=True,
        coverage_aggressor_mode=coverage_aggressor_mode,
    )


def test_coverage_aggressor_exports_without_hard_stop() -> None:
    result = _portage_generate(coverage_aggressor_mode=True)
    assert result.deterministic_status == "AGGRESSOR_GENERATED"
    assert result.coverage_aggressor_mode is True
    assert result.breakroom_export_path
    assert len(result.aggressive_fill_flags) > 0


def test_aggressive_fill_flags_csv_header() -> None:
    flags = [
        AggressiveFillFlag(
            category="contract_fte",
            code="CONTRACT_FTE_160",
            message="scheduled 128.0h vs 160h contract target",
            employee_name="Vacant MLT D/N - Line 01",
        )
    ]
    rows = format_aggressive_fill_flags_csv_rows(flags)
    assert rows[0]["Employee"] == "AGGRESSIVE_FILL_FLAGS"
    assert rows[1]["employee_id"] == "CONTRACT_FTE_160"


def test_prepend_aggressive_fill_flags_to_export_rows() -> None:
    flags = [
        AggressiveFillFlag(
            category="manitoba_union",
            code="MAX_WEEKLY_HOURS",
            message="48.0h in work week (limit 40h).",
        )
    ]
    schedule_rows = [
        {
            "Employee": "Vacant MLT D/E - Line 01",
            "employee_id": "portage-mlt-05",
            "fte": "1.0",
            "contract_line_type": "D/E",
            "2026-06-01": "D",
        }
    ]
    merged = prepend_aggressive_fill_flags_to_export_rows(schedule_rows, flags)
    assert merged[0]["Employee"] == "AGGRESSIVE_FILL_FLAGS"
    assert is_aggressive_fill_flag_row(merged[0])
    assert not is_aggressive_fill_flag_row(merged[-1])


def test_aggressive_fill_flags_html_section() -> None:
    html = format_aggressive_fill_flags_html(
        [
            AggressiveFillFlag(
                category="overtime",
                code="OVERTIME_REQUIRED_COMPLIANCE_BYPASSED",
                message="weekly-hour compliance bypassed",
            )
        ]
    )
    assert "AGGRESSIVE_FILL_FLAGS" in html
    assert "Coverage Aggressor Mode" in html
