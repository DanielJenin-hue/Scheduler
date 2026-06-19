"""Runtime optimizations: deferred fairness rerun and conditional gap closure."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy

from types import SimpleNamespace
from unittest.mock import patch

from lab_scheduler.scheduling.auto_generate import (
    _run_cpsat_vacant_fill_with_fairness_rerun,
    _should_run_cpsat_gap_closure,
)
from lab_scheduler.scheduling.fairness_thresholds import (
    CPSAT_INTERACTIVE_PORTAGE_PRIMARY_TIME_LIMIT_SECONDS,
)


def test_should_run_gap_closure_only_when_gaps_remain() -> None:
    assert _should_run_cpsat_gap_closure(
        clinical_seats_locked=True,
        rest_resolved=2,
        coverage_gap_count=5,
    )
    assert not _should_run_cpsat_gap_closure(
        clinical_seats_locked=True,
        rest_resolved=2,
        coverage_gap_count=0,
    )
    assert not _should_run_cpsat_gap_closure(
        clinical_seats_locked=False,
        rest_resolved=2,
        coverage_gap_count=5,
    )
    assert not _should_run_cpsat_gap_closure(
        clinical_seats_locked=True,
        rest_resolved=0,
        coverage_gap_count=5,
    )


def test_fairness_rerun_skipped_when_disabled() -> None:
    fill_result = SimpleNamespace(
        evening_cluster_slack_total=3,
        post_night_recovery_slack_total=0,
    )
    report = SimpleNamespace(
        overall_status="REVIEW_REQUIRED",
        flags=(),
        to_dict=lambda: {"overall_status": "REVIEW_REQUIRED", "flags": []},
    )

    with patch(
        "lab_scheduler.scheduling.auto_generate._run_cpsat_vacant_fill_pass",
        return_value=(4, [], fill_result),
    ) as mock_pass, patch(
        "lab_scheduler.scheduling.auto_generate._build_generation_fairness_report",
        return_value=report,
    ), patch(
        "lab_scheduler.scheduling.auto_generate._rollback_cpsat_assignments",
    ) as mock_rollback:
        added = _run_cpsat_vacant_fill_with_fairness_rerun(
            result=SimpleNamespace(
                assignments=[],
                staff_fairness_report={},
                fairness_rerun_count=0,
            ),
            states={},
            rules=SimpleNamespace(),
            period_start=__import__("datetime").date(2026, 6, 1),
            period_end=__import__("datetime").date(2026, 6, 28),
            weeks_in_period=4,
            employees=[],
            shift_templates={},
            target_hours_map={},
            availability_blocked=None,
            qual_codes={},
            enable_fairness_rerun=False,
        )

    assert added == 4
    assert mock_pass.call_count == 1
    assert mock_pass.call_args.kwargs["time_limit_seconds"] == (
        CPSAT_INTERACTIVE_PORTAGE_PRIMARY_TIME_LIMIT_SECONDS
    )
    mock_rollback.assert_not_called()
