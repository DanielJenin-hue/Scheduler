from lab_scheduler.engine.demand import count_expanded_slots, portage_concurrent_demands
from lab_scheduler.simulation.hospital_stress import PERIOD_END, PERIOD_START, shift_templates
from lab_scheduler.simulation.load_test import (
    PORTAGE_MLA_COUNT,
    PORTAGE_MLT_COUNT,
    PORTAGE_ROSTER_SIZE,
    build_portage_roster,
    run_portage_load_test,
)


def test_portage_roster_shape() -> None:
    roster = build_portage_roster()
    assert len(roster) == PORTAGE_ROSTER_SIZE == 25
    mlt = sum(1 for employee in roster if "portage-mlt" in employee.id)
    mla = sum(1 for employee in roster if "portage-mla" in employee.id)
    assert mlt == PORTAGE_MLT_COUNT == 13
    assert mla == PORTAGE_MLA_COUNT == 12
    seniority_values = {employee.seniority_hours for employee in roster}
    assert len(seniority_values) == len(roster)


def test_portage_load_test_summary_passes_benchmarks() -> None:
    summary = run_portage_load_test()
    assert not summary.exception_occurred
    assert summary.coverage_success_rate_pct >= 85.0


def test_portage_load_test_reports_coverage_success_rate() -> None:
    summary = run_portage_load_test()
    assert not summary.exception_occurred
    assert summary.coverage_success_rate_pct >= 85.0


def test_portage_load_test_tracks_optional_gaps_without_forcing_fill() -> None:
    summary = run_portage_load_test()
    assert not summary.exception_occurred
    assert summary.gap_count >= 0
    assert summary.coverage_success_rate_pct >= 85.0
