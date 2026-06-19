"""Tests for Portage weekend and alternate-shift equity targets."""

from __future__ import annotations

from lab_scheduler.scheduling.portage_equity_targets import (
    portage_alt_shift_target,
    portage_weekend_shift_target,
    PORTAGE_FULLTIME_WEEKEND_SHIFTS,
)


def test_fulltime_weekend_target_is_eight_shifts() -> None:
    assert portage_weekend_shift_target(320.0) == PORTAGE_FULLTIME_WEEKEND_SHIFTS


def test_weekend_targets_scale_with_catalog_hours_and_stay_even() -> None:
    assert portage_weekend_shift_target(160.0) == 4
    assert portage_weekend_shift_target(224.0) == 6
    assert portage_weekend_shift_target(192.0) == 4
    assert portage_weekend_shift_target(128.0) == 4
    assert portage_weekend_shift_target(64.0) == 2
    for hours in (320.0, 224.0, 160.0, 128.0, 64.0):
        assert portage_weekend_shift_target(hours) % 2 == 0


def test_alt_shift_target_is_twenty_percent_of_catalog_shifts() -> None:
    assert portage_alt_shift_target(320.0) == 8
    assert portage_alt_shift_target(328.0) == 8


def test_parttime_alt_shift_target_uses_hours_weighted_round_up() -> None:
    assert portage_alt_shift_target(224.0, contract_line_type="D/E") == 6
    assert portage_alt_shift_target(248.0, contract_line_type="D/E") == 7
    assert portage_alt_shift_target(160.0, contract_line_type="D/E") == 4
    assert portage_alt_shift_target(128.0, contract_line_type="D/E") == 4
    assert portage_alt_shift_target(64.0, contract_line_type="D/E") == 2


def test_mlt_de_pool_hours_weighted_targets() -> None:
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_de_evenings_per_catalog_hour,
        portage_pool_hours_weighted_alt_targets,
    )

    hours = [320.0] * 6 + [224.0, 160.0, 64.0]
    targets = portage_pool_hours_weighted_alt_targets(hours)
    assert portage_de_evenings_per_catalog_hour() == 0.025
    assert targets == (8, 8, 8, 8, 8, 8, 6, 4, 2)


def test_pool_weekend_targets_scale_down_when_pool_capacity_exceeded() -> None:
    from lab_scheduler.scheduling.portage_equity_targets import portage_pool_weekend_shift_targets

    targets = portage_pool_weekend_shift_targets(
        [320.0] * 6,
        qual_code="MLT",
        weekend_day_count=16,
    )
    assert targets == (6, 6, 6, 6, 4, 4)
    assert sum(targets) <= 32


def test_scale_weekend_ideals_from_stamped_counts() -> None:
    from lab_scheduler.scheduling.portage_equity_targets import scale_weekend_ideals_to_pool_capacity

    scaled = scale_weekend_ideals_to_pool_capacity(
        [8, 8, 8, 8, 8, 8],
        qual_code="MLT",
        weekend_day_count=16,
    )
    assert scaled == (6, 6, 6, 6, 4, 4)
    assert sum(scaled) <= 32

