from __future__ import annotations

from dataclasses import dataclass

from lab_scheduler.compliance.jurisdictions import get_jurisdiction
from lab_scheduler.scheduling.contract_payroll import (
    aggregate_payroll_contract_hours,
    resolve_employee_fte,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


@dataclass
class ReloadedEmployeeProfile:
    """Simulates Streamlit hot-reload breaking isinstance(EmployeeProfile)."""

    id: str
    fte: float


def test_resolve_employee_fte_from_profile() -> None:
    profile = EmployeeProfile(
        id="emp-1",
        full_name="Test User",
        fte=1.0,
        qualification_ids=set(),
    )
    assert resolve_employee_fte(profile) == 1.0


def test_resolve_employee_fte_from_mapping() -> None:
    assert resolve_employee_fte({"id": "emp-1", "fte": 0.8}) == 0.8


def test_resolve_employee_fte_from_reloaded_profile_class() -> None:
    reloaded = ReloadedEmployeeProfile(id="emp-1", fte=0.75)
    assert resolve_employee_fte(reloaded) == 0.75


def test_aggregate_payroll_contract_hours_accepts_reloaded_profiles() -> None:
    rules = get_jurisdiction("Manitoba")
    employees = [
        ReloadedEmployeeProfile(id="a", fte=1.0),
        {"id": "b", "fte": 0.5},
    ]
    total = aggregate_payroll_contract_hours(
        employees,
        rules=rules,
        weeks_in_period=2,
    )
    assert total == 120.0
