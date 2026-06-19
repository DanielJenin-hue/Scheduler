"""Shared stdlib-backed calendar helpers for the scheduling package.

Consolidates the previously duplicated ``_daterange`` loops into a single
``datetime.timedelta``-based implementation so calendar math lives in one place.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List


def daterange(start: date, end_inclusive: date) -> List[date]:
    """Return every calendar day from ``start`` to ``end_inclusive`` (inclusive).

    Returns an empty list when ``end_inclusive`` precedes ``start``.
    """
    days: List[date] = []
    cursor = start
    while cursor <= end_inclusive:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days
