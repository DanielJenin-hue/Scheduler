from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Union


DateLike = Union[date, datetime]


@dataclass(frozen=True, slots=True)
class WorkWeek:
    """
    A Monday-start work week.

    `start` is the Monday (inclusive).
    `end_exclusive` is the next Monday (exclusive).
    """

    start: date
    end_exclusive: date

    @property
    def end_inclusive(self) -> date:
        return self.end_exclusive - timedelta(days=1)

    def contains(self, d: DateLike) -> bool:
        dd = d.date() if isinstance(d, datetime) else d
        return self.start <= dd < self.end_exclusive


def workweek_for(d: DateLike) -> WorkWeek:
    """
    Return the Monday-start work week for the given date/datetime.

    Crucial constraint: the logic engine's standard work week starts on Monday.
    Python's `date.weekday()` matches this: Monday=0 ... Sunday=6.
    """

    dd = d.date() if isinstance(d, datetime) else d
    start = dd - timedelta(days=dd.weekday())
    end_exclusive = start + timedelta(days=7)
    return WorkWeek(start=start, end_exclusive=end_exclusive)

