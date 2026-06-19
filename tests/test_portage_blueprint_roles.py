"""Tests for Portage blueprint equity roles and pool budget diagnostics."""

from __future__ import annotations

from lab_scheduler.scheduling.portage_equity_targets import (
    portage_alt_shift_target,
    portage_alt_shift_target_for_employee,
    portage_weekend_shift_target,
)
from lab_scheduler.scheduling.portage_feasibility import build_portage_pool_budget_rows
from lab_scheduler.simulation.portage_blueprint import (
    build_portage_blueprint_roster,
    portage_equity_role_for_employee,
    portage_line_spec_for_vacant_name,
)


def test_blueprint_equity_roles_for_gap_fill_and_light_lines() -> None:
    mla_l6 = portage_line_spec_for_vacant_name("Vacant MLA D/E - Line 06")
    assert mla_l6 is not None
    assert mla_l6.equity_role == "gap_fill_pt"

    mlt_l9 = portage_line_spec_for_vacant_name("Vacant MLT D/E - Line 09")
    assert mlt_l9 is not None
    assert mlt_l9.equity_role == "light_pt"

    mlt_l1 = portage_line_spec_for_vacant_name("Vacant MLT D/N - Line 01")
    assert mlt_l1 is not None
    assert mlt_l1.equity_role == "core_ft"


def test_gap_fill_pt_de_alt_target_is_hours_weighted() -> None:
    roster = build_portage_blueprint_roster()
    mla_l6 = next(e for e in roster if "MLA D/E - Line 06" in e.full_name)
    assert portage_equity_role_for_employee(mla_l6) == "gap_fill_pt"
    # 224h × 0.025 evenings/hour → 5.6, round up for part-time → 6
    assert portage_alt_shift_target_for_employee(mla_l6, 224.0) == 6
    assert portage_alt_shift_target(224.0, equity_role="gap_fill_pt") == 10


def test_light_pt_weekend_target_is_reduced() -> None:
    core = portage_weekend_shift_target(128.0, equity_role="core_ft")
    light = portage_weekend_shift_target(128.0, equity_role="light_pt")
    assert light < core
    assert light % 2 == 0


def test_pool_budget_rows_include_headroom_for_each_qual_contract() -> None:
    roster = build_portage_blueprint_roster()
    catalog_targets = {employee.id: 320.0 for employee in roster}
    qual_codes = {
        employee.id: "MLT" if "MLT" in employee.full_name else "MLA"
        for employee in roster
    }
    rows = build_portage_pool_budget_rows(
        roster,
        catalog_targets,
        qual_codes=qual_codes,
        weekend_day_count=16,
        period_day_count=112,
    )
    pools = {row["Pool"] for row in rows}
    assert "MLT D/E" in pools
    assert "MLA D/E" in pools
    for row in rows:
        assert "Weekend headroom" in row
        assert "E alt headroom" in row or "N alt headroom" in row
