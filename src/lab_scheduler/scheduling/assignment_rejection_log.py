from __future__ import annotations

import logging
import os
import sys
from datetime import date
from typing import Optional, Union

logger = logging.getLogger(__name__)


def scheduling_trace_enabled() -> bool:
    """True when verbose scheduling trace (stdout rejections) is enabled."""

    return os.environ.get("LAB_SCHEDULING_TRACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _emit_rejection_line(line: str) -> None:
    """Best-effort terminal trace; never raise from logging (Streamlit-safe on Windows)."""

    logger.debug(line)
    try:
        sys.stdout.write(f"{line}\n")
        flush = getattr(sys.stdout, "flush", None)
        if callable(flush):
            flush()
    except OSError:
        # Streamlit/Uvicorn on Windows can reject flushed stdout writes (Errno 22).
        pass


def emit_scheduling_trace(message: str) -> None:
    """Best-effort diagnostic line for generation; safe under Streamlit on Windows."""

    _emit_rejection_line(message)


def log_assignment_rejection(
    employee_id: str,
    assignment_date: Optional[Union[date, str]],
    reason: str,
) -> None:
    """Emit a single-line terminal trace for a blocked assignment attempt."""

    if assignment_date is None:
        date_label = "unknown-date"
    elif isinstance(assignment_date, date):
        date_label = assignment_date.isoformat()
    else:
        date_label = str(assignment_date)
    emp_label = employee_id or "unknown-employee"
    _emit_rejection_line(f"REJECTED: {emp_label} on {date_label} due to {reason}")
