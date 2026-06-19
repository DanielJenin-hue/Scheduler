"""Tests for Portage feasibility analysis."""

from __future__ import annotations

from lab_scheduler.scheduling.portage_equity_policy import CLINICAL_AND_HOURS_FIRST
from lab_scheduler.scheduling.portage_feasibility import (
    analyze_portage_feasibility,
    build_portage_rules_contract,
    portage_qual_contract_weekend_targets,
)
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.profiles import EmployeeProfile


def _vacant(role: str, contract: str, line: int, fte: float, hours: float) -> EmployeeProfile:
    qual = "qual-mlt" if role == "MLT" else "qual-mla"
    return EmployeeProfile(
        id=f"portage-{role.lower()}-{line:02d}",
        full_name=f"Vacant {role} {contract} - Line {line:02d} ({int(hours)}h)",
        fte=fte,
        qualification_ids={qual},
        contract_line_type=contract,
    )


def test_six_ft_mlt_de_weekend_catalog_exceeds_qual_cap() -> None:
    employees = [_vacant("MLT", "D/E", index, 1.0, 320.0) for index in range(1, 7)]
    targets = {employee.id: 320.0 for employee in employees}
    qual_codes = {employee.id: "MLT" for employee in employees}
    report = analyze_portage_feasibility(
        employees,
        targets,
        qual_codes=qual_codes,
        weekend_day_count=16,
        period_day_count=56,
    )
    hard = [item for item in report.conflicts if item.code == "FT_WEEKEND_CATALOG_VS_CAP"]
    assert hard
    assert hard[0].demand == 48.0
    assert hard[0].capacity == 32.0


def test_qual_contract_weekend_targets_scale_proportionally() -> None:
    hours = [320.0] * 6 + [224.0, 160.0, 64.0]
    targets = portage_qual_contract_weekend_targets(hours, qual_code="MLT", weekend_day_count=16)
    assert sum(targets) <= 32
    assert len(targets) == 9
    assert all(value % 2 == 0 for value in targets)


def test_vacant_line_parser_still_reads_catalog_hours() -> None:
    employee = _vacant("MLT", "D/E", 9, 0.2, 64.0)
    assert parse_vacant_portage_line(employee.full_name) == ("MLT", "D/E", 9)


def test_build_portage_rules_contract_includes_primary_goals() -> None:
    contract = build_portage_rules_contract(CLINICAL_AND_HOURS_FIRST)
    assert contract.policy_id == "clinical_and_hours_first"
    primary_codes = {entry.code for entry in contract.entries_by_tier("primary")}
    assert "PRIMARY_CLINICAL_2EN" in primary_codes
    assert "PRIMARY_CATALOG_HOURS" in primary_codes
    hard_codes = {entry.code for entry in contract.entries_by_tier("hard")}
    assert "WEEKEND_QUAL_CAP" in hard_codes
