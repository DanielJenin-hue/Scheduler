from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set


@dataclass(frozen=True, slots=True)
class EmployeeProfile:
    id: str
    full_name: str
    fte: float
    qualification_ids: Set[str]
    seniority_hours: float = 0.0
    base_hourly_rate: float = 40.0
    contract_line_type: Optional[str] = None
    modified_work_schedule: bool = False
