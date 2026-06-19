from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.engine.demand import infer_qual_code, roster_line_number
from lab_scheduler.scheduling.auto_generate import auto_generate_schedule
from lab_scheduler.scheduling.portage_template import (
    PORTAGE_CYCLE_WEEKS,
    PORTAGE_MLT_LINE_SPECS,
    _MLA_DE_FULL,
    _MLA_DE_PT,
    _MLA_DE_PT_LIGHT,
    line_cycle_pattern,
    portage_master_line_spec,
    shift_token_for_day,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_coverage_targets,
    portage_employee_target_hours,
)
from portage_fixtures import portage_generate_kwargs


def test_portage_master_line_specs_cover_nine_mlt_de_catalog_lines() -> None:
    assert len(PORTAGE_MLT_LINE_SPECS) == 9
    assert all(spec.role == "MLT" for spec in PORTAGE_MLT_LINE_SPECS.values())
    assert all(spec.contract_line_type == "D/E" for spec in PORTAGE_MLT_LINE_SPECS.values())
    assert PORTAGE_MLT_LINE_SPECS[1].target_fte == 1.0
    assert PORTAGE_MLT_LINE_SPECS[9].target_fte == 0.2


def test_line_offset_rotates_eight_week_cycle() -> None:
    line1 = line_cycle_pattern(PORTAGE_MLT_LINE_SPECS[1])
    line2 = line_cycle_pattern(PORTAGE_MLT_LINE_SPECS[2])
    assert line1[0] != line2[0]
    assert len(line1) == PORTAGE_CYCLE_WEEKS
    assert line1[1:] + line1[:1] == line2


def test_eight_week_pattern_tiles_onto_four_week_period() -> None:
    profile = EmployeeProfile(
        id="portage-mlt-de-01",
        full_name="Vacant MLT D/E - Line 01",
        fte=1.0,
        qualification_ids={QUAL_MLT},
        contract_line_type="D/E",
    )
    spec = portage_master_line_spec(profile)
    assert spec is not None
    for day in range(7):
        assert shift_token_for_day(spec, week_index=0, day_of_week=day) == shift_token_for_day(
            spec, week_index=8, day_of_week=day
        )
    assert shift_token_for_day(spec, week_index=3, day_of_week=2) == "E"


def test_mlt_de_line_nine_catalog_work_days_in_summer_period() -> None:
    """Line 09 minimal PT pattern should call for eight 8h shifts per 8-week period."""

    from lab_scheduler.scheduling.portage_template import vacant_master_scheduled_shift_code

    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    profile = EmployeeProfile(
        id="portage-mlt-09",
        full_name="Vacant MLT D/E - Line 09",
        fte=0.2,
        qualification_ids={QUAL_MLT},
        contract_line_type="D/E",
    )
    work_days = 0
    day = period_start
    while day <= period_end:
        if vacant_master_scheduled_shift_code(profile, day, period_start):
            work_days += 1
        day += timedelta(days=1)
    assert work_days == 8, f"expected 8 catalog work days, got {work_days}"


def test_mlt_dn_fulltime_lines_use_screenshot_reference_catalog() -> None:
    """Each MLT D/N line uses its own screenshot-derived 8-week reference grid."""

    from lab_scheduler.scheduling.portage_dn_reference import reference_cycle_for_line
    from lab_scheduler.scheduling.portage_template import _mlt_dn_catalog_spec, line_cycle_pattern

    for line in range(1, 5):
        spec = _mlt_dn_catalog_spec(line)
        assert spec is not None
        assert spec.week_offset == 0
        assert line_cycle_pattern(spec) == reference_cycle_for_line(role="MLT", line=line)


def test_catalog_master_stamp_protected_blocks_trim() -> None:
    """Assignments matching the 8-week catalog token must not count as trim surplus."""

    from lab_scheduler.scheduling.auto_generate import _catalog_master_stamp_protected
    from lab_scheduler.scheduling.auto_generate import PlannedAssignment
    from lab_scheduler.compliance.engine import ShiftTemplateInfo

    period_start = date(2026, 6, 1)
    profile = EmployeeProfile(
        id="portage-mlt-01",
        full_name="Vacant MLT D/N - Line 01",
        fte=1.0,
        qualification_ids={QUAL_MLT},
        contract_line_type="D/N",
    )
    night_id = "shift-night"
    shift_templates = {
        night_id: ShiftTemplateInfo(
            night_id, "NIGHT", "Night", "23:00", "07:00", 480, True
        )
    }
    from lab_scheduler.scheduling.portage_template import vacant_master_scheduled_shift_code

    protected_date = date(2026, 6, 2)
    while vacant_master_scheduled_shift_code(profile, protected_date, period_start) != "NIGHT":
        protected_date += timedelta(days=1)
    assignment = PlannedAssignment(
        employee_id=profile.id,
        shift_template_id=night_id,
        assignment_date=protected_date,
        master_template_frozen=True,
    )
    assert _catalog_master_stamp_protected(
        profile,
        assignment,
        period_start,
        shift_templates,
    )


def test_template_propagation_produces_evening_blocks_for_mlt_line_one() -> None:
    kwargs = portage_generate_kwargs()
    result = auto_generate_schedule(**kwargs)
    line_five = next(
        employee for employee in kwargs["employees"] if employee.id == "portage-mlt-05"
    )
    worked_dates = sorted(
        assignment.assignment_date
        for assignment in result.assignments
        if assignment.employee_id == line_five.id
    )
    assert len(worked_dates) >= 12


def test_healing_fills_pto_gap_without_removing_template_blocks() -> None:
    kwargs = portage_generate_kwargs(
        period_end=date(2026, 6, 14),
        weeks_in_period=2,
    )
    employee = next(employee for employee in kwargs["employees"] if employee.id == "portage-mlt-11")
    pto_blocked = {employee.id: {date(2026, 6, 3)}}
    result = auto_generate_schedule(
        **kwargs,
        availability_blocked=pto_blocked,
    )
    assigned_dates = {
        assignment.assignment_date
        for assignment in result.assignments
        if assignment.employee_id == employee.id
    }
    assert date(2026, 6, 3) not in assigned_dates
    assert date(2026, 6, 1) in assigned_dates
    assert date(2026, 6, 2) in assigned_dates


def test_portage_load_roster_gets_structured_template_assignments() -> None:
    employees = build_portage_roster()
    targets = portage_coverage_targets(employees)
    target_hours = portage_employee_target_hours(
        employees,
        weeks_in_period=4,
        rules=MANITOBA,
    )
    result = auto_generate_schedule(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employees=employees,
        shift_templates=shift_templates(),
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=target_hours,
        coverage_targets=targets,
        strict_complete_block=False,
    )
    by_employee: dict[str, list[date]] = {}
    for assignment in result.assignments:
        by_employee.setdefault(assignment.employee_id, []).append(assignment.assignment_date)

    line_one = next(employee for employee in employees if roster_line_number(employee) == 1)
    line_one_dates = sorted(by_employee.get(line_one.id, []))
    assert len(line_one_dates) >= 12
    consecutive = 1
    best = 1
    for index in range(1, len(line_one_dates)):
        if line_one_dates[index] == line_one_dates[index - 1] + timedelta(days=1):
            consecutive += 1
            best = max(best, consecutive)
        else:
            consecutive = 1
    assert best >= 3

    mlt_assignments = [
        assignment
        for assignment in result.assignments
        if infer_qual_code(next(employee for employee in employees if employee.id == assignment.employee_id))
        == "MLT"
    ]
    assert len(mlt_assignments) > len(employees)


def test_mlt_de_template_includes_day_and_evening_tokens() -> None:
    profile = EmployeeProfile(
        id="portage-mlt-05",
        full_name="Vacant MLT D/E - Line 05",
        fte=1.0,
        qualification_ids={QUAL_MLT},
        contract_line_type="D/E",
    )
    spec = portage_master_line_spec(profile)
    assert spec is not None
    cycle = line_cycle_pattern(spec)
    tokens = {token for week in cycle for token in week if token}
    assert "D" in tokens
    assert "E" in tokens


def test_vacant_mla_de_line_uses_profile_fte_when_below_catalog_line() -> None:
    """Gap-fill PT rows named Line 06–08 must not inherit FT catalog targets."""

    line_six = EmployeeProfile(
        id="portage-mla-10",
        full_name="Vacant MLA D/E - Line 06",
        fte=0.7,
        qualification_ids=set(),
        contract_line_type="D/E",
    )
    line_seven = EmployeeProfile(
        id="portage-mla-11",
        full_name="Vacant MLA D/E - Line 07",
        fte=0.6,
        qualification_ids=set(),
        contract_line_type="D/E",
    )
    line_eight = EmployeeProfile(
        id="portage-mla-12",
        full_name="Vacant MLA D/E - Line 08",
        fte=0.4,
        qualification_ids=set(),
        contract_line_type="D/E",
    )

    spec_six = portage_master_line_spec(line_six)
    spec_seven = portage_master_line_spec(line_seven)
    spec_eight = portage_master_line_spec(line_eight)

    assert spec_six is not None and spec_six.target_fte == 0.7
    assert spec_six.cycle_pattern == _MLA_DE_PT
    assert spec_six.cycle_pattern != _MLA_DE_FULL

    assert spec_seven is not None and spec_seven.target_fte == 0.6
    assert spec_seven.cycle_pattern == _MLA_DE_PT

    assert spec_eight is not None and spec_eight.target_fte == 0.4
    assert spec_eight.cycle_pattern == _MLA_DE_PT_LIGHT


def test_portage_mla_roster_receives_contract_hours() -> None:
    kwargs = portage_generate_kwargs()
    mla_employees = [
        employee for employee in kwargs["employees"] if "portage-mla" in employee.id
    ]
    result = auto_generate_schedule(**kwargs)
    templates = kwargs["shift_templates"]
    hours_by_employee = {employee.id: 0.0 for employee in mla_employees}
    for assignment in result.assignments:
        if assignment.employee_id not in hours_by_employee:
            continue
        template = templates[assignment.shift_template_id]
        hours_by_employee[assignment.employee_id] += template.duration_minutes / 60.0
    worked = [hours for hours in hours_by_employee.values() if hours > 0.01]
    assert worked
    assert min(worked) > 0.0
    assert sum(1 for hours in hours_by_employee.values() if hours >= 16.0) >= 4
