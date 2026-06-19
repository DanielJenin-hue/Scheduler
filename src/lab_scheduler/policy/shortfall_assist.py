from __future__ import annotations

from datetime import date

from lab_scheduler.engine.swap_controller import (
    ScheduleState,
    SwapAssistCandidate,
    get_eligible_swap_candidates,
)
from lab_scheduler.policy.policy_engine import SHORTFALL_ASSIST_TARGET_ID


def get_shortfall_fill_candidates(
    schedule_state: ScheduleState,
    *,
    target_date: date,
    target_band: str,
    limit: int = 25,
) -> list[SwapAssistCandidate]:
    """
    Employees who can legally cover a shortfall on ``target_date`` for ``target_band``.

    Filters rest/contract rules via the swap controller and ranks by proximity to
    target hours (closest scheduled-to-target balance first).
    """

    candidates = get_eligible_swap_candidates(
        schedule_state,
        target_employee_id=SHORTFALL_ASSIST_TARGET_ID,
        target_date=target_date,
        target_shift_type=target_band,
        include_ineligible=False,
        limit=max(limit * 2, limit),
    )
    ranked = sorted(
        candidates,
        key=lambda candidate: abs(candidate.target_hours - candidate.scheduled_hours),
    )
    return ranked[:limit]
