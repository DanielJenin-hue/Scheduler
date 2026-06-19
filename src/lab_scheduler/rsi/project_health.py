from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Sequence

PREMIUM_MRR_USD = 299.0


@dataclass(frozen=True, slots=True)
class ClinicalRiskInstance:
    """A shift covered via Forced Clinical OT or a hard-floor breach."""

    assignment_date: date
    shift_code: str
    employee_id: str
    risk_type: str
    detail: str
    assignment_id: Optional[str] = None


@dataclass
class ProjectHealthManifest:
    """
    RSI system boundary: recurring revenue and clinical-risk exposure.

    Tracks total Monthly Recurring Revenue (MRR) and every clinical-risk event
    (forced OT assignments plus immutable 2/2/2 floor breaches).
    """

    schema_version: str = "1.0"
    updated_at: str = ""
    total_mrr_usd: float = 0.0
    active_tenant_count: int = 0
    clinical_risk_instances: List[ClinicalRiskInstance] = field(default_factory=list)

    @property
    def forced_ot_count(self) -> int:
        return sum(1 for item in self.clinical_risk_instances if item.risk_type == "forced_clinical_ot")

    @property
    def floor_breach_count(self) -> int:
        return sum(1 for item in self.clinical_risk_instances if item.risk_type == "clinical_floor_breach")

    @property
    def total_clinical_risk_count(self) -> int:
        return len(self.clinical_risk_instances)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "total_mrr_usd": round(self.total_mrr_usd, 2),
            "active_tenant_count": self.active_tenant_count,
            "clinical_risk_instances": [
                {
                    "assignment_date": item.assignment_date.isoformat(),
                    "shift_code": item.shift_code,
                    "employee_id": item.employee_id,
                    "risk_type": item.risk_type,
                    "detail": item.detail,
                    "assignment_id": item.assignment_id,
                }
                for item in self.clinical_risk_instances
            ],
            "summary": {
                "forced_ot_count": self.forced_ot_count,
                "floor_breach_count": self.floor_breach_count,
                "total_clinical_risk_count": self.total_clinical_risk_count,
            },
        }

    @classmethod
    def from_dict(cls, payload: dict) -> ProjectHealthManifest:
        instances: List[ClinicalRiskInstance] = []
        for raw in payload.get("clinical_risk_instances", []):
            instances.append(
                ClinicalRiskInstance(
                    assignment_date=date.fromisoformat(str(raw["assignment_date"])),
                    shift_code=str(raw["shift_code"]),
                    employee_id=str(raw["employee_id"]),
                    risk_type=str(raw["risk_type"]),
                    detail=str(raw.get("detail", "")),
                    assignment_id=raw.get("assignment_id"),
                )
            )
        return cls(
            schema_version=str(payload.get("schema_version", "1.0")),
            updated_at=str(payload.get("updated_at", "")),
            total_mrr_usd=float(payload.get("total_mrr_usd", 0.0)),
            active_tenant_count=int(payload.get("active_tenant_count", 0)),
            clinical_risk_instances=instances,
        )


def compute_total_mrr(active_tenant_count: int, *, monthly_rate: float = PREMIUM_MRR_USD) -> float:
    return round(active_tenant_count * monthly_rate, 2)


def merge_clinical_risks(
    existing: Sequence[ClinicalRiskInstance],
    incoming: Sequence[ClinicalRiskInstance],
    *,
    max_entries: int = 500,
) -> List[ClinicalRiskInstance]:
    seen = {
        (
            item.assignment_date,
            item.shift_code,
            item.employee_id,
            item.risk_type,
            item.detail,
        )
        for item in existing
    }
    merged = list(existing)
    for item in incoming:
        key = (item.assignment_date, item.shift_code, item.employee_id, item.risk_type, item.detail)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    if len(merged) > max_entries:
        merged = merged[-max_entries:]
    return merged
