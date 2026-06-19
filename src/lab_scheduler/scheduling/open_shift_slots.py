"""Operational shift gap detection for manual scheduling (no Auto-Pilot engine)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Sequence

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.scheduling.models import UnfilledSlot


def _daterange(start: date, end_inclusive: date) -> List[date]:
    days: List[date] = []
    current = start
    while current <= end_inclusive:
        days.append(current)
        current += timedelta(days=1)
    return days


def is_operational_shift_template(
    template: ShiftTemplateInfo,
    *,
    schedule_archetype: str = "STANDARD",
) -> bool:
    """Return False for templates that are not staffed coverage seats for the archetype."""

    code = str(template.code or "").strip().upper()
    if code.startswith("TOPUP"):
        return False
    template_id = str(template.id or "")
    if "twelve-hour-fte-topup" in template_id:
        return False
    normalized_archetype = str(schedule_archetype or "STANDARD").strip().upper().replace("-", "_")
    if normalized_archetype in {"TWELVE_HOUR", "TWELVEHOUR", "12H", "7ON7OFF"} and code in {
        "EVENING",
        "E",
    }:
        return False
    return True


def list_open_shift_slots(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Dict[str, ShiftTemplateInfo],
    assignments: Sequence[ScheduledShift],
    schedule_archetype: str = "STANDARD",
) -> List[UnfilledSlot]:
    """Slots with no coverage: one worker required per operational shift template per day."""

    covered = {(a.assignment_date, a.shift_template_id) for a in assignments}
    open_slots: List[UnfilledSlot] = []
    for d in _daterange(period_start, period_end):
        for shift_id, tmpl in shift_templates.items():
            if not is_operational_shift_template(tmpl, schedule_archetype=schedule_archetype):
                continue
            if (d, shift_id) in covered:
                continue
            open_slots.append(
                UnfilledSlot(
                    assignment_date=d,
                    shift_template_id=shift_id,
                    shift_code=tmpl.code,
                    reason="No coverage scheduled",
                )
            )
    return open_slots
