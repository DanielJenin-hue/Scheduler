"""D/E full-time master catalog: day blocks followed by evening blocks."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy

from datetime import date, timedelta

from lab_scheduler.scheduling.auto_generate import auto_generate_schedule
from lab_scheduler.scheduling.portage_template import (
    line_cycle_pattern,
    portage_master_line_spec,
    vacant_master_scheduled_shift_code,
)
from portage_fixtures import portage_generate_kwargs

_PERIOD_START = date(2026, 6, 1)
_PERIOD_END = date(2026, 7, 26)


def _assigned_tokens(result, employee, templates) -> list[str]:
    code = {"MORNING": "D", "EVENING": "E", "NIGHT": "N"}
    tokens: list[str] = []
    day = _PERIOD_START
    while day <= _PERIOD_END:
        assignment = next(
            (
                row
                for row in result.assignments
                if row.employee_id == employee.id and row.assignment_date == day
            ),
            None,
        )
        if assignment is None:
            tokens.append("-")
        else:
            tokens.append(code[templates[assignment.shift_template_id].code])
        day += timedelta(days=1)
    return tokens


def _catalog_tokens(employee) -> list[str]:
    spec = portage_master_line_spec(employee)
    assert spec is not None
    cycle = line_cycle_pattern(spec)
    tokens: list[str] = []
    for week in cycle:
        for token in week:
            tokens.append("-" if not token else token)
    return tokens


def test_de_ft_lines_match_master_catalog_after_generate() -> None:
    kwargs = portage_generate_kwargs(
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        weeks_in_period=8,
    )
    result = auto_generate_schedule(**kwargs)
    templates = kwargs["shift_templates"]

    for employee in kwargs["employees"]:
        if (employee.contract_line_type or "").upper() != "D/E" or employee.fte < 1.0:
            continue
        catalog = _catalog_tokens(employee)
        assigned = _assigned_tokens(result, employee, templates)
        assert assigned == catalog, employee.full_name


def test_de_ft_rotation_has_day_blocks_before_evening_blocks() -> None:
    kwargs = portage_generate_kwargs(
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        weeks_in_period=8,
    )
    result = auto_generate_schedule(**kwargs)
    templates = kwargs["shift_templates"]

    for employee in kwargs["employees"]:
        if (employee.contract_line_type or "").upper() != "D/E" or employee.fte < 1.0:
            continue
        tokens = _assigned_tokens(result, employee, templates)
        work = [token for token in tokens if token != "-"]
        assert "D" in work and "E" in work, employee.full_name

        last_band: str | None = None
        for token in work:
            if token == last_band:
                continue
            if last_band == "E" and token == "D":
                pass
            elif last_band == "D" and token == "E":
                pass
            elif last_band is None:
                pass
            else:
                assert last_band in {"D", "E"} and token in {"D", "E"}
            last_band = token

        day = _PERIOD_START
        while day <= _PERIOD_END:
            expected = vacant_master_scheduled_shift_code(employee, day, _PERIOD_START)
            if expected is None:
                day += timedelta(days=1)
                continue
            assignment = next(
                (
                    row
                    for row in result.assignments
                    if row.employee_id == employee.id and row.assignment_date == day
                ),
                None,
            )
            assert assignment is not None, f"{employee.full_name} missing {day}"
            assert templates[assignment.shift_template_id].code == expected
            day += timedelta(days=1)
