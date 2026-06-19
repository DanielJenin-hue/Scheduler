"""Tests for preference-driven schedule fill."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.policy.frame_bridge import normalize_grid_shift_token
from lab_scheduler.scheduling.alternate_shift_distributor import (
    enumerate_staggered_weekend_blocks,
)
from lab_scheduler.scheduling.employee_scheduling_profile import (
    EmployeeSchedulingProfile,
    build_portage_scheduling_profiles,
    compute_tier_targets,
    derive_weekend_band,
)
from lab_scheduler.scheduling.preference_fill import (
    build_pool_stagger_assignments,
    fill_schedule_by_preferences,
)
from lab_scheduler.scheduling.preference_policy import (
    PORTAGE_DEFAULT_POLICY,
    FillMode,
    SchedulingPreferencePolicy,
    SlotTier,
    load_tenant_preference_policy,
    policy_from_json,
    policy_to_json,
    resolve_slot_tier,
    save_tenant_preference_policy,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.weekend_placement_rules import mirror_weekend_partner
from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours
from tests.test_distribute_alternate_shifts import (
    _assigned_weekend_dates,
    _build_ft_pool_frame,
    _db_templates,
    _employees_from_specs,
    _period_dates,
    _templates,
)


def _empty_frame(dates: list[date], specs: list[tuple[str, str, str]]) -> pd.DataFrame:
    rows = []
    for employee_id, name, contract in specs:
        row: dict[str, object] = {
            "employee_id": employee_id,
            "Employee": name,
            "contract_line_type": contract,
        }
        for day in dates:
            row[day.isoformat()] = "—"
        rows.append(row)
    return pd.DataFrame(rows)


def _fill_specs(
    dates: list[date],
    specs: list[tuple[str, str, str]],
    *,
    targets: dict[str, float],
    mode: FillMode = FillMode.FULL,
    policy: SchedulingPreferencePolicy = PORTAGE_DEFAULT_POLICY,
):
    frame = _empty_frame(dates, specs)
    employees = _employees_from_specs(specs)
    profiles_list = [
        EmployeeProfile(
            id=employee_id,
            full_name=name,
            fte=1.0,
            qualification_ids={"qual-mlt"} if "MLT" in name else {"qual-mla"},
            contract_line_type=contract,
        )
        for employee_id, name, contract in specs
    ]
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    profiles = build_portage_scheduling_profiles(
        frame,
        profiles_list,
        employee_target_hours=targets,
        qual_codes=qual_codes,
    )
    emp_quals = {
        employee_id: {"qual-mlt"} if "MLT" in name else {"qual-mla"}
        for employee_id, name, _contract in specs
    }
    return fill_schedule_by_preferences(
        frame,
        employees=employees,
        dates=dates,
        period_start=dates[0],
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals=emp_quals,
        qual_codes=qual_codes,
        employee_target_hours=targets,
        policy=policy,
        profiles=profiles,
        mode=mode,
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE tenants (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO tenants (id) VALUES ('tenant-a')")
    connection.commit()
    return connection


def test_resolve_slot_tier_weekend_and_weekday() -> None:
    saturday = date(2026, 6, 6)
    monday = date(2026, 6, 8)
    assert resolve_slot_tier(saturday, "E", "D/E") == SlotTier.WEEKEND_ALT
    assert resolve_slot_tier(saturday, "D", "D/E") == SlotTier.WEEKEND_DAY
    assert resolve_slot_tier(monday, "E", "D/E") == SlotTier.WEEKDAY_ALT
    assert resolve_slot_tier(monday, "D", "D/E") == SlotTier.WEEKDAY_DAY
    assert resolve_slot_tier(saturday, "N", "D/N") == SlotTier.WEEKEND_ALT


def test_derive_weekend_band_portage_lines() -> None:
    de_l1 = EmployeeProfile("a", "Vacant MLT D/E - Line 01", 1.0, {"q"}, contract_line_type="D/E")
    de_l6 = EmployeeProfile("b", "Vacant MLT D/E - Line 06", 1.0, {"q"}, contract_line_type="D/E")
    de_l9 = EmployeeProfile("c", "Vacant MLT D/E - Line 09", 0.2, {"q"}, contract_line_type="D/E")
    dn = EmployeeProfile("d", "Vacant MLT D/N - Line 01", 1.0, {"q"}, contract_line_type="D/N")
    assert derive_weekend_band(de_l1) == "E"
    assert derive_weekend_band(de_l6) == "D"
    assert derive_weekend_band(de_l9) == "E"
    assert derive_weekend_band(dn) == "N"


def test_compute_tier_targets_de_ft_and_dn_ft() -> None:
    de_l1 = EmployeeProfile("a", "Vacant MLT D/E - Line 01", 1.0, {"q"}, contract_line_type="D/E")
    de_l6 = EmployeeProfile("b", "Vacant MLT D/E - Line 06", 1.0, {"q"}, contract_line_type="D/E")
    dn = EmployeeProfile("c", "Vacant MLT D/N - Line 01", 1.0, {"q"}, contract_line_type="D/N")

    l1_targets = compute_tier_targets(
        catalog_hours=320.0,
        contract_line_type="D/E",
        weekend_band="E",
        equity_role="core_ft",
        employee=de_l1,
    )
    assert l1_targets[SlotTier.WEEKEND_ALT] == 8
    assert l1_targets[SlotTier.WEEKEND_DAY] == 0

    l6_targets = compute_tier_targets(
        catalog_hours=320.0,
        contract_line_type="D/E",
        weekend_band="D",
        equity_role="core_ft",
        employee=de_l6,
    )
    assert l6_targets[SlotTier.WEEKEND_ALT] == 0
    assert l6_targets[SlotTier.WEEKEND_DAY] == 8

    dn_targets = compute_tier_targets(
        catalog_hours=320.0,
        contract_line_type="D/N",
        weekend_band="N",
        equity_role="core_ft",
        employee=dn,
    )
    assert dn_targets[SlotTier.WEEKEND_ALT] == 4
    assert dn_targets[SlotTier.WEEKEND_DAY] == 0
    assert dn_targets[SlotTier.WEEKDAY_ALT] == 10


def test_tenant_policy_round_trip(conn) -> None:
    from lab_scheduler.tenant.configuration import ensure_tenant_configuration_schema

    ensure_tenant_configuration_schema(conn)
    custom = SchedulingPreferencePolicy(
        tier_order=(
            SlotTier.WEEKDAY_DAY,
            SlotTier.WEEKEND_ALT,
            SlotTier.WEEKEND_DAY,
            SlotTier.WEEKDAY_ALT,
        )
    )
    save_tenant_preference_policy(conn, tenant_id="tenant-a", policy=custom)
    loaded = load_tenant_preference_policy(conn, "tenant-a")
    assert loaded.tier_order[0] == SlotTier.WEEKDAY_DAY
    assert policy_from_json(policy_to_json(custom)).tier_order == custom.tier_order


def test_fill_empty_only_preserves_existing_weekday_d() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("line-01", "Vacant MLT D/E - Line 01", "D/E"),
    ]
    frame = _empty_frame(dates, specs)
    monday = next(day for day in dates if day.weekday() == 0)
    frame.loc[frame["employee_id"] == "line-01", monday.isoformat()] = "D"
    employees = _employees_from_specs(specs)
    profiles_list = [
        EmployeeProfile(
            id="line-01",
            full_name="Vacant MLT D/E - Line 01",
            fte=1.0,
            qualification_ids={"qual-mlt"},
            contract_line_type="D/E",
        )
    ]
    targets = {"line-01": 320.0}
    profiles = build_portage_scheduling_profiles(
        frame,
        profiles_list,
        employee_target_hours=targets,
        qual_codes={"qual-mlt": "MLT"},
    )
    updated, result = fill_schedule_by_preferences(
        frame,
        employees=employees,
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={"line-01": {"qual-mlt"}},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours=targets,
        policy=PORTAGE_DEFAULT_POLICY,
        profiles=profiles,
        mode=FillMode.FULL,
    )
    row = updated[updated["employee_id"] == "line-01"].iloc[0]
    assert normalize_grid_shift_token(row.get(monday.isoformat(), "")) == "D"
    assert result.cells_changed >= 0


def test_weekend_stagger_slice_places_de_line_six_weekend_d() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("mlt-de-05", "Vacant MLT D/E - Line 05", "D/E"),
        ("mlt-de-06", "Vacant MLT D/E - Line 06", "D/E"),
    ]
    targets = {"mlt-de-05": 320.0, "mlt-de-06": 320.0}
    updated, result = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.WEEKEND_STAGGER_SLICE,
    )
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    line6_weekends = set(_assigned_weekend_dates(updated, "mlt-de-06", dates))
    assert line6_weekends == set(blocks[1])
    assert all(
        normalize_grid_shift_token(
            updated[updated["employee_id"] == "mlt-de-06"].iloc[0].get(day.isoformat(), "")
        )
        == "D"
        for day in line6_weekends
    )
    assert result.stagger_applied


def test_full_fill_idempotent_second_run() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("mlt-de-01", "Vacant MLT D/E - Line 01", "D/E"),
        ("mlt-de-02", "Vacant MLT D/E - Line 02", "D/E"),
    ]
    targets = {"mlt-de-01": 320.0, "mlt-de-02": 320.0}
    frame = _empty_frame(dates, specs)
    employees = _employees_from_specs(specs)
    profiles_list = [
        EmployeeProfile(
            id=employee_id,
            full_name=name,
            fte=1.0,
            qualification_ids={"qual-mlt"},
            contract_line_type=contract,
        )
        for employee_id, name, contract in specs
    ]
    profiles = build_portage_scheduling_profiles(
        frame,
        profiles_list,
        employee_target_hours=targets,
        qual_codes={"qual-mlt": "MLT"},
    )
    common = {
        "employees": employees,
        "dates": dates,
        "period_start": start,
        "period_end": dates[-1],
        "weeks_in_period": 8,
        "rules": MANITOBA,
        "templates": _db_templates(),
        "shift_templates": _templates(),
        "locked_cells": set(),
        "blocked_map": {},
        "emp_quals": {"mlt-de-01": {"qual-mlt"}, "mlt-de-02": {"qual-mlt"}},
        "qual_codes": {"qual-mlt": "MLT"},
        "employee_target_hours": targets,
        "policy": PORTAGE_DEFAULT_POLICY,
        "profiles": profiles,
        "mode": FillMode.FULL,
    }
    updated, first = fill_schedule_by_preferences(frame, **common)
    assert first.cells_changed > 0
    _updated2, second = fill_schedule_by_preferences(
        updated, **{**common, "profiles": None}
    )
    assert second.cells_changed == 0


def test_pool_index_stagger_scales_with_four_lines() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        (f"mlt-de-0{i}", f"Vacant MLT D/E - Line 0{i}", "D/E")
        for i in range(1, 5)
    ]
    frame = _empty_frame(dates, specs)
    profiles_list = [
        EmployeeProfile(
            id=employee_id,
            full_name=name,
            fte=1.0,
            qualification_ids={"qual-mlt"},
            contract_line_type="D/E",
        )
        for employee_id, name, _contract in specs
    ]
    targets = {employee_id: 320.0 for employee_id, _name, _contract in specs}
    profiles = build_portage_scheduling_profiles(
        frame,
        profiles_list,
        employee_target_hours=targets,
        qual_codes={"qual-mlt": "MLT"},
    )
    assignments = build_pool_stagger_assignments(
        profiles,
        period_start=start,
        period_end=dates[-1],
        frame_order=[employee_id for employee_id, _name, _contract in specs],
        employees_by_id={profile.id: profile for profile in profiles_list},
        employee_target_hours=targets,
    )
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    for index, (employee_id, _name, _contract) in enumerate(specs):
        assert assignments[employee_id] == blocks[index]


def test_portage_roster_full_fill_assigns_weekend_tokens() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    de_lines = [
        employee
        for employee in roster
        if (employee.contract_line_type or "").upper() == "D/E"
        and ("Line 05" in employee.full_name or "Line 06" in employee.full_name)
    ]
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in de_lines
    ]
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    updated, result = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.WEEKEND_STAGGER_SLICE,
    )
    assert result.cells_changed > 0
    for employee_id, name, _contract in specs:
        if "Line 06" in name:
            weekends = _assigned_weekend_dates(updated, employee_id, dates)
            assert weekends
            row = updated[updated["employee_id"] == employee_id].iloc[0]
            assert all(
                normalize_grid_shift_token(row.get(day.isoformat(), "")) == "D"
                for day in weekends
            )


def test_full_fill_respects_six_day_consecutive_work_cap() -> None:
    """No employee may work more than six calendar days in a row."""
    from lab_scheduler.scheduling.streak_validator import (
        validate_work_streaks_from_schedule_rows,
    )
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in roster
    ]
    updated, _result = _fill_specs(dates, specs, targets=targets, mode=FillMode.FULL)
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    rows = []
    employees = [
        {
            "id": employee.id,
            "full_name": employee.full_name,
            "contract_line_type": employee.contract_line_type,
        }
        for employee in roster
    ]
    for employee in roster:
        row_idx = row_lookup[employee.id]
        row = {"employee_id": employee.id, "Employee": employee.full_name}
        for day in dates:
            row[day.isoformat()] = get_grid_token(updated, row_idx, day) or "—"
        rows.append(row)
    violations = validate_work_streaks_from_schedule_rows(
        rows,
        employees=employees,
        dates=dates,
    )
    assert not violations, violations[0].message if violations else ""


def test_full_fill_preserves_ft_de_master_catalog() -> None:
    """Regression: tier fill must not scatter extra D shifts onto catalog-stamped FT D/E lines."""
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
    from lab_scheduler.scheduling.preference_fill import _would_violate_consecutive_work_cap
    from lab_scheduler.scheduling.portage_template import (
        _catalog_shift_token_for_date,
        portage_master_line_spec,
    )
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    de_ft = [
        employee
        for employee in roster
        if (employee.contract_line_type or "").upper() == "D/E"
        and employee.fte >= 1.0
        and "MLT" in employee.full_name
    ]
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in de_ft
    ]
    updated, result = _fill_specs(dates, specs, targets=targets, mode=FillMode.FULL)
    assert result.tier_counts.get("weekday_day", 0) == 0
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    for employee in de_ft:
        spec = portage_master_line_spec(employee)
        row_idx = row_lookup[employee.id]
        for day in dates:
            expected = _catalog_shift_token_for_date(spec, day, start) or "-"
            actual = get_grid_token(updated, row_idx, day) or "-"
            if expected == "D" and actual not in {"D", "-"}:
                raise AssertionError(
                    f"{employee.full_name} {day.isoformat()}: placed {actual} != catalog {expected}"
                )
            if expected != "-" and actual == "-":
                if expected in {"E", "N"} and day.weekday() < 5:
                    from lab_scheduler.scheduling.weekend_placement_rules import (
                        daily_band_qual_count,
                    )

                    qual = "MLT" if "MLT" in employee.full_name else "MLA"
                    counts = daily_band_qual_count(
                        updated,
                        row_lookup,
                        {employee.id: employee for employee in de_ft},
                        {"qual-mlt": "MLT", "qual-mla": "MLA"},
                        day,
                        expected,
                    )
                    if counts.get(qual, 0) >= 1:
                        continue
                assert _would_violate_consecutive_work_cap(
                    updated,
                    row_idx,
                    day,
                    dates=dates,
                    rules=MANITOBA,
                ), (
                    f"{employee.full_name} {day.isoformat()}: catalog {expected} skipped "
                    "without 6-day cap reason"
                )


def test_full_fill_stamps_dn_master_rotation_one_night_per_qual_per_day() -> None:
    """D/N catalog honors 6-day cap and weekend night pairing on every calendar day."""
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
    from lab_scheduler.scheduling.preference_fill import (
        _DN_SACRIFICE_WEEKDAY,
        _count_band_by_qual_on_day,
    )
    from lab_scheduler.scheduling.streak_validator import (
        validate_work_streaks_from_schedule_rows,
    )
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in roster
    ]
    employees = [
        {
            "id": employee.id,
            "full_name": employee.full_name,
            "contract_line_type": employee.contract_line_type,
        }
        for employee in roster
    ]
    updated, result = _fill_specs(dates, specs, targets=targets, mode=FillMode.FULL)
    assert result.tier_counts.get("master_catalog", 0) > 0
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    employees_by_id = {employee.id: employee for employee in roster}
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    rows = []
    for employee in roster:
        row_idx = row_lookup[employee.id]
        row = {"employee_id": employee.id, "Employee": employee.full_name}
        for day in dates:
            row[day.isoformat()] = get_grid_token(updated, row_idx, day) or "—"
        rows.append(row)
    assert not validate_work_streaks_from_schedule_rows(
        rows,
        employees=employees,
        dates=dates,
    )
    for day in dates:
        for qual in ("MLT", "MLA"):
            counts = _count_band_by_qual_on_day(
                updated,
                row_lookup,
                employees_by_id,
                qual_codes,
                day,
                "N",
            )
            if day.weekday() == _DN_SACRIFICE_WEEKDAY and counts[qual] == 0:
                continue
            assert counts[qual] >= 1, f"{qual} missing night on {day.isoformat()}"
    for employee in roster:
        if (employee.contract_line_type or "").upper() != "D/N":
            continue
        row_idx = row_lookup[employee.id]
        for day in dates:
            if day.weekday() != 5:
                continue
            sunday = day + timedelta(days=1)
            if sunday > max(dates):
                continue
            sat = get_grid_token(updated, row_idx, day)
            sun = get_grid_token(updated, row_idx, sunday)
            assert (sat == "N") == (sun == "N"), (
                f"{employee.full_name}: weekend night pair broken on {day.isoformat()}"
            )


def test_pt_de_lines_respect_payroll_cap_on_full_fill() -> None:
    """Regression: PT MLT L7-9 and MLA L6-8 must not exceed catalog shift count."""
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
    from lab_scheduler.scheduling.portage_equity_targets import portage_contract_shift_count
    from lab_scheduler.scheduling.preference_fill import _count_work_shifts

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    pt_lines = [
        employee
        for employee in roster
        if "D/E" in (employee.contract_line_type or "")
        and any(
            token in employee.full_name
            for token in (
                "MLT D/E - Line 07",
                "MLT D/E - Line 08",
                "MLT D/E - Line 09",
                "MLA D/E - Line 06",
                "MLA D/E - Line 07",
                "MLA D/E - Line 08",
            )
        )
    ]
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in pt_lines
    ]
    updated, _result = _fill_specs(dates, specs, targets=targets, mode=FillMode.FULL)
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    for employee in pt_lines:
        row_idx = row_lookup[employee.id]
        cap = portage_contract_shift_count(targets[employee.id])
        assigned = _count_work_shifts(updated, row_idx, dates)
        assert assigned <= cap, f"{employee.full_name}: {assigned} shifts > cap {cap}"


def test_alternate_shifts_mode_skips_weekday_day_filler() -> None:
    """Alternate-shifts fill must not run the weekday D filler tier."""
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("line-01", "Vacant MLT D/E - Line 01", "D/E"),
    ]
    targets = {"line-01": 320.0}
    _updated, result = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.ALTERNATE_SHIFTS,
    )
    assert result.tier_counts.get("weekday_day", 0) == 0


def test_alternate_shifts_mode_includes_weekend_stagger_and_weekday_alt() -> None:
    """Alternate-shifts fill places one 7-day E block per FT D/E line (8 E total)."""
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("mlt-de-05", "Vacant MLT D/E - Line 05", "D/E"),
        ("mlt-de-06", "Vacant MLT D/E - Line 06", "D/E"),
    ]
    targets = {"mlt-de-05": 320.0, "mlt-de-06": 320.0}
    updated, result = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.ALTERNATE_SHIFTS,
    )
    assert result.stagger_applied
    assert result.tier_counts.get("seven_day_evening_blocks", 0) > 0
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    for employee_id in ("mlt-de-05", "mlt-de-06"):
        evenings = sum(
            1
            for day in dates
            if get_grid_token(updated, row_lookup[employee_id], day) == "E"
        )
        assert evenings == 8


def test_alternate_shifts_mode_stamps_catalog_and_covers_clinical_floor() -> None:
    """Structured alternate fill reaches clinical floor during active cohort weeks."""
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
    from lab_scheduler.scheduling.preference_fill import _count_band_by_qual_on_day

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in roster
    ]
    updated, result = _fill_specs(dates, specs, targets=targets, mode=FillMode.ALTERNATE_SHIFTS)
    assert result.tier_counts.get("seven_day_evening_blocks", 0) > 0
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    employees_by_id = {employee.id: employee for employee in roster}
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    for day in dates:
        if day < date(2026, 6, 3) or day > date(2026, 6, 8) or day.weekday() >= 5:
            continue
        counts = _count_band_by_qual_on_day(
            updated,
            row_lookup,
            employees_by_id,
            qual_codes,
            day,
            "E",
        )
        for qual in ("MLT", "MLA"):
            assert counts[qual] >= 1, (
                f"{qual} E under-filled on {day.isoformat()}: {counts[qual]}/1"
            )
        night_counts = _count_band_by_qual_on_day(
            updated,
            row_lookup,
            employees_by_id,
            qual_codes,
            day,
            "N",
        )
        for qual in ("MLT", "MLA"):
            assert night_counts[qual] >= 1, (
                f"{qual} N missing on {day.isoformat()}: {night_counts[qual]}"
            )
        assert sum(night_counts.values()) >= 2, (
            f"pool N under-filled on {day.isoformat()}: {dict(night_counts)}"
        )


def test_alternate_shifts_populates_dn_weekday_days_and_targets_dn_nights() -> None:
    """D/N lines get weekday Days on day-band weeks; FT D/E uses 7-day E blocks; FT D/N gets 14 nights."""
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
    from lab_scheduler.scheduling.portage_equity_targets import (
        PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
        portage_contract_shift_count,
        portage_is_fulltime_catalog_hours,
    )
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in roster
    ]
    updated, result = _fill_specs(
        dates, specs, targets=targets, mode=FillMode.ALTERNATE_SHIFTS
    )
    assert result.tier_counts.get("dn_weekday_day_balanced", 0) > 0
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    expected_weekday_d_by_id = {
        employee.id: portage_contract_shift_count(targets[employee.id])
        - PORTAGE_DN_FT_NIGHT_SHIFT_TARGET
        for employee in roster
        if (employee.contract_line_type or "").upper() == "D/N"
        and portage_is_fulltime_catalog_hours(targets[employee.id])
    }
    for employee in roster:
        if (employee.contract_line_type or "").upper() != "D/N":
            continue
        if not portage_is_fulltime_catalog_hours(targets[employee.id]):
            continue
        weekday_d = sum(
            1
            for day in dates
            if day.weekday() < 5
            and get_grid_token(updated, row_lookup[employee.id], day) == "D"
        )
        expected_weekday_d = expected_weekday_d_by_id[employee.id]
        assert weekday_d == expected_weekday_d, (
            f"{employee.full_name}: {weekday_d} weekday D != {expected_weekday_d}"
        )
    de_ft_evenings = [
        sum(
            1
            for day in dates
            if get_grid_token(updated, row_lookup[employee.id], day) == "E"
        )
        for employee in roster
        if (employee.contract_line_type or "").upper() == "D/E"
        and portage_is_fulltime_catalog_hours(targets[employee.id])
    ]
    assert de_ft_evenings
    assert all(count == 8 for count in de_ft_evenings)
    for employee in roster:
        if (employee.contract_line_type or "").upper() != "D/N":
            continue
        if employee.fte < 1.0:
            continue
        nights = sum(
            1
            for day in dates
            if get_grid_token(updated, row_lookup[employee.id], day) == "N"
        )
        assert nights == PORTAGE_DN_FT_NIGHT_SHIFT_TARGET, (
            f"{employee.full_name}: {nights} nights != "
            f"{PORTAGE_DN_FT_NIGHT_SHIFT_TARGET}"
        )


def test_alternate_shifts_evening_equity_among_de_ft_peers() -> None:
    """FT D/E peers with the same FTE should share total evening counts."""
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_alt_shift_target_for_employee,
        portage_is_fulltime_catalog_hours,
    )
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in roster
    ]
    updated, _result = _fill_specs(
        dates, specs, targets=targets, mode=FillMode.ALTERNATE_SHIFTS
    )
    row_lookup = schedule_frame_row_index_by_employee_id(updated)

    for qual_code, qual_prefix in (("MLT", "Vacant MLT D/E"), ("MLA", "Vacant MLA D/E")):
        ft_counts: list[int] = []
        for employee in roster:
            if not employee.full_name.startswith(qual_prefix):
                continue
            if (employee.contract_line_type or "").upper() != "D/E":
                continue
            catalog_hours = float(targets.get(employee.id, 0.0))
            if not portage_is_fulltime_catalog_hours(catalog_hours):
                continue
            target = portage_alt_shift_target_for_employee(employee, catalog_hours)
            assigned = sum(
                1
                for day in dates
                if get_grid_token(updated, row_lookup[employee.id], day) == "E"
            )
            assert assigned == target
            ft_counts.append(assigned)
        if len(ft_counts) >= 2:
            assert max(ft_counts) - min(ft_counts) == 0, (
                f"{qual_code} FT D/E {qual_prefix} evening spread {ft_counts}"
            )


def test_alternate_shifts_respects_hours_weighted_evening_targets() -> None:
    """No D/E line should exceed its hours-weighted evening target after fill."""
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_hours_weighted_de_alt_target,
    )
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in roster
        if (employee.contract_line_type or "").upper() == "D/E"
    ]
    updated, _result = _fill_specs(
        dates, specs, targets=targets, mode=FillMode.ALTERNATE_SHIFTS
    )
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    for employee in roster:
        if (employee.contract_line_type or "").upper() != "D/E":
            continue
        if employee.full_name.startswith("Vacant"):
            # Catalog rotation stamps 7-day E blocks on vacant lines; FT vacant
            # evenings are asserted in test_all_ft_de_lines_have_equal_total_evening_count.
            continue
        catalog_hours = targets[employee.id]
        target = portage_hours_weighted_de_alt_target(catalog_hours)
        assigned = sum(
            1
            for day in dates
            if get_grid_token(updated, row_lookup[employee.id], day) == "E"
        )
        assert assigned <= target, (
            f"{employee.full_name}: {assigned} E > target {target} for {catalog_hours}h"
        )


def test_all_ft_de_lines_have_equal_total_evening_count() -> None:
    """Every full-time (320h) D/E line must end with the same total evening count."""
    from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_alt_shift_target_for_employee,
        portage_is_fulltime_catalog_hours,
    )
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in roster
    ]
    updated, _result = _fill_specs(
        dates, specs, targets=targets, mode=FillMode.ALTERNATE_SHIFTS
    )
    row_lookup = schedule_frame_row_index_by_employee_id(updated)

    for qual_prefix in ("Vacant MLT D/E", "Vacant MLA D/E"):
        ft_counts: list[int] = []
        for employee in roster:
            if not employee.full_name.startswith(qual_prefix):
                continue
            if (employee.contract_line_type or "").upper() != "D/E":
                continue
            catalog_hours = float(targets.get(employee.id, 0.0))
            if not portage_is_fulltime_catalog_hours(catalog_hours):
                continue
            target = portage_alt_shift_target_for_employee(employee, catalog_hours)
            assigned = sum(
                1
                for day in dates
                if get_grid_token(updated, row_lookup[employee.id], day) == "E"
            )
            assert assigned == target, (
                f"{employee.full_name}: {assigned} E != target {target}"
            )
            ft_counts.append(assigned)
        if len(ft_counts) >= 2:
            assert max(ft_counts) - min(ft_counts) == 0, (
                f"{qual_prefix} FT evening spread {ft_counts}"
            )


def test_mirror_weekend_partner() -> None:
    saturday = date(2026, 6, 6)
    sunday = date(2026, 6, 7)
    assert mirror_weekend_partner(saturday) == sunday
    assert mirror_weekend_partner(sunday) == saturday


def test_named_staff_skipped() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("named-1", "Jane Smith", "D/E"),
    ]
    frame = _empty_frame(dates, specs)
    employees = _employees_from_specs(specs)
    profiles_list = [
        EmployeeProfile(
            id="named-1",
            full_name="Jane Smith",
            fte=1.0,
            qualification_ids={"qual-mlt"},
            contract_line_type="D/E",
        )
    ]
    profiles = build_portage_scheduling_profiles(
        frame,
        profiles_list,
        employee_target_hours={"named-1": 320.0},
        qual_codes={"qual-mlt": "MLT"},
    )
    assert profiles["named-1"].eligible_for_fill is False
    updated, result = fill_schedule_by_preferences(
        frame,
        employees=employees,
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={"named-1": {"qual-mlt"}},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours={"named-1": 320.0},
        policy=PORTAGE_DEFAULT_POLICY,
        profiles=profiles,
    )
    assert result.cells_changed == 0
    row = updated.iloc[0]
    assert all(
        normalize_grid_shift_token(row.get(day.isoformat(), "")) == ""
        for day in dates
    )
