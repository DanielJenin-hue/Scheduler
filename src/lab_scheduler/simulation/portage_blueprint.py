from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import QUAL_MLA, QUAL_MLT

# Exact Portage vacant-line blueprint (25 lines).
PORTAGE_MLT_LINE_COUNT = 13
PORTAGE_MLA_LINE_COUNT = 12
PORTAGE_ROSTER_SIZE = PORTAGE_MLT_LINE_COUNT + PORTAGE_MLA_LINE_COUNT

PortageEquityRole = Literal["core_ft", "gap_fill_pt", "light_pt"]
DEFAULT_EQUITY_ROLE: PortageEquityRole = "core_ft"


@dataclass(frozen=True, slots=True)
class PortageLineSpec:
    """One vacant line with contract-group line numbering (each group starts at Line 01)."""

    role: str
    contract_line_type: str
    fte: float
    line_number: int
    equity_role: PortageEquityRole = DEFAULT_EQUITY_ROLE


def _specs(
    role: str,
    contract: str,
    fte: float,
    count: int,
    *,
    start_line: int = 1,
    equity_role: PortageEquityRole = DEFAULT_EQUITY_ROLE,
) -> Tuple[PortageLineSpec, ...]:
    return tuple(
        PortageLineSpec(role, contract, fte, start_line + offset, equity_role=equity_role)
        for offset in range(count)
    )

# Canonical Portage blueprint — line numbers restart within each role + contract group.
PORTAGE_LINE_SPECS: Tuple[PortageLineSpec, ...] = (
    _specs("MLT", "D/N", 1.0, 4, equity_role="core_ft")
    + _specs("MLT", "D/E", 1.0, 6, equity_role="core_ft")
    + (
        PortageLineSpec("MLT", "D/E", 0.7, 7, equity_role="gap_fill_pt"),
        PortageLineSpec("MLT", "D/E", 0.5, 8, equity_role="gap_fill_pt"),
        PortageLineSpec("MLT", "D/E", 0.2, 9, equity_role="light_pt"),
    )
    + _specs("MLA", "D/E", 1.0, 5, equity_role="core_ft")
    + _specs("MLA", "D/N", 1.0, 4, equity_role="core_ft")
    + (
        PortageLineSpec("MLA", "D/E", 0.7, 6, equity_role="gap_fill_pt"),
        PortageLineSpec("MLA", "D/E", 0.6, 7, equity_role="gap_fill_pt"),
        PortageLineSpec("MLA", "D/E", 0.4, 8, equity_role="gap_fill_pt"),
    )
)

_PORTAGE_LINE_SPEC_INDEX: Dict[tuple[str, str, int], PortageLineSpec] = {
    (spec.role, spec.contract_line_type, spec.line_number): spec for spec in PORTAGE_LINE_SPECS
}
# Backward-compatible (role, contract, fte) tuples for tests and tallies.
PORTAGE_LINE_BLUEPRINT: Tuple[Tuple[str, str, float], ...] = tuple(
    (spec.role, spec.contract_line_type, spec.fte) for spec in PORTAGE_LINE_SPECS
)


def portage_vacant_line_name(spec: PortageLineSpec) -> str:
    return f"Vacant {spec.role} {spec.contract_line_type} - Line {spec.line_number:02d}"


def portage_line_spec_for_vacant_name(full_name: str) -> Optional[PortageLineSpec]:
    """Resolve blueprint row for a vacant Portage line label."""

    parsed = parse_vacant_portage_line(full_name)
    if parsed is None:
        return None
    role, contract, line_number = parsed
    return _PORTAGE_LINE_SPEC_INDEX.get((role, contract, line_number))


def portage_equity_role_for_employee(employee: EmployeeProfile) -> PortageEquityRole | None:
    spec = portage_line_spec_for_vacant_name(employee.full_name)
    return spec.equity_role if spec is not None else None

def build_portage_blueprint_roster() -> List[EmployeeProfile]:
    """Build the canonical 25-line Portage roster from the lab blueprint."""

    roster: List[EmployeeProfile] = []
    mlt_seq = 0
    mla_seq = 0
    for spec in PORTAGE_LINE_SPECS:
        if spec.role == "MLT":
            mlt_seq += 1
            seq = mlt_seq
            qual = {QUAL_MLT}
            base_rate = 40.0
            seniority = round(12000.0 - (seq - 1) * 420.0, 1)
            employee_id = f"portage-mlt-{seq:02d}"
        else:
            mla_seq += 1
            seq = mla_seq
            qual = {QUAL_MLA}
            base_rate = 26.0
            seniority = round(8200.0 - (seq - 1) * 360.0, 1)
            employee_id = f"portage-mla-{seq:02d}"

        roster.append(
            EmployeeProfile(
                id=employee_id,
                full_name=portage_vacant_line_name(spec),
                fte=spec.fte,
                qualification_ids=qual,
                seniority_hours=seniority,
                base_hourly_rate=base_rate,
                contract_line_type=spec.contract_line_type,
            )
        )

    assert len(roster) == PORTAGE_ROSTER_SIZE
    assert mlt_seq == PORTAGE_MLT_LINE_COUNT
    assert mla_seq == PORTAGE_MLA_LINE_COUNT
    return roster
