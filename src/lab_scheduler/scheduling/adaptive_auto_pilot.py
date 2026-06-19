"""Tiered Auto-Pilot solver ladder: strict → adaptive → optional trial preview."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from lab_scheduler.scheduling.auto_pilot import AutoPilotError, AutoPilotRunResult
from lab_scheduler.scheduling.schedule_families import ScheduleFamily


@dataclass(frozen=True, slots=True)
class AutoPilotSolverAttempt:
    tier: str
    coverage_aggressor_mode: bool
    strict_complete_block: bool
    require_master_compliance: bool


_PORTAGE_STRICT_ATTEMPT = AutoPilotSolverAttempt(
    tier="strict",
    coverage_aggressor_mode=False,
    strict_complete_block=True,
    require_master_compliance=True,
)

_PORTAGE_ADAPTIVE_ATTEMPT = AutoPilotSolverAttempt(
    tier="adaptive",
    coverage_aggressor_mode=False,
    strict_complete_block=False,
    require_master_compliance=True,
)

_PREVIEW_ATTEMPT = AutoPilotSolverAttempt(
    tier="preview",
    coverage_aggressor_mode=True,
    strict_complete_block=False,
    require_master_compliance=False,
)


def resolve_adaptive_attempts(
    *,
    family: ScheduleFamily,
    allow_preview_tier: bool,
) -> Tuple[AutoPilotSolverAttempt, ...]:
    """Build solver tiers for a schedule family (Portage: strict then adaptive)."""

    if family is ScheduleFamily.PORTAGE_STANDARD:
        attempts: list[AutoPilotSolverAttempt] = [
            _PORTAGE_STRICT_ATTEMPT,
            _PORTAGE_ADAPTIVE_ATTEMPT,
        ]
    else:
        attempts = [
            AutoPilotSolverAttempt(
                tier="strict",
                coverage_aggressor_mode=False,
                strict_complete_block=True,
                require_master_compliance=False,
            ),
            _PORTAGE_ADAPTIVE_ATTEMPT,
        ]
    if allow_preview_tier:
        attempts.append(_PREVIEW_ATTEMPT)
    return tuple(attempts)


def adaptive_auto_pilot_attempts(*, allow_preview_tier: bool) -> Tuple[AutoPilotSolverAttempt, ...]:
    """Legacy helper: generic strict → adaptive ladder (+ optional trial preview)."""

    return resolve_adaptive_attempts(
        family=ScheduleFamily.GENERIC_LEGACY,
        allow_preview_tier=allow_preview_tier,
    )


def run_adaptive_auto_pilot_ladder(
    run_block: Callable[..., AutoPilotRunResult],
    *,
    allow_preview_tier: bool,
    require_complete_for_success: bool,
    family: ScheduleFamily = ScheduleFamily.GENERIC_LEGACY,
    **run_kwargs: object,
) -> Tuple[AutoPilotRunResult, str, bool]:
    """
    Run solver tiers in order.

    Returns ``(pilot, tier_used, adaptive_rescue)`` where ``adaptive_rescue`` is
    True when tier ``adaptive`` produced the final result after an earlier tier failed
    or returned incomplete coverage.
    """

    attempts = resolve_adaptive_attempts(
        family=family,
        allow_preview_tier=allow_preview_tier,
    )
    single_portage_pass = False
    last_error: Optional[AutoPilotError] = None
    strict_failed = False
    fallback_pilot: Optional[AutoPilotRunResult] = None
    fallback_tier: Optional[str] = None

    for index, attempt in enumerate(attempts):
        is_last = index == len(attempts) - 1
        try:
            pilot = run_block(
                coverage_aggressor_mode=attempt.coverage_aggressor_mode,
                strict_complete_block=attempt.strict_complete_block,
                require_master_compliance=attempt.require_master_compliance,
                **run_kwargs,
            )
        except AutoPilotError as exc:
            last_error = exc
            if attempt.tier == "strict":
                strict_failed = True
            if is_last:
                raise
            continue

        if pilot.proof.coverage_complete:
            adaptive_rescue = (
                not single_portage_pass
                and attempt.tier == "adaptive"
                and (strict_failed or index > 0)
            )
            return pilot, attempt.tier, adaptive_rescue

        fallback_pilot = pilot
        fallback_tier = attempt.tier

        if require_complete_for_success:
            if is_last:
                adaptive_rescue = attempt.tier == "adaptive" and strict_failed
                return pilot, attempt.tier, adaptive_rescue
            continue

        if attempt.tier == "preview":
            return pilot, attempt.tier, False

        if is_last:
            return pilot, attempt.tier, attempt.tier == "adaptive" and strict_failed

    if fallback_pilot is not None and fallback_tier is not None:
        return (
            fallback_pilot,
            fallback_tier,
            fallback_tier == "adaptive" and strict_failed,
        )
    if last_error is not None:
        raise last_error
    raise AutoPilotError("Adaptive Auto-Pilot produced no result")
