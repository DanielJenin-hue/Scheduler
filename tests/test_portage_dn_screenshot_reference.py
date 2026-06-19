"""D/N full-time master catalog: staggered weekends and night-first stamping."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy

from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.auto_generate import auto_generate_schedule
from lab_scheduler.scheduling.portage_dn_reference import (
    PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
    PORTAGE_DN_FT_PERIOD_WORK_SHIFTS,
    PORTAGE_DN_FT_WEEKEND_PAIRS,
    dn_weekend_catalog_week_indices,
    load_portage_dn_master_reference,
    pool_daily_night_counts,
    reference_cycle_for_line,
    staggered_dn_ft_cycle_for_line,
    validate_no_day_night_adjacency,
)
from lab_scheduler.scheduling.portage_template import (
    _mlt_dn_catalog_spec,
    line_cycle_pattern,
)
from portage_fixtures import portage_generate_kwargs


def _week_tokens(cycle, week_index: int) -> tuple[str, ...]:
    week = cycle[week_index]
    return tuple("-" if token == "" else token for token in week)


def test_dn_ft_catalog_is_exactly_320_hours() -> None:
    from lab_scheduler.scheduling.contract_payroll import (
        apply_catalog_targets_for_vacant_master_lines,
    )
    from lab_scheduler.scheduling.portage_template import vacant_master_catalog_period_hours
    from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours

    employees = build_portage_roster()
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    payroll = portage_employee_target_hours(employees, weeks_in_period=8, rules=MANITOBA)
    catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        employees,
        payroll,
        rules=MANITOBA,
        weeks_in_period=8,
        period_start=period_start,
        period_end=period_end,
    )
    for line in range(1, 5):
        cycle = staggered_dn_ft_cycle_for_line(line)
        work_shifts = sum(1 for week in cycle for token in week if token)
        assert work_shifts == PORTAGE_DN_FT_PERIOD_WORK_SHIFTS
        assert work_shifts * 8 == 320

    for employee in employees:
        if (employee.contract_line_type or "").upper() != "D/N" or employee.fte < 1.0:
            continue
        assert vacant_master_catalog_period_hours(employee, period_start, period_end) == 320.0
        assert catalog_targets[employee.id] == payroll[employee.id]


def test_reference_fixture_loads_with_fourteen_n_and_two_weekend_pairs() -> None:
    entries = load_portage_dn_master_reference()
    assert len(entries) == 8
    for entry in entries:
        weeks = reference_cycle_for_line(role=str(entry["role"]), line=int(entry["line"]))
        night_count = sum(1 for week in weeks for token in week if token == "N")
        assert night_count == PORTAGE_DN_FT_NIGHT_SHIFT_TARGET
        weekend_pairs = sum(1 for week in weeks if week[5] == "N" and week[6] == "N")
        assert weekend_pairs == PORTAGE_DN_FT_WEEKEND_PAIRS
        for week in weeks:
            assert week[5] != "D"
            assert week[6] != "D"


def test_staggered_weekend_weeks_by_line_number() -> None:
    for line in range(1, 5):
        expected = dn_weekend_catalog_week_indices(line)
        for role in ("MLT", "MLA"):
            cycle = reference_cycle_for_line(role=role, line=line)
            actual = tuple(
                week_index
                for week_index, week in enumerate(cycle)
                if week[5] == "N" and week[6] == "N"
            )
            assert actual == expected
            assert cycle[expected[0]][5:] == ("N", "N")
            assert cycle[expected[0]].count("N") == 7


def test_mlt_dn_line01_opens_with_weekend_night_block() -> None:
    spec = _mlt_dn_catalog_spec(1)
    assert spec is not None
    cycle = line_cycle_pattern(spec)
    assert _week_tokens(cycle, 0) == ("N", "N", "N", "N", "N", "N", "N")
    assert _week_tokens(cycle, 1) == ("N", "N", "N", "N", "N", "N", "N")


def test_mlt_dn_line02_weekend_nights_on_catalog_weeks_three_four() -> None:
    spec = _mlt_dn_catalog_spec(2)
    assert spec is not None
    cycle = line_cycle_pattern(spec)
    assert _week_tokens(cycle, 0) == ("D", "D", "D", "D", "D", "-", "-")
    assert _week_tokens(cycle, 1) == ("D", "D", "D", "D", "D", "-", "-")
    assert _week_tokens(cycle, 2) == ("N", "N", "N", "N", "N", "N", "N")
    assert _week_tokens(cycle, 3) == ("N", "N", "N", "N", "N", "N", "N")


def test_staggered_builder_matches_reference_fixture() -> None:
    for line in range(1, 5):
        built = staggered_dn_ft_cycle_for_line(line)
        for role in ("MLT", "MLA"):
            assert reference_cycle_for_line(role=role, line=line) == built


def test_dn_zero_day_on_weekend() -> None:
    for line in range(1, 5):
        for role in ("MLT", "MLA"):
            weeks = reference_cycle_for_line(role=role, line=line)
            for week in weeks:
                assert week[5] != "D"
                assert week[6] != "D"


def test_dn_pool_has_exactly_one_night_per_day() -> None:
    from lab_scheduler.scheduling.portage_dn_reference import validate_pool_exactly_one_night_per_day

    for role in ("MLT", "MLA"):
        validate_pool_exactly_one_night_per_day(role=role)
        counts = pool_daily_night_counts(role)
        weekend_counts = [counts[day_index] for day_index in range(56) if day_index % 7 >= 5]
        assert len(weekend_counts) == 16
        assert sum(weekend_counts) == PORTAGE_DN_FT_WEEKEND_PAIRS * 2 * 4
        assert min(counts) == 1
        assert max(counts) == 1


def test_mlt_dn_fulltime_stamps_catalog_night_block() -> None:
    import os

    os.environ["LAB_SCHEDULER_QUIET"] = "1"
    from lab_scheduler.scheduling.auto_generate import _propagate_portage_template
    from lab_scheduler.simulation.hospital_stress import shift_templates
    from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours

    employees = build_portage_roster()
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 7)
    targets = portage_employee_target_hours(employees, weeks_in_period=1, rules=MANITOBA)
    assignments, _states = _propagate_portage_template(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=1,
        employees=employees,
        shift_templates=shift_templates(),
        employee_target_hours=targets,
    )
    night_id = next(tid for tid, info in shift_templates().items() if info.code == "NIGHT")
    line_one_nights = sorted(
        assignment.assignment_date
        for assignment in assignments
        if assignment.employee_id == "portage-mlt-01" and assignment.shift_template_id == night_id
    )
    assert line_one_nights == [
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
        date(2026, 6, 5),
        date(2026, 6, 6),
        date(2026, 6, 7),
    ]


def test_autopilot_summer_preserves_dn_reference_weeks() -> None:
    import os

    os.environ["LAB_SCHEDULER_QUIET"] = "1"
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 14),
        weeks_in_period=2,
    )
    result = auto_generate_schedule(**kwargs)
    code_map = {"MORNING": "D", "EVENING": "E", "NIGHT": "N"}
    shift_templates = kwargs["shift_templates"]

    def first_two_weeks(employee_id: str) -> list[tuple[str, ...]]:
        tokens: list[str] = []
        day = kwargs["period_start"]
        while day <= kwargs["period_end"]:
            assignment = next(
                (
                    row
                    for row in result.assignments
                    if row.employee_id == employee_id and row.assignment_date == day
                ),
                None,
            )
            if assignment is None:
                tokens.append("-")
            else:
                code = shift_templates[assignment.shift_template_id].code
                tokens.append(code_map[code])
            day += timedelta(days=1)
        return [tuple(tokens[index : index + 7]) for index in range(0, 14, 7)]

    def expected_weeks(line: int) -> list[tuple[str, ...]]:
        spec = _mlt_dn_catalog_spec(line)
        assert spec is not None
        cycle = line_cycle_pattern(spec)
        return [
            tuple("-" if token == "" else token for token in cycle[0]),
            tuple("-" if token == "" else token for token in cycle[1]),
        ]

    line_one_weeks = first_two_weeks("portage-mlt-01")
    assert line_one_weeks[0][:4] == ("N", "N", "N", "N")
    assert line_one_weeks[1] == expected_weeks(1)[1]

    for employee_id in ("portage-mlt-01", "portage-mlt-02", "portage-mlt-03", "portage-mlt-04"):
        weeks = first_two_weeks(employee_id)
        for week in weeks:
            assert week[5] != "D"
            assert week[6] != "D"


def test_dn_reference_has_no_day_night_adjacency() -> None:
    for entry in load_portage_dn_master_reference():
        weeks = reference_cycle_for_line(role=str(entry["role"]), line=int(entry["line"]))
        validate_no_day_night_adjacency(weeks)


def test_dn_ft_catalog_stamp_order_weekend_before_weekday() -> None:
    from lab_scheduler.scheduling.auto_generate import (
        _catalog_stamp_dates_for_employee,
        _pool_interleave_dn_weekend_catalog_stamps,
    )
    from lab_scheduler.simulation.load_test import build_portage_roster

    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 28)
    employees = build_portage_roster()
    weekend_dates_by_employee: dict[str, list[date]] = {}
    for employee, assignment_date in _pool_interleave_dn_weekend_catalog_stamps(
        employees,
        period_start,
        period_end,
    ):
        weekend_dates_by_employee.setdefault(employee.id, []).append(assignment_date)

    line_two = next(employee for employee in employees if employee.id == "portage-mlt-02")
    stamp_dates = _catalog_stamp_dates_for_employee(
        line_two,
        period_start=period_start,
        period_end=period_end,
        weekend_dates_by_employee=weekend_dates_by_employee,
    )
    first_weekend = date(2026, 6, 20)
    first_weekday = date(2026, 6, 16)
    assert stamp_dates.index(first_weekend) < stamp_dates.index(first_weekday)


def test_dn_pool_interleaves_weekend_nights_by_line_order() -> None:
    from lab_scheduler.scheduling.auto_generate import _pool_interleave_dn_weekend_catalog_stamps
    from lab_scheduler.simulation.load_test import build_portage_roster

    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 14)
    employees = build_portage_roster()
    schedule = _pool_interleave_dn_weekend_catalog_stamps(
        employees,
        period_start,
        period_end,
    )
    first_weekend_employee_ids = [
        employee.id
        for employee, assignment_date in schedule
        if assignment_date == date(2026, 6, 6)
    ]
    assert first_weekend_employee_ids == ["portage-mla-06", "portage-mlt-01"]


def test_mlt_dn_line02_propagate_stamps_weekend_before_weekday_d() -> None:
    import os

    os.environ["LAB_SCHEDULER_QUIET"] = "1"
    from lab_scheduler.scheduling.auto_generate import _propagate_portage_template
    from lab_scheduler.simulation.hospital_stress import shift_templates
    from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours

    employees = build_portage_roster()
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 21)
    targets = portage_employee_target_hours(employees, weeks_in_period=1, rules=MANITOBA)
    assignments, _states = _propagate_portage_template(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=1,
        employees=employees,
        shift_templates=shift_templates(),
        employee_target_hours=targets,
    )
    templates = shift_templates()
    line_two = [
        assignment
        for assignment in assignments
        if assignment.employee_id == "portage-mlt-02"
    ]
    stamp_order = [
        (
            assignment.assignment_date,
            templates[assignment.shift_template_id].code,
        )
        for assignment in line_two
    ]
    assert stamp_order[:2] == [
        (date(2026, 6, 20), "NIGHT"),
        (date(2026, 6, 21), "NIGHT"),
    ]
    assert stamp_order[2][0] < stamp_order[0][0]
    assert any(code == "MORNING" for _day, code in stamp_order[2:])


def test_autopilot_passes_dn_catalog_quota_persist_gate() -> None:
    import os

    os.environ["LAB_SCHEDULER_QUIET"] = "1"
    from lab_scheduler.scheduling.persist_validation import find_dn_ft_master_catalog_quota_violations

    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = auto_generate_schedule(**kwargs)
    violations = find_dn_ft_master_catalog_quota_violations(
        assignments=result.assignments,
        employees=kwargs["employees"],
        shift_templates=kwargs["shift_templates"],
        period_start=kwargs["period_start"],
        period_end=kwargs["period_end"],
    )
    assert violations == []


def test_all_dn_ft_lines_receive_four_weekend_nights_after_autopilot() -> None:
    import os

    os.environ["LAB_SCHEDULER_QUIET"] = "1"
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = auto_generate_schedule(**kwargs)
    shift_templates = kwargs["shift_templates"]
    for employee in kwargs["employees"]:
        if (employee.contract_line_type or "").upper() != "D/N" or employee.fte < 1.0:
            continue
        weekend_nights = sum(
            1
            for assignment in result.assignments
            if assignment.employee_id == employee.id
            and assignment.assignment_date.weekday() >= 5
            and shift_templates[assignment.shift_template_id].code == "NIGHT"
        )
        assert weekend_nights == PORTAGE_DN_FT_WEEKEND_PAIRS * 2, employee.full_name
