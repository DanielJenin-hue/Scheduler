from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from lab_scheduler.compliance.jurisdictions import get_jurisdiction
from lab_scheduler.scheduling.auto_generate import (
    AutoGenerateResult,
    PlannedAssignment,
    _build_generation_fairness_report,
    _fairness_rerun_warranted,
    _rollback_cpsat_assignments,
    _run_cpsat_vacant_fill_with_fairness_rerun,
)
from lab_scheduler.scheduling.fairness_thresholds import (
    CPSAT_FAIRNESS_RERUN_TIME_LIMIT_SECONDS,
    CPSAT_PRIMARY_TIME_LIMIT_SECONDS,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import QUAL_MLA, shift_templates as build_shift_templates
from lab_scheduler.validation.staff_fairness_report import (
    FairnessFlag,
    STATUS_NOT_RECOMMENDED,
    STATUS_READY,
    STATUS_REVIEW_REQUIRED,
)


def _mock_fill_result(
    *,
    evening_slack: int = 0,
    post_night_slack: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        evening_cluster_slack_total=evening_slack,
        post_night_recovery_slack_total=post_night_slack,
    )


def _mock_report(
    *,
    overall_status: str = STATUS_READY,
    flags: tuple = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        overall_status=overall_status,
        flags=flags,
        to_dict=lambda: {
            "overall_status": overall_status,
            "flags": [
                flag.to_dict() if hasattr(flag, "to_dict") else flag for flag in flags
            ],
        },
    )


def test_fairness_rerun_warranted_when_pass1_has_slack() -> None:
    fill = _mock_fill_result(evening_slack=2)
    report = _mock_report(overall_status=STATUS_REVIEW_REQUIRED)
    assert _fairness_rerun_warranted(fill, report) is True


def test_fairness_rerun_not_warranted_for_contract_hours_only() -> None:
    fill = _mock_fill_result()
    report = _mock_report(
        overall_status=STATUS_REVIEW_REQUIRED,
        flags=(
            FairnessFlag(
                employee_id="portage-mla-01",
                employee_name="Vacant MLA D/E - Line 01",
                code="CONTRACT_HOURS",
                severity="YELLOW",
                message="hours off target",
            ),
        ),
    )
    assert _fairness_rerun_warranted(fill, report) is False


def test_fairness_rerun_warranted_for_evening_cluster_flag() -> None:
    fill = _mock_fill_result()
    report = _mock_report(
        overall_status=STATUS_REVIEW_REQUIRED,
        flags=(
            FairnessFlag(
                employee_id="portage-mla-01",
                employee_name="Vacant MLA D/E - Line 01",
                code="EVENING_CLUSTER",
                severity="YELLOW",
                message="too many evenings",
            ),
        ),
    )
    assert _fairness_rerun_warranted(fill, report) is True


def test_build_generation_fairness_report_ready_on_light_schedule() -> None:
    employee = EmployeeProfile(
        "portage-mla-01",
        "Vacant MLA D/E - Line 01",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/E",
    )
    templates = build_shift_templates()
    assignments = [
        PlannedAssignment("portage-mla-01", "shift-morning", date(2026, 6, 1)),
    ]
    report = _build_generation_fairness_report(
        employees=[employee],
        assignments=assignments,
        shift_templates=templates,
        target_hours_map={"portage-mla-01": 8.0},
        qual_codes={"portage-mla-01": QUAL_MLA},
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 14),
    )
    assert report.overall_status == STATUS_READY


def test_rollback_cpsat_assignments_removes_only_cpsat_rows() -> None:
    from lab_scheduler.scheduling.auto_generate import _EmployeeState

    rules = get_jurisdiction("Manitoba")
    templates = build_shift_templates()
    employee = EmployeeProfile(
        "portage-mla-01",
        "Vacant MLA D/E - Line 01",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/E",
    )
    fixed = PlannedAssignment("portage-mla-01", "shift-morning", date(2026, 6, 1))
    cpsat_added = PlannedAssignment("portage-mla-01", "shift-evening", date(2026, 6, 2))
    result = AutoGenerateResult()
    result.assignments = [fixed, cpsat_added]
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=320.0),
    }
    _rollback_cpsat_assignments(
        result,
        states,
        [cpsat_added],
        employees=[employee],
        target_hours_map={employee.id: 320.0},
        shift_templates=templates,
        rules=rules,
    )
    assert len(result.assignments) == 1
    assert result.assignments[0].assignment_date == date(2026, 6, 1)


def test_fairness_rerun_invoked_when_addressable_flags_present() -> None:
    rules = get_jurisdiction("Manitoba")
    templates = build_shift_templates()
    employee = EmployeeProfile(
        "portage-mla-01",
        "Vacant MLA D/E - Line 01",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/E",
    )
    result = AutoGenerateResult()
    states = {employee.id: type("S", (), {"profile": employee, "target_hours": 320.0})()}
    pass1_fill = _mock_fill_result(evening_slack=1)
    pass2_fill = _mock_fill_result()
    not_ready = _mock_report(
        overall_status=STATUS_NOT_RECOMMENDED,
        flags=(
            FairnessFlag(
                employee_id=employee.id,
                employee_name=employee.full_name,
                code="EVENING_CLUSTER",
                severity="YELLOW",
                message="clustered evenings",
            ),
        ),
    )
    ready = _mock_report(overall_status=STATUS_READY)

    with patch(
        "lab_scheduler.scheduling.auto_generate._run_cpsat_vacant_fill_pass",
        side_effect=[
            (2, [PlannedAssignment("portage-mla-01", "shift-evening", date(2026, 6, 1))], pass1_fill),
            (2, [], pass2_fill),
        ],
    ) as mock_pass, patch(
        "lab_scheduler.scheduling.auto_generate._build_generation_fairness_report",
        side_effect=[not_ready, ready],
    ), patch(
        "lab_scheduler.scheduling.auto_generate._rollback_cpsat_assignments",
    ) as mock_rollback:
        added = _run_cpsat_vacant_fill_with_fairness_rerun(
            result=result,
            states=states,
            rules=rules,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 28),
            weeks_in_period=4,
            employees=[employee],
            shift_templates=templates,
            target_hours_map={employee.id: 320.0},
            availability_blocked={},
            qual_codes={employee.id: QUAL_MLA},
        )

    assert added == 2
    assert mock_pass.call_count == 2
    assert mock_pass.call_args_list[0].kwargs["time_limit_seconds"] == CPSAT_PRIMARY_TIME_LIMIT_SECONDS
    assert mock_pass.call_args_list[1].kwargs["fairness_weight_scale"] == 2.0
    assert mock_pass.call_args_list[1].kwargs["time_limit_seconds"] == CPSAT_FAIRNESS_RERUN_TIME_LIMIT_SECONDS
    mock_rollback.assert_called_once()
    assert result.fairness_rerun_count == 1


def test_fairness_rerun_skipped_when_only_non_addressable_flags() -> None:
    rules = get_jurisdiction("Manitoba")
    templates = build_shift_templates()
    employee = EmployeeProfile(
        "portage-mla-01",
        "Vacant MLA D/E - Line 01",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/E",
    )
    result = AutoGenerateResult()
    states = {employee.id: type("S", (), {"profile": employee, "target_hours": 320.0})()}
    pass1_fill = _mock_fill_result()
    review = _mock_report(
        overall_status=STATUS_REVIEW_REQUIRED,
        flags=(
            FairnessFlag(
                employee_id=employee.id,
                employee_name=employee.full_name,
                code="ALT_SHIFT_EQUITY",
                severity="YELLOW",
                message="alt shift variance",
            ),
        ),
    )

    with patch(
        "lab_scheduler.scheduling.auto_generate._run_cpsat_vacant_fill_pass",
        return_value=(1, [], pass1_fill),
    ) as mock_pass, patch(
        "lab_scheduler.scheduling.auto_generate._build_generation_fairness_report",
        return_value=review,
    ), patch(
        "lab_scheduler.scheduling.auto_generate._rollback_cpsat_assignments",
    ) as mock_rollback:
        added = _run_cpsat_vacant_fill_with_fairness_rerun(
            result=result,
            states=states,
            rules=rules,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 28),
            weeks_in_period=4,
            employees=[employee],
            shift_templates=templates,
            target_hours_map={employee.id: 320.0},
            availability_blocked={},
            qual_codes={employee.id: QUAL_MLA},
        )

    assert added == 1
    assert mock_pass.call_count == 1
    mock_rollback.assert_not_called()
    assert result.fairness_rerun_count == 0


def test_fairness_rerun_skipped_when_first_pass_ready() -> None:
    rules = get_jurisdiction("Manitoba")
    templates = build_shift_templates()
    employee = EmployeeProfile(
        "portage-mla-01",
        "Vacant MLA D/E - Line 01",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/E",
    )
    result = AutoGenerateResult()
    states = {employee.id: type("S", (), {"profile": employee, "target_hours": 320.0})()}
    pass1_fill = _mock_fill_result()
    ready = _mock_report(overall_status=STATUS_READY)

    with patch(
        "lab_scheduler.scheduling.auto_generate._run_cpsat_vacant_fill_pass",
        return_value=(1, [], pass1_fill),
    ) as mock_pass, patch(
        "lab_scheduler.scheduling.auto_generate._build_generation_fairness_report",
        return_value=ready,
    ), patch(
        "lab_scheduler.scheduling.auto_generate._rollback_cpsat_assignments",
    ) as mock_rollback:
        added = _run_cpsat_vacant_fill_with_fairness_rerun(
            result=result,
            states=states,
            rules=rules,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 28),
            weeks_in_period=4,
            employees=[employee],
            shift_templates=templates,
            target_hours_map={employee.id: 320.0},
            availability_blocked={},
            qual_codes={employee.id: QUAL_MLA},
        )

    assert added == 1
    assert mock_pass.call_count == 1
    mock_rollback.assert_not_called()
    assert result.fairness_rerun_count == 0
