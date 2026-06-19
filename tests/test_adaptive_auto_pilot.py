"""Tests for tiered adaptive Auto-Pilot ladder."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy

from datetime import date

from lab_scheduler.scheduling.adaptive_auto_pilot import (
    adaptive_auto_pilot_attempts,
    resolve_adaptive_attempts,
    run_adaptive_auto_pilot_ladder,
)
from lab_scheduler.scheduling.auto_pilot import AutoPilotError, AutoPilotProof, AutoPilotRunResult
from lab_scheduler.scheduling.auto_generate import AutoGenerateResult
from lab_scheduler.scheduling.schedule_families import ScheduleFamily


def _proof(*, coverage_complete: bool, gap_count: int = 0) -> AutoPilotProof:
    rate = 100.0 if coverage_complete else 75.0
    return AutoPilotProof(
        block_start_monday=date(2026, 6, 1),
        week_count=8,
        lines_populated=25,
        slots_filled=900 if coverage_complete else 675,
        slots_total=900,
        total_statutory_ot_hours=0.0,
        compliance_error_count=0,
        compliance_warning_count=0,
        coverage_complete=coverage_complete,
        coverage_success_rate_pct=rate,
        coverage_gap_count=gap_count if not coverage_complete else 0,
    )


def _pilot(*, coverage_complete: bool, gap_count: int = 56) -> AutoPilotRunResult:
    return AutoPilotRunResult(
        generate=AutoGenerateResult(),
        proof=_proof(coverage_complete=coverage_complete, gap_count=gap_count),
    )


def test_adaptive_attempts_generic_legacy_has_two_tiers() -> None:
    attempts = adaptive_auto_pilot_attempts(allow_preview_tier=False)
    assert len(attempts) == 2
    assert attempts[0].tier == "strict"
    assert attempts[1].tier == "adaptive"
    assert attempts[1].require_master_compliance is True
    assert attempts[1].strict_complete_block is False


def test_portage_premium_resolves_strict_then_adaptive() -> None:
    attempts = resolve_adaptive_attempts(
        family=ScheduleFamily.PORTAGE_STANDARD,
        allow_preview_tier=False,
    )
    assert len(attempts) == 2
    assert attempts[0].tier == "strict"
    assert attempts[0].coverage_aggressor_mode is False
    assert attempts[1].tier == "adaptive"
    assert attempts[1].coverage_aggressor_mode is False
    assert attempts[1].strict_complete_block is False
    assert attempts[1].require_master_compliance is True


def test_portage_trial_includes_preview_after_adaptive() -> None:
    attempts = resolve_adaptive_attempts(
        family=ScheduleFamily.PORTAGE_STANDARD,
        allow_preview_tier=True,
    )
    assert len(attempts) == 3
    assert attempts[0].tier == "strict"
    assert attempts[1].tier == "adaptive"
    assert attempts[2].tier == "preview"


def test_adaptive_attempts_trial_includes_preview_tier() -> None:
    attempts = adaptive_auto_pilot_attempts(allow_preview_tier=True)
    assert len(attempts) == 3
    assert attempts[2].tier == "preview"
    assert attempts[2].strict_complete_block is False


def test_portage_ladder_tries_strict_before_adaptive() -> None:
    calls: list[str] = []

    def run_block(**kwargs: object) -> AutoPilotRunResult:
        if kwargs.get("strict_complete_block"):
            tier = "strict"
        elif kwargs.get("coverage_aggressor_mode"):
            tier = "aggressor"
        else:
            tier = "adaptive"
        calls.append(tier)
        if tier == "strict":
            raise AutoPilotError("strict blocked")
        return _pilot(coverage_complete=True)

    pilot, tier_used, adaptive_rescue = run_adaptive_auto_pilot_ladder(
        run_block,
        allow_preview_tier=False,
        require_complete_for_success=True,
        family=ScheduleFamily.PORTAGE_STANDARD,
    )
    assert pilot.proof.coverage_complete
    assert tier_used == "adaptive"
    assert adaptive_rescue is True
    assert calls == ["strict", "adaptive"]


def test_premium_ladder_skips_preview_and_uses_adaptive_when_strict_fails() -> None:
    calls: list[str] = []

    def run_block(**kwargs: object) -> AutoPilotRunResult:
        if kwargs.get("strict_complete_block"):
            tier = "strict"
        elif kwargs.get("coverage_aggressor_mode"):
            tier = "aggressor"
        else:
            tier = "adaptive"
        calls.append(tier)
        if tier == "strict":
            raise AutoPilotError("strict blocked")
        return _pilot(coverage_complete=True)

    pilot, tier_used, adaptive_rescue = run_adaptive_auto_pilot_ladder(
        run_block,
        allow_preview_tier=False,
        require_complete_for_success=True,
    )
    assert pilot.proof.coverage_complete
    assert tier_used == "adaptive"
    assert adaptive_rescue is True
    assert calls == ["strict", "adaptive"]


def test_premium_ladder_rejects_incomplete_adaptive_without_persist() -> None:
    def run_block(**kwargs: object) -> AutoPilotRunResult:
        if kwargs.get("strict_complete_block"):
            raise AutoPilotError("strict blocked")
        return _pilot(coverage_complete=False)

    pilot, tier_used, _ = run_adaptive_auto_pilot_ladder(
        run_block,
        allow_preview_tier=False,
        require_complete_for_success=True,
    )
    assert tier_used == "adaptive"
    assert not pilot.proof.coverage_complete


def test_trial_ladder_falls_through_to_preview_partial() -> None:
    def run_block(**kwargs: object) -> AutoPilotRunResult:
        if kwargs.get("strict_complete_block"):
            return _pilot(coverage_complete=False)
        return _pilot(coverage_complete=False, gap_count=56)

    pilot, tier_used, adaptive_rescue = run_adaptive_auto_pilot_ladder(
        run_block,
        allow_preview_tier=True,
        require_complete_for_success=False,
    )
    assert tier_used == "preview"
    assert not pilot.proof.coverage_complete
    assert adaptive_rescue is False
