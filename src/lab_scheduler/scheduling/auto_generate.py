"""Compatibility shim — prefer open_shift_slots / assignment_validation for manual UI."""

from __future__ import annotations

import importlib
from typing import Any

from lab_scheduler.scheduling.assignment_validation import validate_assignment_change
from lab_scheduler.scheduling.open_shift_slots import (
    is_operational_shift_template,
    list_open_shift_slots,
)

_LEGACY = "lab_scheduler.legacy.auto_generate"


def __getattr__(name: str) -> Any:
    legacy = importlib.import_module(_LEGACY)
    return getattr(legacy, name)


def __dir__() -> list[str]:
    legacy = importlib.import_module(_LEGACY)
    return sorted(
        {
            "validate_assignment_change",
            "list_open_shift_slots",
            "is_operational_shift_template",
            *dir(legacy),
        }
    )
