"""Prospect discovery for Manitoba hospital labs via RSI facility data."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from lab_scheduler.business.models import (
    Prospect,
    ProspectStatus,
    ensure_business_prospects_schema,
    serialize_pain_signals,
    utc_now_iso,
)
from lab_scheduler.rsi.prospector import (
    RegionalFacilityRecord,
    ViabilityReport,
    build_viability_report,
    load_regional_facility_dataset,
)

__all__ = [
    "DEFAULT_FACILITY_DATASET",
    "EXCLUDED_FACILITY_IDS",
    "DiscoveryResult",
    "compute_icp_score",
    "derive_pain_signals",
    "discover_manitoba_prospects",
    "purge_excluded_prospects",
    "score_facility_record",
]

DEFAULT_FACILITY_DATASET = (
    Path(__file__).resolve().parents[3] / "data" / "rsi" / "regional_facilities.csv"
)

MANITOBA_PROVINCE_CODES = frozenset({"MB", "MANITOBA"})

# Facilities the operator manages directly or has excluded from outbound GTM.
EXCLUDED_FACILITY_IDS = frozenset({"MB-WPG-PORTAGE"})


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    created: int
    updated: int
    skipped: int
    prospects: tuple[Prospect, ...]


def compute_icp_score(
    *,
    deployment_score: float,
    annual_test_volume: int,
    mlt_fte: float,
    mla_fte: float,
) -> int:
    """Normalize RSI deployment score and roster signals to 0–100 ICP."""

    volume_bonus = min(25.0, annual_test_volume / 40_000.0)
    roster_bonus = min(15.0, (mlt_fte + mla_fte) * 1.2)
    raw = deployment_score * 0.55 + volume_bonus + roster_bonus
    return int(round(min(100.0, max(0.0, raw))))


def derive_pain_signals(
    facility: RegionalFacilityRecord,
    report: ViabilityReport,
) -> List[str]:
    signals: list[str] = []
    roster_size = facility.mlt_fte + facility.mla_fte

    if facility.annual_test_volume >= 750_000:
        signals.append("High test volume increases scheduling leakage and OT risk")
    elif facility.annual_test_volume >= 400_000:
        signals.append("Mid-volume lab still juggling manual rotation spreadsheets")

    if roster_size >= 15:
        signals.append("Large MLT/MLA roster makes vacant-line fairness hard to track")
    elif roster_size >= 8:
        signals.append("Growing roster outpaces keeping a wall-ready schedule in Excel")

    if facility.state_province.upper() in MANITOBA_PROVINCE_CODES:
        signals.append("Manitoba union fatigue and rest rules require audit-ready schedules")

    if report.estimated_annual_savings_usd >= 50_000:
        signals.append(
            f"Projected ${report.estimated_annual_savings_usd:,.0f}/yr from coverage gap prevention"
        )

    signals.append(
        "Posting season still means weekends in Excel before staff see the schedule on the wall"
    )
    return signals


def score_facility_record(facility: RegionalFacilityRecord) -> tuple[ViabilityReport, int, List[str]]:
    report = build_viability_report(facility)
    icp = compute_icp_score(
        deployment_score=report.deployment_score,
        annual_test_volume=facility.annual_test_volume,
        mlt_fte=facility.mlt_fte,
        mla_fte=facility.mla_fte,
    )
    pain_signals = derive_pain_signals(facility, report)
    return report, icp, pain_signals


def _is_manitoba_facility(facility: RegionalFacilityRecord) -> bool:
    return facility.state_province.strip().upper() in MANITOBA_PROVINCE_CODES


def _is_excluded_facility(facility: RegionalFacilityRecord) -> bool:
    return facility.facility_id.strip().upper() in EXCLUDED_FACILITY_IDS


def purge_excluded_prospects(conn: sqlite3.Connection) -> int:
    """Remove excluded facilities from the persisted pipeline (e.g. Portage Regional)."""

    ensure_business_prospects_schema(conn)
    placeholders = ",".join("?" for _ in EXCLUDED_FACILITY_IDS)
    if not placeholders:
        return 0
    cursor = conn.execute(
        f"""
        DELETE FROM business_prospects
        WHERE facility_id IN ({placeholders})
           OR facility LIKE 'Portage Regional%'
        """,
        tuple(sorted(EXCLUDED_FACILITY_IDS)),
    )
    conn.commit()
    return int(cursor.rowcount)


def _fetch_existing_by_facility_id(
    conn: sqlite3.Connection,
    facility_id: str,
) -> Optional[Prospect]:
    row = conn.execute(
        "SELECT * FROM business_prospects WHERE facility_id = ?",
        (facility_id,),
    ).fetchone()
    if row is None:
        return None
    return Prospect.from_row(row)


def discover_manitoba_prospects(
    conn: sqlite3.Connection,
    *,
    dataset_path: Path | None = None,
    skip_existing: bool = False,
    min_icp_score: int = 0,
) -> DiscoveryResult:
    """Discover and score Manitoba hospital lab prospects from the RSI CSV dataset."""

    ensure_business_prospects_schema(conn)
    purge_excluded_prospects(conn)
    path = dataset_path or DEFAULT_FACILITY_DATASET
    facilities = [
        facility
        for facility in load_regional_facility_dataset(path)
        if _is_manitoba_facility(facility) and not _is_excluded_facility(facility)
    ]

    created = 0
    updated = 0
    skipped = 0
    saved: list[Prospect] = []

    for facility in facilities:
        report, icp_score, pain_signals = score_facility_record(facility)
        if icp_score < min_icp_score:
            skipped += 1
            continue

        existing = _fetch_existing_by_facility_id(conn, facility.facility_id)
        if existing is not None and skip_existing:
            skipped += 1
            continue

        now = utc_now_iso()
        notes = report.rationale
        if existing is None:
            prospect_id = f"prospect-{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO business_prospects (
                  id, facility_id, facility, province, icp_score,
                  pain_signals_json, status, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prospect_id,
                    facility.facility_id,
                    facility.facility_name,
                    facility.state_province.upper()[:2] if facility.state_province else "MB",
                    icp_score,
                    serialize_pain_signals(pain_signals),
                    ProspectStatus.DISCOVERED.value,
                    notes,
                    now,
                    now,
                ),
            )
            created += 1
            row = conn.execute(
                "SELECT * FROM business_prospects WHERE id = ?",
                (prospect_id,),
            ).fetchone()
        else:
            conn.execute(
                """
                UPDATE business_prospects
                SET facility = ?,
                    province = ?,
                    icp_score = ?,
                    pain_signals_json = ?,
                    notes = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    facility.facility_name,
                    facility.state_province.upper()[:2] if facility.state_province else "MB",
                    icp_score,
                    serialize_pain_signals(pain_signals),
                    notes,
                    now,
                    existing.id,
                ),
            )
            updated += 1
            row = conn.execute(
                "SELECT * FROM business_prospects WHERE id = ?",
                (existing.id,),
            ).fetchone()

        if row is not None:
            saved.append(Prospect.from_row(row))

    conn.commit()
    saved.sort(key=lambda prospect: (-prospect.icp_score, prospect.facility))
    return DiscoveryResult(
        created=created,
        updated=updated,
        skipped=skipped,
        prospects=tuple(saved),
    )


def list_scored_manitoba_facilities(
    dataset_path: Path | None = None,
) -> List[tuple[RegionalFacilityRecord, ViabilityReport, int, List[str]]]:
    """Score Manitoba facilities without persisting (preview / dry-run)."""

    path = dataset_path or DEFAULT_FACILITY_DATASET
    scored: list[tuple[RegionalFacilityRecord, ViabilityReport, int, List[str]]] = []
    for facility in load_regional_facility_dataset(path):
        if not _is_manitoba_facility(facility) or _is_excluded_facility(facility):
            continue
        report, icp, pain = score_facility_record(facility)
        scored.append((facility, report, icp, pain))
    scored.sort(key=lambda item: (-item[2], item[0].facility_name))
    return scored
