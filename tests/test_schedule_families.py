from datetime import date

from lab_scheduler.engine.constraints import CoverageTierResult
from lab_scheduler.scheduling.auto_generate import AutoGenerateResult, PlannedAssignment
from lab_scheduler.scheduling.auto_pilot import AutoPilotProof, AutoPilotRunResult
from lab_scheduler.scheduling.schedule_families import (
    ScheduleFamily,
    build_infeasibility_notes,
    is_portage_roster,
    resolve_schedule_family,
    validate_persist_gate,
)
from lab_scheduler.scheduling.strategies import ScheduleArchetype


def _pilot(*, coverage_complete: bool, gap_count: int = 0) -> AutoPilotRunResult:
    return AutoPilotRunResult(
        generate=AutoGenerateResult(
            coverage_tier_results=[
                CoverageTierResult(
                    tier_id="mlt-core",
                    label="MLT",
                    target_fte=13.0,
                    actual_fte=11.0,
                    gap_fte=2.0,
                    target_hours=4160.0,
                    actual_hours=3520.0,
                    period_target_hours=4160.0,
                    is_impossible=False,
                )
            ]
        ),
        proof=AutoPilotProof(
            block_start_monday=date(2026, 6, 1),
            week_count=8,
            lines_populated=25,
            slots_filled=800,
            slots_total=900,
            total_statutory_ot_hours=0.0,
            compliance_error_count=0,
            compliance_warning_count=0,
            coverage_complete=coverage_complete,
            coverage_success_rate_pct=100.0 if coverage_complete else 75.0,
            coverage_gap_count=gap_count if not coverage_complete else 0,
        ),
    )


def test_is_portage_roster_detects_vacant_lines() -> None:
    assert is_portage_roster([{"id": "portage-mlt-01", "full_name": "Vacant MLT D/E - Line 01"}])
    assert not is_portage_roster([{"id": "emp-a", "full_name": "Avery Miller"}])


def test_resolve_schedule_family_portage_vs_generic() -> None:
    portage = resolve_schedule_family(
        archetype=ScheduleArchetype.STANDARD.value,
        has_portage_coverage_targets=True,
        is_self_serve_trial=False,
    )
    assert portage.family is ScheduleFamily.PORTAGE_STANDARD
    assert portage.require_complete_for_success is True
    assert portage.allow_preview_tier is False

    generic = resolve_schedule_family(
        archetype=ScheduleArchetype.STANDARD.value,
        has_portage_coverage_targets=False,
        is_self_serve_trial=True,
    )
    assert generic.family is ScheduleFamily.GENERIC_LEGACY
    assert generic.allow_preview_tier is True


def test_validate_persist_gate_blocks_incomplete_portage() -> None:
    pilot = _pilot(coverage_complete=False, gap_count=12)
    result = validate_persist_gate(
        pilot,
        family=ScheduleFamily.PORTAGE_STANDARD,
        assignments=[],
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        template_id_to_band={"shift-evening": "E", "shift-night": "N"},
        allow_partial_persist=False,
    )
    assert not result.ok
    assert "blocked" in result.error_message.lower()
    assert result.infeasibility_notes


def test_validate_persist_gate_blocks_portage_tally_violations() -> None:
    pilot = _pilot(coverage_complete=True)
    assignments = [
        PlannedAssignment("a", "shift-evening", date(2026, 6, 1)),
    ]
    result = validate_persist_gate(
        pilot,
        family=ScheduleFamily.PORTAGE_STANDARD,
        assignments=assignments,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 1),
        template_id_to_band={"shift-evening": "E", "shift-night": "N"},
        allow_partial_persist=False,
    )
    assert not result.ok
    assert "exactly 2 per day" in result.error_message


def test_build_infeasibility_notes_includes_tier_deficit() -> None:
    pilot = _pilot(coverage_complete=False, gap_count=3)
    notes = build_infeasibility_notes(pilot)
    assert any("MLT" in note for note in notes)
