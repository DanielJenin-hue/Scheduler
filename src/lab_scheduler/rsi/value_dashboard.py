from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from lab_scheduler.rsi.prospector import ViabilityReport


@dataclass(frozen=True, slots=True)
class ValueFirstDashboard:
    """
    Executive 'Living Asset' dashboard — three value-first metrics only.

    Refreshed daily by the RSI auto-manager.
    """

    updated_on: date
    operational_reliability_pct: float
    total_revenue_month_usd: float
    next_best_facility_target: Optional[str]

    def to_dict(self) -> dict:
        return {
            "updated_on": self.updated_on.isoformat(),
            "operational_reliability_pct": self.operational_reliability_pct,
            "total_revenue_month_usd": round(self.total_revenue_month_usd, 2),
            "next_best_facility_target": self.next_best_facility_target,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> ValueFirstDashboard:
        target = payload.get("next_best_facility_target")
        return cls(
            updated_on=date.fromisoformat(str(payload["updated_on"])),
            operational_reliability_pct=float(payload["operational_reliability_pct"]),
            total_revenue_month_usd=float(payload["total_revenue_month_usd"]),
            next_best_facility_target=str(target) if target else None,
        )


def build_value_first_dashboard(
    *,
    updated_on: date,
    operational_reliability_pct: float,
    total_revenue_month_usd: float,
    prospect_reports: Sequence[ViabilityReport],
) -> ValueFirstDashboard:
    next_target: Optional[str] = None
    if prospect_reports:
        top = max(
            prospect_reports,
            key=lambda report: (report.estimated_annual_savings_usd, report.deployment_score),
        )
        next_target = (
            f"{top.facility_name} ({top.region}) — "
            f"${top.estimated_annual_savings_usd:,.0f}/yr projected savings"
        )
    return ValueFirstDashboard(
        updated_on=updated_on,
        operational_reliability_pct=operational_reliability_pct,
        total_revenue_month_usd=total_revenue_month_usd,
        next_best_facility_target=next_target,
    )
