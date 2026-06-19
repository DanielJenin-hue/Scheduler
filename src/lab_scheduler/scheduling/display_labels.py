from __future__ import annotations

from typing import Dict, Mapping

from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

_SHIFT_TEMPLATE_LABELS = {
    "shift-morning": "Morning Shift (D)",
    "shift-evening": "Evening Shift (E)",
    "shift-night": "Night Shift (N)",
}

_CODE_LABELS = {
    "MORNING": "Morning Shift",
    "EVENING": "Evening Shift",
    "NIGHT": "Night Shift",
}


def staff_line_display_name(full_name: str) -> str:
    """Compact roster label for UI selectboxes (e.g. MLA D/E Line 01)."""

    parsed = parse_vacant_portage_line(full_name)
    if parsed is not None:
        role, contract, line_number = parsed
        return f"{role} {contract} Line {line_number:02d}"
    trimmed = full_name.strip()
    if trimmed.endswith("h)"):
        paren = trimmed.rfind("(")
        if paren > 0:
            trimmed = trimmed[:paren].strip()
    return trimmed


def shift_template_display_name(
    template_id: str,
    templates: Mapping[str, object],
    *,
    template_short_by_id: Mapping[str, str] | None = None,
) -> str:
    """Human-readable shift label for assignment cards."""

    if template_id in _SHIFT_TEMPLATE_LABELS:
        return _SHIFT_TEMPLATE_LABELS[template_id]
    template = templates.get(template_id, {})
    if not isinstance(template, dict):
        template = {}
    code = str(template.get("code", "")).strip().upper()
    if template_short_by_id is not None:
        band = template_short_by_id.get(template_id, "?")
    else:
        short = str(template.get("short", template.get("code", ""))[:1]).upper()
        band = short or "?"
    label = _CODE_LABELS.get(code, str(template.get("name") or code.title() or "Shift"))
    return f"{label} ({band})"
