from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence

DEFAULT_HIGH_VOLUME_THRESHOLD = 750_000
HARD_LOCK_ANNUAL_SAVINGS_PER_TEST = 0.18


@dataclass(frozen=True, slots=True)
class RegionalFacilityRecord:
    facility_id: str
    facility_name: str
    region: str
    state_province: str
    annual_test_volume: int
    mlt_fte: float
    mla_fte: float


@dataclass(frozen=True, slots=True)
class ViabilityReport:
    facility_id: str
    facility_name: str
    region: str
    annual_test_volume: int
    estimated_annual_savings_usd: float
    deployment_score: float
    rationale: str

    def to_dict(self) -> dict:
        return {
            "facility_id": self.facility_id,
            "facility_name": self.facility_name,
            "region": self.region,
            "annual_test_volume": self.annual_test_volume,
            "estimated_annual_savings_usd": round(self.estimated_annual_savings_usd, 2),
            "deployment_score": round(self.deployment_score, 2),
            "rationale": self.rationale,
        }


def _parse_int(value: object, default: int = 0) -> int:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return default
    return int(float(text))


def _parse_float(value: object, default: float = 0.0) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return default
    return float(text)


def load_regional_facility_dataset(path: Path) -> List[RegionalFacilityRecord]:
    if not path.is_file():
        return []

    records: List[RegionalFacilityRecord] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            facility_id = str(row.get("facility_id") or row.get("id") or "").strip()
            name = str(row.get("facility_name") or row.get("name") or "").strip()
            if not facility_id or not name:
                continue
            records.append(
                RegionalFacilityRecord(
                    facility_id=facility_id,
                    facility_name=name,
                    region=str(row.get("region") or "").strip(),
                    state_province=str(row.get("state_province") or row.get("state") or "").strip(),
                    annual_test_volume=_parse_int(row.get("annual_test_volume")),
                    mlt_fte=_parse_float(row.get("mlt_fte")),
                    mla_fte=_parse_float(row.get("mla_fte")),
                )
            )
    return records


def estimate_hard_lock_annual_savings(
    *,
    annual_test_volume: int,
    mlt_fte: float,
    mla_fte: float,
    savings_per_test: float = HARD_LOCK_ANNUAL_SAVINGS_PER_TEST,
) -> float:
    """
    Model savings from deploying the immutable 2/2/2 hard-lock engine.

    Baseline assumes manual scheduling leakage scales with test volume and roster size.
    """

    roster_factor = 1.0 + (mlt_fte + mla_fte) * 0.04
    return annual_test_volume * savings_per_test * roster_factor


def build_viability_report(facility: RegionalFacilityRecord) -> ViabilityReport:
    savings = estimate_hard_lock_annual_savings(
        annual_test_volume=facility.annual_test_volume,
        mlt_fte=facility.mlt_fte,
        mla_fte=facility.mla_fte,
    )
    volume_score = min(100.0, facility.annual_test_volume / 10_000.0)
    roster_score = (facility.mlt_fte + facility.mla_fte) * 8.0
    deployment_score = volume_score + roster_score
    rationale = (
        f"High-volume lab ({facility.annual_test_volume:,} tests/yr) in {facility.region}. "
        f"Hard-lock 2/2/2 coverage projects ${savings:,.0f}/yr OT and gap-prevention savings."
    )
    return ViabilityReport(
        facility_id=facility.facility_id,
        facility_name=facility.facility_name,
        region=facility.region,
        annual_test_volume=facility.annual_test_volume,
        estimated_annual_savings_usd=savings,
        deployment_score=deployment_score,
        rationale=rationale,
    )


def run_prospector_scan(
    dataset_path: Path,
    *,
    high_volume_threshold: int = DEFAULT_HIGH_VOLUME_THRESHOLD,
    deployed_facility_ids: Optional[Sequence[str]] = None,
) -> List[ViabilityReport]:
    deployed = set(deployed_facility_ids or ())
    facilities = load_regional_facility_dataset(dataset_path)
    high_volume = [
        facility
        for facility in facilities
        if facility.annual_test_volume >= high_volume_threshold
        and facility.facility_id not in deployed
    ]
    reports = [build_viability_report(facility) for facility in high_volume]
    reports.sort(
        key=lambda report: (report.estimated_annual_savings_usd, report.annual_test_volume),
        reverse=True,
    )
    return reports


def select_next_best_facility_target(reports: Sequence[ViabilityReport]) -> Optional[ViabilityReport]:
    if not reports:
        return None
    return max(
        reports,
        key=lambda report: (report.estimated_annual_savings_usd, report.deployment_score),
    )
