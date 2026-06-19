"""Tests for manual alternate-shift redistribution."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.policy.frame_bridge import normalize_grid_shift_token
from lab_scheduler.scheduling.alternate_shift_distributor import (
    FT_WEEKEND_SHIFT_DAYS,
    alternate_band_for_contract_line,
    distribute_alternate_shifts,
    enumerate_consecutive_weekend_pairs,
    enumerate_staggered_weekend_blocks,
    weekend_band_for_contract_line,
    weekend_shift_token_for_employee,
)
from lab_scheduler.scheduling.portage_equity_targets import portage_is_fulltime_catalog_hours
from lab_scheduler.scheduling.profiles import EmployeeProfile


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


def _db_templates() -> dict[str, dict[str, str]]:
    return {
        "shift-morning": {"short": "D", "code": "MORNING"},
        "shift-evening": {"short": "E", "code": "EVENING"},
        "shift-night": {"short": "N", "code": "NIGHT"},
    }


def _period_dates(start: date, weeks: int = 8) -> list[date]:
    end = start + timedelta(days=(weeks * 7) - 1)
    dates: list[date] = []
    current = start
    while current <= end:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def _build_ft_pool_frame(
    dates: list[date],
    *,
    employee_specs: list[tuple[str, str, str]],
) -> pd.DataFrame:
    rows = []
    for employee_id, name, contract in employee_specs:
        row: dict[str, object] = {
            "employee_id": employee_id,
            "Employee": name,
            "contract_line_type": contract,
        }
        alt = alternate_band_for_contract_line(contract)
        for day in dates:
            if day.weekday() < 5:
                row[day.isoformat()] = alt if day.day % 9 == 0 else "D"
            else:
                row[day.isoformat()] = "—"
        rows.append(row)
    return pd.DataFrame(rows)


def _employees_from_specs(
    specs: list[tuple[str, str, str]],
) -> list[dict[str, object]]:
    return [
        {
            "id": employee_id,
            "full_name": name,
            "fte": 1.0,
            "contract_line_type": contract,
        }
        for employee_id, name, contract in specs
    ]


def _weekend_shift_days(
    frame: pd.DataFrame,
    employee_id: str,
    dates: list[date],
) -> int:
    row = frame[frame["employee_id"] == employee_id].iloc[0]
    return sum(
        1
        for day in dates
        if day.weekday() >= 5
        and normalize_grid_shift_token(row.get(day.isoformat(), "")) in {"D", "E", "N"}
    )


def _assigned_weekend_dates(
    frame: pd.DataFrame,
    employee_id: str,
    dates: list[date],
) -> list[date]:
    row = frame[frame["employee_id"] == employee_id].iloc[0]
    return [
        day
        for day in dates
        if day.weekday() >= 5
        and normalize_grid_shift_token(row.get(day.isoformat(), "")) in {"D", "E", "N"}
    ]


def _assert_consecutive_weekend_pair(weekend_days: list[date]) -> None:
    assert len(weekend_days) == FT_WEEKEND_SHIFT_DAYS
    saturdays = sorted(day for day in weekend_days if day.weekday() == 5)
    assert len(saturdays) == 2
    assert (saturdays[1] - saturdays[0]).days == 7
    for saturday in saturdays:
        sunday = saturday + timedelta(days=1)
        assert sunday in weekend_days


def _assert_sat_sun_mirror(
    frame: pd.DataFrame,
    employee_id: str,
    dates: list[date],
) -> None:
    row = frame[frame["employee_id"] == employee_id].iloc[0]
    for day in dates:
        if day.weekday() != 5:
            continue
        sunday = day + timedelta(days=1)
        if sunday not in dates:
            continue
        sat = normalize_grid_shift_token(row.get(day.isoformat(), ""))
        sun = normalize_grid_shift_token(row.get(sunday.isoformat(), ""))
        if sat in {"D", "E", "N"} or sun in {"D", "E", "N"}:
            assert sat == sun


def _count_alt(
    frame: pd.DataFrame,
    employee_id: str,
    dates: list[date],
    contract: str,
) -> int:
    alt = alternate_band_for_contract_line(contract)
    row = frame[frame["employee_id"] == employee_id].iloc[0]
    return sum(
        1
        for day in dates
        if day.weekday() < 5
        and normalize_grid_shift_token(row.get(day.isoformat(), "")) == alt
    )


def test_alternate_band_mapping() -> None:
    assert alternate_band_for_contract_line("D/E") == "E"
    assert alternate_band_for_contract_line("D/N") == "N"
    assert weekend_band_for_contract_line("D/E") == "E"
    assert weekend_band_for_contract_line("D/N") == "N"


def test_enumerate_consecutive_weekend_pairs() -> None:
    start = date(2026, 6, 1)
    end = start + timedelta(days=27)
    pairs = enumerate_consecutive_weekend_pairs(start, end)
    assert pairs
    (sat1, sun1), (sat2, sun2) = pairs[0]
    assert sat1.weekday() == 5
    assert sun1.weekday() == 6
    assert (sat2 - sat1).days == 7


def _daily_band_count(frame: pd.DataFrame, dates: list[date], band: str) -> dict[date, int]:
    counts: dict[date, int] = {}
    for day in dates:
        if day.weekday() >= 5:
            continue
        counts[day] = sum(
            1
            for _, row in frame.iterrows()
            if normalize_grid_shift_token(row.get(day.isoformat(), "")) == band
        )
    return counts


def test_mla_dn_weekends_use_alternate_band_without_de_conflict() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("mla-dn-01", "Vacant MLA D/N - Line 01", "D/N"),
        ("mla-de-01", "Vacant MLA D/E - Line 01", "D/E"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={
            "mla-de-01": {"qual-mla"},
            "mla-dn-01": {"qual-mla"},
        },
        qual_codes={"qual-mla": "MLA"},
        employee_target_hours={
            "mla-de-01": 320.0,
            "mla-dn-01": 320.0,
        },
    )
    dn_weekends = _assigned_weekend_dates(updated, "mla-dn-01", dates)
    assert len(dn_weekends) == FT_WEEKEND_SHIFT_DAYS
    row = updated[updated["employee_id"] == "mla-dn-01"].iloc[0]
    assert all(
        normalize_grid_shift_token(row.get(day.isoformat(), "")) == "N"
        for day in dn_weekends
    )
    de_weekends = _assigned_weekend_dates(updated, "mla-de-01", dates)
    assert de_weekends
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    assert set(dn_weekends) == set(blocks[0])
    assert set(de_weekends) == set(blocks[0])
    de_row = updated[updated["employee_id"] == "mla-de-01"].iloc[0]
    assert all(
        normalize_grid_shift_token(de_row.get(day.isoformat(), "")) == "E"
        for day in de_weekends
    )
    assert not any("mla-dn-01" in warning for warning in result.warnings)


def test_mla_de_lines_stagger_within_de_pool() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("mla-dn-01", "Vacant MLA D/N - Line 01", "D/N"),
        ("mla-de-01", "Vacant MLA D/E - Line 01", "D/E"),
        ("mla-dn-02", "Vacant MLA D/N - Line 02", "D/N"),
        ("mla-de-02", "Vacant MLA D/E - Line 02", "D/E"),
        ("mla-de-03", "Vacant MLA D/E - Line 03", "D/E"),
        ("mla-de-04", "Vacant MLA D/E - Line 04", "D/E"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={employee_id: {"qual-mla"} for employee_id, _, _ in specs},
        qual_codes={"qual-mla": "MLA"},
        employee_target_hours={employee_id: 320.0 for employee_id, _, _ in specs},
    )
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    for line_no, block_index in enumerate(range(4)):
        employee_id = f"mla-de-0{line_no + 1}"
        weekends = set(_assigned_weekend_dates(updated, employee_id, dates))
        assert weekends == set(blocks[block_index]), employee_id
    assert not any("mla-de-0" in warning for warning in result.warnings)


def test_mla_de_four_lines_on_portage_roster() -> None:
    from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    employees = build_portage_roster()
    de_lines = [
        employee
        for employee in employees
        if (employee.contract_line_type or "").upper() == "D/E"
        and employee.full_name.startswith("Vacant MLA")
        and portage_employee_target_hours([employee], weeks_in_period=8, rules=MANITOBA)[employee.id] >= 312
    ]
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in de_lines[:4]
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    target_hours = portage_employee_target_hours(
        [employee for employee in employees if employee.id in {s[0] for s in specs}],
        weeks_in_period=8,
        rules=MANITOBA,
    )
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={employee_id: {"qual-mla"} for employee_id, _, _ in specs},
        qual_codes={"qual-mla": "MLA"},
        employee_target_hours=target_hours,
    )
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    for index, (employee_id, _, _) in enumerate(specs):
        weekends = set(_assigned_weekend_dates(updated, employee_id, dates))
        assert weekends == set(blocks[index]), employee_id
    assert not result.warnings


def test_de_lines_one_to_four_evening_and_five_to_eight_day_weekends() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        (f"mlt-de-{line_no:02d}", f"Vacant MLT D/E - Line {line_no:02d}", "D/E")
        for line_no in range(1, 9)
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    employees = _employees_from_specs(specs)
    profile_objs = [
        EmployeeProfile(
            id=employee_id,
            full_name=name,
            fte=1.0,
            qualification_ids={"qual-mlt"},
            contract_line_type=contract,
        )
        for employee_id, name, contract in specs
    ]
    assert weekend_shift_token_for_employee(profile_objs[0]) == "E"
    assert weekend_shift_token_for_employee(profile_objs[4]) == "D"

    updated, result = distribute_alternate_shifts(
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
        emp_quals={employee_id: {"qual-mlt"} for employee_id, _, _ in specs},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours={employee_id: 320.0 for employee_id, _, _ in specs},
    )
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    for line_no in range(1, 5):
        employee_id = f"mlt-de-{line_no:02d}"
        weekends = set(_assigned_weekend_dates(updated, employee_id, dates))
        assert weekends == set(blocks[line_no - 1])
        row = updated[updated["employee_id"] == employee_id].iloc[0]
        assert all(
            normalize_grid_shift_token(row.get(day.isoformat(), "")) == "E"
            for day in weekends
        )
    for line_no in range(5, 9):
        employee_id = f"mlt-de-{line_no:02d}"
        weekends = set(_assigned_weekend_dates(updated, employee_id, dates))
        assert weekends == set(blocks[line_no - 5])
        row = updated[updated["employee_id"] == employee_id].iloc[0]
        assert all(
            normalize_grid_shift_token(row.get(day.isoformat(), "")) == "D"
            for day in weekends
        )
    assert not any("mlt-de-0" in warning for warning in result.warnings)


def test_portage_mlt_and_mla_de_lines_five_through_eight_get_weekend_days() -> None:
    from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    employees = build_portage_roster()
    de_lines = [
        employee
        for employee in employees
        if (employee.contract_line_type or "").upper() == "D/E"
        and employee.full_name.startswith(("Vacant MLT", "Vacant MLA"))
    ]
    targets = portage_employee_target_hours(employees, weeks_in_period=8, rules=MANITOBA)
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/E")
        for employee in de_lines
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={
            employee.id: employee.qualification_ids for employee in de_lines
        },
        qual_codes={"qual-mlt": "MLT", "qual-mla": "MLA"},
        employee_target_hours=targets,
    )
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    for employee in de_lines:
        from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

        parsed = parse_vacant_portage_line(employee.full_name)
        assert parsed is not None
        _role, _contract, line_no = parsed
        if line_no < 5 or line_no > 8:
            continue
        from lab_scheduler.scheduling.alternate_shift_distributor import (
            _weekend_stagger_block_index,
        )

        block_index = _weekend_stagger_block_index(line_no)
        weekends = set(_assigned_weekend_dates(updated, employee.id, dates))
        assert weekends == set(blocks[block_index]), employee.full_name
        row = updated[updated["employee_id"] == employee.id].iloc[0]
        assert all(
            normalize_grid_shift_token(row.get(day.isoformat(), "")) == "D"
            for day in weekends
        )
    assert not [warning for warning in result.warnings if "Line 0" in warning]


def test_portage_mlt_and_mla_de_lines_five_six_get_weekend_days() -> None:
    """Backward-compatible alias; see lines-five-through-eight test."""
    test_portage_mlt_and_mla_de_lines_five_through_eight_get_weekend_days()

def test_staggered_weekend_blocks_do_not_overlap() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("line-01", "Vacant MLT D/E - Line 01", "D/E"),
        ("line-02", "Vacant MLT D/E - Line 02", "D/E"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    updated, _result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={"line-01": {"qual-mlt"}, "line-02": {"qual-mlt"}},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours={"line-01": 320.0, "line-02": 320.0},
    )
    line1 = set(_assigned_weekend_dates(updated, "line-01", dates))
    line2 = set(_assigned_weekend_dates(updated, "line-02", dates))
    assert line1 and line2
    assert not line1.intersection(line2)

    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    assert line1 == set(blocks[0])
    assert line2 == set(blocks[1])


def test_mlt_dn_lines_stagger_within_dn_pool_despite_de_sibling_lines() -> None:
    """D/N Line 1 -> W1-2 and Line 2 -> W3-4 even when D/E lines share the qual."""
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("mlt-dn-01", "Vacant MLT D/N - Line 01", "D/N"),
        ("mlt-dn-02", "Vacant MLT D/N - Line 02", "D/N"),
        ("mlt-de-01", "Vacant MLT D/E - Line 01", "D/E"),
        ("mlt-de-02", "Vacant MLT D/E - Line 02", "D/E"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={
            "mlt-dn-01": {"qual-mlt"},
            "mlt-dn-02": {"qual-mlt"},
            "mlt-de-01": {"qual-mlt"},
            "mlt-de-02": {"qual-mlt"},
        },
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours={
            "mlt-dn-01": 320.0,
            "mlt-dn-02": 320.0,
            "mlt-de-01": 320.0,
            "mlt-de-02": 320.0,
        },
    )
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    dn1 = set(_assigned_weekend_dates(updated, "mlt-dn-01", dates))
    dn2 = set(_assigned_weekend_dates(updated, "mlt-dn-02", dates))
    de1 = set(_assigned_weekend_dates(updated, "mlt-de-01", dates))
    assert dn1 == set(blocks[0])
    assert dn2 == set(blocks[1])
    assert de1 == set(blocks[0])
    assert not dn1.intersection(dn2)
    assert not any("mlt-dn-02" in warning for warning in result.warnings)


def test_mlt_dn_four_lines_stagger_across_hour_tiers() -> None:
    """316h and 320h D/N lines share one stagger: L3 W5-6, L4 W7-8."""
    from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    employees = build_portage_roster()
    dn_employees = [
        employee
        for employee in employees
        if (employee.contract_line_type or "").upper() == "D/N"
        and employee.full_name.startswith("Vacant MLT")
    ]
    assert len(dn_employees) == 4
    specs = [
        (employee.id, employee.full_name, employee.contract_line_type or "D/N")
        for employee in dn_employees
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    target_hours = portage_employee_target_hours(
        dn_employees, weeks_in_period=8, rules=MANITOBA
    )
    emp_quals = {employee.id: employee.qualification_ids for employee in dn_employees}
    qual_codes = {"qual-mlt": "MLT"}
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals=emp_quals,
        qual_codes=qual_codes,
        employee_target_hours=target_hours,
    )
    blocks = enumerate_staggered_weekend_blocks(start, dates[-1])
    by_line = {
        employee_id: set(_assigned_weekend_dates(updated, employee_id, dates))
        for employee_id, _, _ in specs
    }
    assert by_line["portage-mlt-01"] == set(blocks[0])
    assert by_line["portage-mlt-02"] == set(blocks[1])
    assert by_line["portage-mlt-03"] == set(blocks[2])
    assert by_line["portage-mlt-04"] == set(blocks[3])
    assert not any("portage-mlt-03" in warning for warning in result.warnings)
    assert not any("portage-mlt-04" in warning for warning in result.warnings)


def test_distribute_respects_daily_two_evening_cap() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("line-01", "Vacant MLT D/E - Line 01", "D/E"),
        ("line-02", "Vacant MLT D/E - Line 02", "D/E"),
        ("line-03", "Vacant MLT D/E - Line 03", "D/E"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    for day in dates:
        if day.weekday() >= 5:
            continue
        for row_idx in range(len(specs)):
            frame.at[row_idx, day.isoformat()] = "E"
    updated, _result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={
            "line-01": {"qual-mlt"},
            "line-02": {"qual-mlt"},
            "line-03": {"qual-mlt"},
        },
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours={
            "line-01": 320.0,
            "line-02": 320.0,
            "line-03": 320.0,
        },
    )
    evening_counts = _daily_band_count(updated, dates, "E")
    assert all(count <= 2 for count in evening_counts.values())


def test_distribute_places_four_weekend_days_on_ft_lines() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("line-01", "Vacant MLT D/E - Line 01", "D/E"),
        ("line-02", "Vacant MLT D/E - Line 02", "D/E"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    target_hours = {"line-01": 320.0, "line-02": 320.0}
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={"line-01": {"qual-mlt"}, "line-02": {"qual-mlt"}},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours=target_hours,
    )
    assert result.cells_changed > 0
    for employee_id, _, _ in specs:
        assert _weekend_shift_days(updated, employee_id, dates) == FT_WEEKEND_SHIFT_DAYS
        _assert_consecutive_weekend_pair(_assigned_weekend_dates(updated, employee_id, dates))
        _assert_sat_sun_mirror(updated, employee_id, dates)


def test_distribute_dn_lines_use_nights_on_weekends() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("line-01", "Vacant MLT D/N - Line 01", "D/N"),
        ("line-02", "Vacant MLT D/N - Line 02", "D/N"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    updated, _result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={"line-01": {"qual-mlt"}, "line-02": {"qual-mlt"}},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours={"line-01": 320.0, "line-02": 320.0},
    )
    for employee_id, _, _ in specs:
        weekend_days = _assigned_weekend_dates(updated, employee_id, dates)
        assert weekend_days
        row = updated[updated["employee_id"] == employee_id].iloc[0]
        for day in weekend_days:
            assert normalize_grid_shift_token(row.get(day.isoformat(), "")) == "N"
        weekday_nights = sum(
            1
            for day in dates
            if day.weekday() < 5
            and normalize_grid_shift_token(row.get(day.isoformat(), "")) == "N"
        )
        assert weekday_nights >= 1


def test_distribute_respects_locked_cells() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [("line-01", "Vacant MLT D/E - Line 01", "D/E")]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    locked_day = next(day for day in dates if day.weekday() == 5)
    locked_token = frame.at[0, locked_day.isoformat()]
    updated, _result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells={("line-01", locked_day)},
        blocked_map={},
        emp_quals={"line-01": {"qual-mlt"}},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours={"line-01": 320.0},
    )
    assert normalize_grid_shift_token(updated.at[0, locked_day.isoformat()]) == normalize_grid_shift_token(
        locked_token
    )


def test_distribute_balances_alt_counts_within_peer_pool() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("line-01", "Vacant MLT D/E - Line 01", "D/E"),
        ("line-02", "Vacant MLT D/E - Line 02", "D/E"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    frame.at[0, dates[10].isoformat()] = "E"
    frame.at[0, dates[11].isoformat()] = "E"
    frame.at[0, dates[12].isoformat()] = "E"
    frame.at[0, dates[13].isoformat()] = "E"
    frame.at[0, dates[14].isoformat()] = "E"
    frame.at[1, dates[10].isoformat()] = "D"
    target_hours = {"line-01": 320.0, "line-02": 320.0}
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={"line-01": {"qual-mlt"}, "line-02": {"qual-mlt"}},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours=target_hours,
    )
    counts = [
        _count_alt(updated, employee_id, dates, "D/E")
        for employee_id, _, _ in specs
    ]
    assert max(counts) - min(counts) <= 2
    assert result.pool_summaries


def test_distribute_skips_part_time_lines() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("line-ft", "Vacant MLT D/E - Line 01", "D/E"),
        ("line-pt", "Vacant MLT D/E - Line 09", "D/E"),
    ]
    frame = _build_ft_pool_frame(dates, employee_specs=specs)
    original_pt_weekends = _weekend_shift_days(frame, "line-pt", dates)
    updated, result = distribute_alternate_shifts(
        frame,
        employees=_employees_from_specs(specs),
        dates=dates,
        period_start=start,
        period_end=dates[-1],
        weeks_in_period=8,
        rules=MANITOBA,
        templates=_db_templates(),
        shift_templates=_templates(),
        locked_cells=set(),
        blocked_map={},
        emp_quals={"line-ft": {"qual-mlt"}, "line-pt": {"qual-mlt"}},
        qual_codes={"qual-mlt": "MLT"},
        employee_target_hours={"line-ft": 320.0, "line-pt": 64.0},
    )
    assert _weekend_shift_days(updated, "line-ft", dates) == FT_WEEKEND_SHIFT_DAYS
    assert _weekend_shift_days(updated, "line-pt", dates) == original_pt_weekends
    assert result.lines_touched >= 1


def test_focus_controls_include_distribute_button() -> None:
    import inspect

    from scripts.app import _render_focus_controls

    source = inspect.getsource(_render_focus_controls)
    assert "Distribute weekend shifts" in source
    assert "Fill alternate shifts" in source
    assert "_schedule_distribute_alt_pending_key" in source
    assert "_schedule_alternate_fill_pending_key" in source
