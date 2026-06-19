"""Tests for schedule generation strategy routing."""

from __future__ import annotations

from datetime import date

import pytest

from lab_scheduler.scheduling.auto_generate import auto_generate_schedule
from lab_scheduler.scheduling.strategies import (
    ScheduleArchetype,
    generate_schedule_for_archetype,
    normalize_archetype,
    schedule_archetype_display_label,
)

from portage_fixtures import portage_generate_kwargs


def test_normalize_archetype_standard_aliases():
    assert normalize_archetype("standard") is ScheduleArchetype.STANDARD
    assert normalize_archetype(ScheduleArchetype.STANDARD) is ScheduleArchetype.STANDARD


def test_normalize_archetype_twelve_hour_aliases():
    assert normalize_archetype("TWELVE_HOUR") is ScheduleArchetype.TWELVE_HOUR
    assert normalize_archetype("12h") is ScheduleArchetype.TWELVE_HOUR
    assert normalize_archetype("7on7off") is ScheduleArchetype.TWELVE_HOUR


def test_normalize_archetype_unknown_raises():
    with pytest.raises(ValueError, match="Unknown schedule archetype"):
        normalize_archetype("INVALID")


def test_auto_generate_schedule_defaults_to_standard():
    kwargs = portage_generate_kwargs()
    result = auto_generate_schedule(**kwargs)
    assert result.slots_filled > 0


def test_schedule_archetype_display_label_standard_default():
    assert schedule_archetype_display_label(ScheduleArchetype.STANDARD) == "Regular"


@pytest.mark.legacy
def test_auto_generate_twelve_hour_archetype_routes_to_strategy():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = auto_generate_schedule(**kwargs, archetype="TWELVE_HOUR")
    assert result.deterministic_status == "GENERATED"
    assert result.assignments
