"""Pytest wrapper for the hospital-scale stress simulation."""

from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLA,
    QUAL_MLT,
    build_hospital_roster,
    run_hospital_stress_simulation,
)


def test_hospital_stress_simulation_completes_without_exceptions() -> None:
    result = run_hospital_stress_simulation()

    assert result.exception_occurred is False, result.exception_message
    assert result.roster_size == 35
    assert result.slots_total == 28 * 3
    assert result.slots_filled > 0
    assert 0.0 <= result.fill_rate_pct <= 100.0
    assert result.execution_seconds >= 0.0
    assert result.blocked_day_count > 0


def test_hospital_roster_fte_distribution() -> None:
    roster = build_hospital_roster()

    mlts = [e for e in roster if QUAL_MLT in e.qualification_ids]
    mlas = [e for e in roster if QUAL_MLA in e.qualification_ids]

    assert len(mlts) == 22
    assert len(mlas) == 13
    assert sum(1 for e in roster if e.fte == 1.0) == 14
    assert sum(1 for e in roster if e.fte in (0.8, 0.6)) == 17
    assert sum(1 for e in roster if e.fte in (0.4, 0.2)) == 4
