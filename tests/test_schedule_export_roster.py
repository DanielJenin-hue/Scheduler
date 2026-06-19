"""Breakroom export and contract tracking roster tests."""

from __future__ import annotations

from datetime import date, timedelta

from lab_scheduler.scheduling.breakroom_print import (
    BreakroomPostingContext,
    compute_contract_tracking_row,
    generate_breakroom_print_html,
)
from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import (
    TWELVE_HOUR_PAID_HOURS,
)


def _dates(count: int, start: date = date(2026, 6, 1)) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(count)]


def test_generate_breakroom_print_html_contains_print_rules() -> None:
    dates = _dates(7)
    row = {
        "Employee": "Vacant MLT D/E - Line 01",
        "employee_id": "portage-mlt-01",
        "fte": 1.0,
        "contract_line_type": "D/E",
        **{day.isoformat(): "D" for day in dates},
    }
    html = generate_breakroom_print_html(
        facility_name="Northstar Medical Laboratory",
        period_name="Summer 2026",
        period_start=dates[0],
        period_end=dates[-1],
        week_count=8,
        employees=[{"id": "portage-mlt-01", "fte": 1.0, "contract_line_type": "D/E"}],
        dates=dates,
        schedule_rows=[row],
    )
    assert "@page" in html
    assert "breakroom-grid" in html
    assert "breakroom-screen-bar" in html
    assert "breakroom-enter-fs" in html


def test_compute_contract_tracking_row_ok_for_twelve_hour_archetype() -> None:
    dates = _dates(28)
    row = {day.isoformat(): "D" for day in dates}
    ok = compute_contract_tracking_row(
        fte=1.0,
        week_count=8,
        row=row,
        dates=dates,
        contract_line_type="D/E",
        schedule_archetype=ScheduleArchetype.TWELVE_HOUR.value,
    )
    assert ok.actual_hours == round(28 * TWELVE_HOUR_PAID_HOURS, 1)
    assert ok.actual_hours != 224.0
    assert abs(ok.variance_hours) < 10


def test_compute_contract_tracking_row_union_risk_for_standard_eight_hour() -> None:
    dates = _dates(27)
    row = {day.isoformat(): "D" for day in dates}
    deficit = compute_contract_tracking_row(
        fte=1.0,
        week_count=8,
        row=row,
        dates=dates,
        contract_line_type="D/E",
        schedule_archetype=ScheduleArchetype.STANDARD.value,
    )
    assert deficit.actual_hours == 216.0
    assert deficit.status_class == "contract-union-risk"


def test_compute_contract_tracking_row_respects_fte_target_override() -> None:
    dates = _dates(41)
    row = {day.isoformat(): "D" for day in dates[:41]}
    tracking = compute_contract_tracking_row(
        fte=1.0,
        week_count=8,
        row=row,
        dates=dates,
        contract_line_type="D/N",
        schedule_archetype=ScheduleArchetype.STANDARD.value,
        contract_target_hours=320.0,
    )
    assert tracking.target_hours == 320.0
    assert tracking.actual_hours == 328.0
    assert tracking.status_class == "contract-overtime-warn"


def test_generate_breakroom_print_html_uses_fte_contract_target_hours() -> None:
    dates = _dates(40)
    row = {
        "Employee": "Vacant MLA D/N - Line 01",
        "employee_id": "portage-mla-01",
        "fte": 1.0,
        "contract_line_type": "D/N",
        **{day.isoformat(): "D" for day in dates},
    }
    html = generate_breakroom_print_html(
        facility_name="Northstar Medical Laboratory",
        period_name="Summer 2026",
        period_start=dates[0],
        period_end=dates[-1],
        week_count=8,
        employees=[{"id": "portage-mla-01", "fte": 1.0, "contract_line_type": "D/N"}],
        dates=dates,
        schedule_rows=[row],
        contract_target_hours_by_employee={"portage-mla-01": 320.0},
    )
    assert "320h / 320h - OK" in html
    assert "328h / 328h" not in html


def test_compute_contract_tracking_row_orange_for_one_shift_over() -> None:
    dates = _dates(41)
    row = {day.isoformat(): "D" for day in dates}
    tracking = compute_contract_tracking_row(
        fte=1.0,
        week_count=8,
        row=row,
        dates=dates,
        contract_line_type="D/E",
        schedule_archetype=ScheduleArchetype.STANDARD.value,
        contract_target_hours=320.0,
    )
    assert tracking.actual_hours == 328.0
    assert tracking.status_class == "contract-overtime-warn"
    assert "Overtime Watch" in tracking.status_label


def test_compute_contract_tracking_row_red_for_multi_shift_over() -> None:
    dates = _dates(11)
    row = {day.isoformat(): "D" for day in dates}
    tracking = compute_contract_tracking_row(
        fte=0.25,
        week_count=8,
        row=row,
        dates=dates,
        contract_line_type="D/E",
        schedule_archetype=ScheduleArchetype.STANDARD.value,
        contract_target_hours=64.0,
    )
    assert tracking.actual_hours == 88.0
    assert tracking.status_class == "contract-overtime-risk"
    assert "Overtime Risk" in tracking.status_label


def _minimal_breakroom_html(
    *,
    posting_context: BreakroomPostingContext | None = None,
    compliance_verified_on: date | None = None,
) -> str:
    dates = _dates(40)
    row = {
        "Employee": "Vacant MLA D/N - Line 01",
        "employee_id": "portage-mla-01",
        "fte": 1.0,
        "contract_line_type": "D/N",
        **{day.isoformat(): "D" for day in dates},
    }
    return generate_breakroom_print_html(
        facility_name="Northstar Medical Laboratory",
        period_name="Summer 2026 Master Rotation",
        period_start=dates[0],
        period_end=dates[-1],
        week_count=8,
        employees=[{"id": "portage-mla-01", "fte": 1.0, "contract_line_type": "D/N"}],
        dates=dates,
        schedule_rows=[row],
        contract_target_hours_by_employee={"portage-mla-01": 320.0},
        compliance_verified_on=compliance_verified_on or date(2026, 6, 5),
        posting_context=posting_context,
    )


def test_generate_breakroom_print_html_draft_preview_blocks_verified_badge() -> None:
    posting_context = BreakroomPostingContext(
        using_autopilot_preview=True,
        persist_ok=False,
        is_premium=True,
        required_filled=336,
        required_total=336,
        violation_codes={"CONTRACT_HOURS": 2, "WEEKEND_SHIFT_DRIFT": 7},
        saved_filled=224,
        saved_total=336,
    )
    html = _minimal_breakroom_html(posting_context=posting_context)

    assert "DRAFT PREVIEW — NOT SAVED TO DATABASE" in html
    assert "Compliance Verified" not in html
    assert "Posting checklist" in html
    assert "CONTRACT_HOURS×2" in html
    assert "336/336" in html
    assert "224/336 slots" in html
    assert "breakroom-draft-badge" in html
    assert "breakroom-draft-header" in html


def test_generate_breakroom_print_html_saved_state_keeps_verified_badge() -> None:
    posting_context = BreakroomPostingContext(
        using_autopilot_preview=False,
        persist_ok=True,
        is_premium=True,
        saved_filled=336,
        saved_total=336,
    )
    html = _minimal_breakroom_html(
        posting_context=posting_context,
        compliance_verified_on=date(2026, 6, 5),
    )

    assert "Compliance Verified: Manitoba Labor Standards [2026-06-05]" in html
    assert "DRAFT PREVIEW — NOT SAVED TO DATABASE" not in html
    assert "class='breakroom-draft-badge'" not in html


def test_generate_breakroom_print_html_trial_meta_suffix_when_not_premium() -> None:
    posting_context = BreakroomPostingContext(is_premium=False)
    html = _minimal_breakroom_html(posting_context=posting_context)

    assert "(8-week trial preview)" in html
    assert "Compliance Verified" in html


def test_generate_breakroom_print_html_without_posting_context_unchanged() -> None:
    html = _minimal_breakroom_html()

    assert "Compliance Verified" in html
    assert "DRAFT PREVIEW" not in html
    assert "Posting checklist" not in html
    assert "(8-week trial preview)" not in html


def test_export_worker_breakroom_html_uses_twelve_hour_paid_coefficient(tmp_path) -> None:
    from lab_scheduler.workers.export_worker import ExportWorkerInput, run_export_worker

    dates = _dates(27)
    assignments = [
        {
            "employee_id": "portage-mlt-01",
            "shift_template_id": "shift-morning",
            "assignment_date": day,
        }
        for day in dates
    ]
    result = run_export_worker(
        tmp_path,
        ExportWorkerInput(
            assignments=assignments,
            period_start=dates[0],
            period_end=dates[-1],
            employees=[{"id": "portage-mlt-01", "fte": 1.0, "contract_line_type": "D/E", "full_name": "Vacant MLT D/E - Line 01"}],
            shift_templates={
                "shift-morning": {
                    "id": "shift-morning",
                    "code": "MORNING",
                    "short": "D",
                    "name": "Morning",
                }
            },
            week_count=8,
            schedule_archetype=ScheduleArchetype.TWELVE_HOUR.value,
        ),
    )
    assert result.breakroom_html_path is not None
    html = result.breakroom_html_path.read_text(encoding="utf-8")
    expected_hours = round(27 * TWELVE_HOUR_PAID_HOURS, 1)
    assert f"{expected_hours:g}h actual" in html
    assert "216h actual" not in html
