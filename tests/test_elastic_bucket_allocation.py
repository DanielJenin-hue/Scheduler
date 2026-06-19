from __future__ import annotations

from datetime import timedelta
from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.balanced_load import (
    CAPACITY_SHORTFALL_MESSAGE,
    assess_elastic_capacity_shortfall,
    balanced_load_rank_key,
    hour_variance,
)
from lab_scheduler.scheduling.pool_manager import ElasticPoolManager
from lab_scheduler.scheduling.portage_template import line_cycle_pattern, portage_pattern_for_bucket
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.load_test import build_portage_roster


def test_pool_manager_assigns_stable_bucket_indices() -> None:
    employees = build_portage_roster()
    pool = ElasticPoolManager.from_employees(employees)

    assert pool.staff_count() == len(employees)
    assert pool.staff_count("MLT") == 13
    assert pool.staff_count("MLA") == 12

    mlt_ids = pool.role_pools["MLT"]
    first = pool.members[mlt_ids[0]]
    second = pool.members[mlt_ids[1]]
    assert first.bucket_index == 0
    assert second.bucket_index == 1


def test_new_hire_shifts_pool_average() -> None:
    rules = MANITOBA
    employees = build_portage_roster()
    baseline = ElasticPoolManager.from_employees(employees)
    baseline_avg = baseline.pool_average_hours(
        baseline.role_pools["MLT"][0],
        rules=rules,
        weeks_in_period=4,
    )

    extra = EmployeeProfile(
        id="new-mlt-hire",
        full_name="New MLT Hire",
        fte=1.0,
        qualification_ids=employees[0].qualification_ids,
        contract_line_type="D/E",
    )
    expanded = ElasticPoolManager.from_employees([*employees, extra])
    new_avg = expanded.pool_average_hours(
        extra.id,
        rules=rules,
        weeks_in_period=4,
    )

    assert expanded.staff_count("MLT") == 14
    expected_avg = expanded.role_capacity_hours(
        "MLT",
        rules=rules,
        weeks_in_period=4,
    ) / 14
    assert abs(new_avg - expected_avg) < 0.02
    assert new_avg != baseline_avg


def test_portage_pattern_for_bucket_does_not_parse_line_name() -> None:
    spec_a = portage_pattern_for_bucket(
        role="MLT",
        contract_line_type="D/E",
        fte=1.0,
        bucket_index=0,
    )
    spec_b = portage_pattern_for_bucket(
        role="MLT",
        contract_line_type="D/E",
        fte=1.0,
        bucket_index=1,
    )

    assert spec_a.line_number == 1
    assert spec_b.line_number == 2
    assert line_cycle_pattern(spec_a) != line_cycle_pattern(spec_b)


def test_balanced_load_rank_key_prefers_underloaded() -> None:
    average = 120.0
    under_key = balanced_load_rank_key(100.0, average)
    over_key = balanced_load_rank_key(140.0, average)
    assert under_key < over_key


def test_hour_variance_is_zero_for_equal_loads() -> None:
    assert hour_variance([120.0, 120.0, 120.0]) == 0.0
    assert hour_variance([100.0, 140.0]) > 0.0


def test_capacity_shortfall_message_is_non_fatal_text() -> None:
    assert "even distribution" in CAPACITY_SHORTFALL_MESSAGE.lower()


def test_assess_elastic_capacity_shortfall_when_demand_exceeds_pool() -> None:
    from datetime import date

    from lab_scheduler.compliance.engine import ShiftTemplateInfo
    from lab_scheduler.engine.demand import ExpandedScheduleSlot

    rules = MANITOBA
    tiny_pool = [
        EmployeeProfile(
            id="solo-mlt",
            full_name="Solo MLT",
            fte=0.5,
            qualification_ids=build_portage_roster()[0].qualification_ids,
            contract_line_type="D/E",
        )
    ]
    pool = ElasticPoolManager.from_employees(tiny_pool)
    templates = {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
    }
    slots = [
        ExpandedScheduleSlot(
            assignment_date=date(2026, 6, 1) + timedelta(days=index),
            shift_id="shift-morning",
            seat_index=index,
            required_qual_code="MLT",
            role_pool_id="Weekday Morning - MLT",
        )
        for index in range(80)
    ]

    alert = assess_elastic_capacity_shortfall(
        pool,
        slots,
        templates,
        rules=rules,
        weeks_in_period=4,
    )
    assert alert is not None
    assert alert.message == CAPACITY_SHORTFALL_MESSAGE
    assert alert.deficit_hours > 0
