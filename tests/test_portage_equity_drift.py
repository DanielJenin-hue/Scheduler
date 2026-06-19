"""Tests for Portage alternate/weekend equity drift indicators."""

from __future__ import annotations

from lab_scheduler.scheduling.portage_equity_drift import evaluate_portage_equity_drift
from lab_scheduler.scheduling.profiles import EmployeeProfile


def _vacant(role: str, contract: str, line: int, *, employee_id: str = "x") -> EmployeeProfile:
    return EmployeeProfile(
        id=employee_id,
        full_name=f"Vacant {role} {contract} - Line {line:02d}",
        fte=1.0,
        qualification_ids=set(),
        contract_line_type=contract,
    )


def test_core_ft_alt_drift_flags_low_and_high() -> None:
    employee = _vacant("MLT", "D/E", 1)
    ok = evaluate_portage_equity_drift(
        employee,
        320.0,
        alternate_shifts=8,
        total_shifts=40,
        weekend_shifts=8,
        weekend_target=8,
    )
    assert ok is not None
    assert ok.alt_status == "ok"
    assert ok.alt_target == 8
    assert ok.alt_target_density_pct == 20.0

    low = evaluate_portage_equity_drift(
        employee,
        320.0,
        alternate_shifts=5,
        total_shifts=40,
        weekend_shifts=8,
        weekend_target=8,
    )
    assert low is not None
    assert low.alt_status == "low"
    assert low.has_drift

    high = evaluate_portage_equity_drift(
        employee,
        320.0,
        alternate_shifts=11,
        total_shifts=40,
        weekend_shifts=8,
        weekend_target=8,
    )
    assert high is not None
    assert high.alt_status == "high"


def test_gap_fill_pt_alt_target_scales_with_fte() -> None:
    employee = _vacant("MLA", "D/E", 6)
    row = evaluate_portage_equity_drift(
        employee,
        224.0,
        alternate_shifts=6,
        total_shifts=28,
        weekend_shifts=6,
        weekend_target=6,
    )
    assert row is not None
    assert row.equity_role == "gap_fill_pt"
    assert row.alt_target == 6
    assert row.alt_target_density_pct == 21.4
    assert row.alt_status == "ok"

    high = evaluate_portage_equity_drift(
        employee,
        224.0,
        alternate_shifts=10,
        total_shifts=28,
        weekend_shifts=6,
        weekend_target=6,
    )
    assert high is not None
    assert high.alt_status == "high"

    under = evaluate_portage_equity_drift(
        employee,
        224.0,
        alternate_shifts=2,
        total_shifts=28,
        weekend_shifts=6,
        weekend_target=6,
    )
    assert under is not None
    assert under.alt_status == "low"


def test_weekend_drift_is_separate_from_alt() -> None:
    employee = _vacant("MLT", "D/E", 1)
    row = evaluate_portage_equity_drift(
        employee,
        320.0,
        alternate_shifts=8,
        total_shifts=40,
        weekend_shifts=5,
        weekend_target=8,
    )
    assert row is not None
    assert row.alt_status == "ok"
    assert row.weekend_status == "low"
    assert row.active_weekend_target == 4
