from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.demand import infer_qual_code, roster_line_number
from lab_scheduler.scheduling.profiles import EmployeeProfile

ShiftToken = str  # "", "D", "E", "N" (Day/Evening/Night union tokens)
WeekPattern = Tuple[ShiftToken, ShiftToken, ShiftToken, ShiftToken, ShiftToken, ShiftToken, ShiftToken]
CyclePattern = Tuple[WeekPattern, ...]

PORTAGE_CYCLE_WEEKS = 8
# 1.0 FTE D/E full-time: 320h ÷ 8h/shift (same contract authority as D/N).
PORTAGE_DE_FT_PERIOD_WORK_SHIFTS = 40

TOKEN_TO_SHIFT_CODE: Dict[ShiftToken, str] = {
    "D": "MORNING",
    "M": "MORNING",
    "E": "EVENING",
    "N": "NIGHT",
}

VACANT_PORTAGE_LINE = re.compile(
    r"Vacant\s+(?P<role>MLT|MLA)\s+(?P<contract>D/E|D/N|M-F)\s*-\s*Line\s+(?P<line>\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PortageMasterLineSpec:
    role: str
    line_number: int
    contract_line_type: str
    target_fte: float
    cycle_pattern: CyclePattern
    week_offset: int = 0


def _week(*tokens: ShiftToken) -> WeekPattern:
    if len(tokens) != 7:
        raise ValueError("Week pattern requires exactly 7 day tokens (Mon–Sun)")
    return tokens  # type: ignore[return-value]


def _rotate_cycle(pattern: CyclePattern, offset: int) -> CyclePattern:
    offset = offset % len(pattern)
    if offset == 0:
        return pattern
    return pattern[offset:] + pattern[:offset]


def _mirror_weekend_days_in_pattern(pattern: CyclePattern) -> CyclePattern:
    """Sat and Sun in the same week use the same shift token (Sat anchor)."""

    weeks: List[List[ShiftToken]] = [list(week) for week in pattern]
    for week in weeks:
        saturday = week[5]
        sunday = week[6]
        if saturday and not sunday:
            week[6] = saturday
        elif sunday and not saturday:
            week[5] = sunday
        elif saturday and sunday and saturday != sunday:
            week[6] = saturday
    return tuple(tuple(week) for week in weeks)


def _de_catalog_work_shift_count(weeks: Sequence[Sequence[ShiftToken]]) -> int:
    return sum(1 for week in weeks for token in week if token)


def _trim_excess_de_catalog_tokens(
    weeks: List[List[ShiftToken]],
    *,
    target_work_shifts: int = PORTAGE_DE_FT_PERIOD_WORK_SHIFTS,
) -> None:
    """Drop weekday tokens from the tail until the 8-week grid matches 320h contract."""

    while _de_catalog_work_shift_count(weeks) > target_work_shifts:
        removed = False
        for week_index in range(PORTAGE_CYCLE_WEEKS - 1, -1, -1):
            for day_index in range(4, -1, -1):
                if not weeks[week_index][day_index]:
                    continue
                weeks[week_index][day_index] = ""
                removed = True
                break
            if removed:
                break
        if not removed:
            for week_index in range(PORTAGE_CYCLE_WEEKS - 1, -1, -1):
                if weeks[week_index][5] or weeks[week_index][6]:
                    weeks[week_index][5] = ""
                    weeks[week_index][6] = ""
                    removed = True
                    break
        if not removed:
            raise ValueError(
                f"unable to trim D/E catalog to {target_work_shifts} work shifts "
                f"(stuck at {_de_catalog_work_shift_count(weeks)})"
            )


def _cap_de_catalog_weekly_work(
    weeks: List[List[ShiftToken]],
    *,
    max_work_days: int = 5,
) -> None:
    """No calendar week may exceed 40h (five 8h shifts) on a full-time D/E line."""

    for week in weeks:
        while sum(1 for token in week if token) > max_work_days:
            if week[5] or week[6]:
                week[5] = ""
                week[6] = ""
                continue
            removed = False
            for day_index in range(4, -1, -1):
                if week[day_index]:
                    week[day_index] = ""
                    removed = True
                    break
            if not removed:
                break


def _fill_de_catalog_to_target(
    weeks: List[List[ShiftToken]],
    baseline_weeks: Sequence[Sequence[ShiftToken]],
    *,
    target_work_shifts: int = PORTAGE_DE_FT_PERIOD_WORK_SHIFTS,
) -> None:
    """Restore baseline weekday tokens when weekly caps left the period under 320h."""

    while _de_catalog_work_shift_count(weeks) < target_work_shifts:
        added = False
        for week_index in range(PORTAGE_CYCLE_WEEKS):
            for day_index in range(5):
                if _de_catalog_work_shift_count(weeks) >= target_work_shifts:
                    return
                if weeks[week_index][day_index]:
                    continue
                baseline_token = baseline_weeks[week_index][day_index]
                if not baseline_token:
                    continue
                if sum(1 for token in weeks[week_index] if token) >= 5:
                    continue
                weeks[week_index][day_index] = baseline_token
                added = True
        if not added:
            return


def _normalize_de_fulltime_cycle(cycle: CyclePattern) -> CyclePattern:
    """Full-time D/E lines must stamp exactly 40 shifts while keeping D→E blocks."""

    baseline_weeks = [list(week) for week in _mirror_weekend_days_in_pattern(cycle)]
    weeks = [list(week) for week in baseline_weeks]
    for _ in range(3):
        _cap_de_catalog_weekly_work(weeks)
        _trim_excess_de_catalog_tokens(weeks)
    _cap_de_catalog_weekly_work(weeks)
    _fill_de_catalog_to_target(weeks, baseline_weeks)
    _trim_excess_de_catalog_tokens(weeks)
    return tuple(tuple(week) for week in weeks)


def _inject_fulltime_weekends(
    pattern: CyclePattern,
    *,
    block_sat_tokens: Tuple[ShiftToken, ShiftToken],
    block_sun_tokens: Tuple[ShiftToken, ShiftToken] = ("", ""),
) -> CyclePattern:
    """
    Ensure each 4-week block includes two paired Sat+Sun weekends (four shift days per block).

    Full-time catalog lines target eight weekend shifts over the 8-week master rotation.
    """

    _ = block_sun_tokens  # legacy arg; Sat/Sun always mirror within the week now.
    weeks = [list(week) for week in pattern]
    target_pairs_per_block = 2
    for block_start in (0, 4):
        block = weeks[block_start : block_start + 4]
        existing_pairs = sum(1 for week in block if week[5] or week[6])
        inject_offsets = (1, 3)
        token_index = 0
        for inject_offset in inject_offsets:
            if existing_pairs >= target_pairs_per_block:
                break
            inject_at = block_start + inject_offset
            if inject_at >= len(weeks):
                continue
            if weeks[inject_at][5] or weeks[inject_at][6]:
                continue
            weekend_token = block_sat_tokens[token_index % len(block_sat_tokens)]
            weeks[inject_at][5] = weekend_token
            weeks[inject_at][6] = weekend_token
            existing_pairs += 1
            token_index += 1
    return _mirror_weekend_days_in_pattern(tuple(tuple(week) for week in weeks))


FULLTIME_FTE_THRESHOLD = 0.99

# Full-time vacant-line counts per role + contract group (Portage blueprint).
# MLT/MLA D/E full-time stagger pools are Lines 01–06 only; Lines 07+ are part-time tiers.
_FULLTIME_POOL_SIZES: Dict[Tuple[str, str], int] = {
    ("MLT", "D/N"): 4,
    ("MLT", "D/E"): 6,
    ("MLA", "D/E"): 6,
    ("MLA", "D/N"): 4,
}


def fulltime_week_offset(pool_index: int, pool_size: int) -> int:
    """Spread ``pool_index`` evenly across the 8-week master cycle."""

    if pool_size <= 1:
        return 0
    index = pool_index % pool_size
    return (index * PORTAGE_CYCLE_WEEKS) // pool_size


def dn_fulltime_week_offset(pool_index: int, pool_size: int) -> int:
    """Alternate D/N lines across D-block and N-block weeks (never all D-aligned)."""

    if pool_size <= 1:
        return 0
    index = pool_index % pool_size
    block_start = ((index // 2) * 4) % PORTAGE_CYCLE_WEEKS
    if index % 2 == 1:
        return (block_start + 1) % PORTAGE_CYCLE_WEEKS
    return block_start


def de_fulltime_week_offset(pool_index: int, pool_size: int) -> int:
    """Spread D/E lines across distinct D-block and E-block weeks (unique offsets)."""

    if pool_size <= 1:
        return 0
    index = pool_index % pool_size
    d_blocks = (0, 1, 4, 7)
    e_blocks = (2, 3, 5, 6)
    pair = index // 2
    if index % 2 == 0:
        return d_blocks[pair % len(d_blocks)]
    return e_blocks[pair % len(e_blocks)]


def de_fulltime_week_roll_offset(pool_index: int) -> int:
    """Simple 1-week roll: Line (pool_index+1) = Line 01 pattern shifted by pool_index weeks."""

    return pool_index % PORTAGE_CYCLE_WEEKS


def _fulltime_pool_week_offset(
    *,
    role: str,
    contract_line_type: str,
    pool_index: int,
) -> int:
    pool_size = _FULLTIME_POOL_SIZES.get((role.upper(), contract_line_type.upper()), 1)
    contract = contract_line_type.upper()
    if contract == "D/N" and pool_size > 1:
        return dn_fulltime_week_offset(pool_index, pool_size)
    if contract == "D/E" and pool_size > 1:
        return de_fulltime_week_roll_offset(pool_index)
    return fulltime_week_offset(pool_index, pool_size)


# Canonical 8-week master patterns (Line 1 baseline before symmetrical offset).
_MLT_DE_FULL: CyclePattern = _inject_fulltime_weekends(
    (
        _week("D", "D", "D", "D", "D", "", ""),
        _week("D", "D", "D", "D", "", "", ""),
        _week("", "", "E", "E", "E", "", ""),
        _week("E", "E", "E", "E", "", "", ""),
        _week("D", "D", "D", "D", "D", "", ""),
        _week("", "", "E", "E", "E", "", ""),
        _week("E", "E", "E", "E", "E", "", ""),
        _week("D", "D", "D", "D", "", "", ""),
    ),
    block_sat_tokens=("D", "E"),
    block_sun_tokens=("E", "D"),
)

_MLT_DE_PT: CyclePattern = (
    _week("D", "D", "D", "D", "", "", ""),
    _week("", "", "E", "E", "E", "E", ""),
    _week("D", "D", "D", "", "", "", ""),
    _week("", "E", "E", "E", "E", "", ""),
    _week("D", "D", "", "E", "E", "", ""),
    _week("", "", "E", "E", "E", "E", ""),
    _week("D", "D", "D", "D", "", "", ""),
    _week("", "", "", "E", "E", "E", "E"),
)

_MLT_DE_PT_LIGHT: CyclePattern = (
    _week("D", "D", "", "E", "E", "", ""),
    _week("", "E", "E", "", "", "", ""),
    _week("D", "", "E", "E", "", "", ""),
    _week("", "", "D", "E", "E", "", ""),
    _week("D", "E", "", "", "E", "", ""),
    _week("", "E", "D", "", "", "", ""),
    _week("", "", "E", "E", "", "", ""),
    _week("D", "", "", "E", "", "", ""),
)

# 0.2 FTE line 09: eight weekday shifts per 8-week cycle (~64h at 8h/shift).
_MLT_DE_PT_MINIMAL: CyclePattern = (
    _week("D", "", "", "", "", "", ""),
    _week("", "", "E", "", "", "", ""),
    _week("", "D", "", "", "", "", ""),
    _week("", "", "", "E", "", "", ""),
    _week("D", "", "", "", "", "", ""),
    _week("", "", "E", "", "", "", ""),
    _week("", "D", "", "", "", "", ""),
    _week("", "", "", "", "E", "", ""),
)


def _dn_fulltime_cycle_for_line(*, role: str, line: int) -> CyclePattern:
    """Screenshot-derived 8-week D/N pattern for a full-time vacant line."""

    from lab_scheduler.scheduling.portage_dn_reference import reference_cycle_for_line

    return reference_cycle_for_line(role=role, line=line)


def _dn_fulltime_baseline_cycle(role: str) -> CyclePattern:
    return _dn_fulltime_cycle_for_line(role=role, line=1)


_MLT_DN_PT: CyclePattern = (
    _week("D", "D", "D", "D", "", "", ""),
    _week("N", "N", "N", "N", "", "", ""),
    _week("D", "D", "", "N", "N", "", ""),
    _week("", "N", "N", "N", "", "", ""),
    _week("D", "D", "D", "", "", "", ""),
    _week("N", "N", "", "", "", "", ""),
    _week("", "D", "D", "N", "N", "", ""),
    _week("N", "", "", "N", "", "", ""),
)

_MLA_DE_FULL: CyclePattern = _inject_fulltime_weekends(
    (
        _week("D", "D", "D", "D", "D", "", ""),
        _week("", "", "E", "E", "E", "", ""),
        _week("D", "D", "D", "D", "D", "", ""),
        _week("E", "E", "E", "E", "", "", ""),
        _week("D", "D", "D", "D", "D", "", ""),
        _week("", "", "E", "E", "E", "", ""),
        _week("D", "D", "D", "D", "", "", ""),
        _week("E", "E", "E", "E", "E", "", ""),
    ),
    block_sat_tokens=("D", "E"),
    block_sun_tokens=("E", "D"),
)

_MLA_DE_PT: CyclePattern = (
    _week("D", "D", "D", "D", "", "", ""),
    _week("", "", "E", "E", "E", "", ""),
    _week("D", "D", "D", "", "", "", ""),
    _week("", "E", "E", "E", "E", "", ""),
    _week("D", "D", "", "E", "E", "", ""),
    _week("", "D", "D", "E", "E", "", ""),
    _week("D", "D", "D", "D", "", "", ""),
    _week("", "", "E", "E", "E", "E", ""),
)

_MLA_DE_PT_LIGHT: CyclePattern = (
    _week("D", "D", "D", "", "", "", ""),
    _week("", "", "E", "E", "", "", ""),
    _week("D", "D", "", "E", "", "", ""),
    _week("", "E", "E", "E", "", "", ""),
    _week("D", "", "E", "E", "", "", ""),
    _week("", "D", "E", "", "", "", ""),
    _week("D", "E", "", "", "", "", ""),
    _week("", "", "E", "D", "", "", ""),
)

_MLA_DN_PT: CyclePattern = (
    _week("D", "D", "D", "D", "D", "", ""),
    _week("", "", "", "", "N", "N", "N"),
    _week("D", "D", "D", "D", "", "", ""),
    _week("N", "N", "N", "", "", "", ""),
    _week("D", "D", "D", "", "N", "", ""),
    _week("", "D", "D", "N", "N", "", ""),
    _week("D", "D", "D", "D", "", "", ""),
    _week("", "", "N", "N", "N", "", ""),
)


def _build_mlt_line_specs() -> Dict[int, PortageMasterLineSpec]:
    """Catalog entries keyed by MLT D/E contract line number (01–09)."""

    specs: Dict[int, PortageMasterLineSpec] = {}
    for line in range(1, 7):
        specs[line] = PortageMasterLineSpec(
            "MLT", line, "D/E", 1.0, _MLT_DE_FULL, week_offset=(line - 1) % PORTAGE_CYCLE_WEEKS
        )
    specs[7] = PortageMasterLineSpec("MLT", 7, "D/E", 0.7, _MLT_DE_PT)
    specs[8] = PortageMasterLineSpec("MLT", 8, "D/E", 0.5, _MLT_DE_PT_LIGHT)
    specs[9] = PortageMasterLineSpec("MLT", 9, "D/E", 0.2, _MLT_DE_PT_MINIMAL)
    return specs


def _mlt_dn_catalog_spec(line_num: int) -> Optional[PortageMasterLineSpec]:
    if line_num < 1 or line_num > 4:
        return None
    return PortageMasterLineSpec(
        "MLT",
        line_num,
        "D/N",
        1.0,
        _dn_fulltime_cycle_for_line(role="MLT", line=line_num),
        week_offset=0,
    )


def _mla_dn_catalog_spec(line_num: int) -> Optional[PortageMasterLineSpec]:
    if line_num < 1 or line_num > 4:
        return None
    return PortageMasterLineSpec(
        "MLA",
        line_num,
        "D/N",
        1.0,
        _dn_fulltime_cycle_for_line(role="MLA", line=line_num),
        week_offset=0,
    )


def _build_mla_line_specs() -> Dict[int, PortageMasterLineSpec]:
    specs: Dict[int, PortageMasterLineSpec] = {}
    for line in range(1, 7):
        specs[line] = PortageMasterLineSpec(
            "MLA", line, "D/E", 1.0, _MLA_DE_FULL, week_offset=(line - 1) % PORTAGE_CYCLE_WEEKS
        )
    for line in range(7, 11):
        specs[line] = PortageMasterLineSpec("MLA", line, "D/E", 0.8, _MLA_DE_PT)
    for line in range(11, 13):
        specs[line] = PortageMasterLineSpec("MLA", line, "D/N", 0.6, _MLA_DN_PT)
    return specs


PORTAGE_MLT_LINE_SPECS: Dict[int, PortageMasterLineSpec] = _build_mlt_line_specs()
PORTAGE_MLA_LINE_SPECS: Dict[int, PortageMasterLineSpec] = _build_mla_line_specs()


def parse_vacant_portage_line(full_name: str) -> Optional[Tuple[str, str, int]]:
    match = VACANT_PORTAGE_LINE.search(full_name)
    if not match:
        return None
    return match.group("role").upper(), match.group("contract").upper(), int(match.group("line"))


def vacant_master_catalog_spec(profile: EmployeeProfile) -> Optional[PortageMasterLineSpec]:
    """Catalog master-line spec for a vacant Portage roster row (by line number)."""

    parsed = parse_vacant_portage_line(profile.full_name)
    if parsed is None:
        return None
    role, contract, line_num = parsed
    if role == "MLT":
        if contract == "D/N":
            return _mlt_dn_catalog_spec(line_num)
        return PORTAGE_MLT_LINE_SPECS.get(line_num)
    if contract == "D/N":
        return _mla_dn_catalog_spec(line_num)
    return PORTAGE_MLA_LINE_SPECS.get(line_num)


def _effective_vacant_rotation_fte(
    profile_fte: float,
    catalog: Optional[PortageMasterLineSpec],
) -> float:
    """Use the lower of catalog line tier and profile FTE for vacant master lines."""

    catalog_fte = catalog.target_fte if catalog is not None else profile_fte
    return min(catalog_fte, profile_fte) if profile_fte > 0 else catalog_fte


def _de_cycle_pattern_for_role_fte(
    role: str,
    fte: float,
    *,
    fallback: CyclePattern,
) -> CyclePattern:
    if fte >= FULLTIME_FTE_THRESHOLD:
        return fallback
    if role == "MLA":
        return _MLA_DE_PT if fte >= 0.6 else _MLA_DE_PT_LIGHT
    if fte >= 0.69:
        return _MLT_DE_PT
    if fte >= 0.49:
        return _MLT_DE_PT_LIGHT
    return _MLT_DE_PT_MINIMAL


def vacant_master_rotation_fte(profile: EmployeeProfile) -> Optional[float]:
    """FTE tier that governs master rotation for a vacant line (min of catalog and profile)."""

    catalog = vacant_master_catalog_spec(profile)
    if catalog is None:
        return None
    profile_fte = profile.fte if profile.fte else 1.0
    return _effective_vacant_rotation_fte(profile_fte, catalog)


def _mlt_de_catalog_line(fte: float, bucket_index: int) -> int:
    if fte >= 0.99:
        return (bucket_index % 6) + 1
    if fte >= 0.69:
        return 7
    if fte >= 0.49:
        return 8
    return 9


def _mla_de_catalog_line(fte: float, bucket_index: int) -> int:
    if fte >= 0.99:
        return (bucket_index % 6) + 1
    if fte >= 0.79:
        return (bucket_index % 4) + 7
    return (bucket_index % 6) + 1


def portage_pattern_for_bucket(
    *,
    role: str,
    contract_line_type: str,
    fte: float,
    bucket_index: int,
) -> PortageMasterLineSpec:
    """
    Resolve an 8-week rotation from pool bucket metadata — no line-name parsing.

    ``bucket_index`` drives both catalog pattern selection and cycle offset.
    """

    contract = (contract_line_type or "D/E").upper()
    role = role.upper()
    line_number = bucket_index + 1

    if contract == "D/N":
        if fte >= FULLTIME_FTE_THRESHOLD:
            cycle_pattern = _dn_fulltime_cycle_for_line(role=role, line=line_number)
            week_offset = 0
        elif role == "MLA":
            cycle_pattern = _MLA_DN_PT
            week_offset = bucket_index % PORTAGE_CYCLE_WEEKS
        else:
            cycle_pattern = _MLT_DN_PT
            week_offset = bucket_index % PORTAGE_CYCLE_WEEKS
        return PortageMasterLineSpec(
            role=role,
            line_number=line_number,
            contract_line_type=contract,
            target_fte=fte if fte else 1.0,
            cycle_pattern=cycle_pattern,
            week_offset=week_offset,
        )

    if role == "MLT":
        catalog_line = _mlt_de_catalog_line(fte, bucket_index)
        base = PORTAGE_MLT_LINE_SPECS[catalog_line]
    else:
        catalog_line = _mla_de_catalog_line(fte, bucket_index)
        base = PORTAGE_MLA_LINE_SPECS[catalog_line]

    week_offset = base.week_offset
    if fte >= FULLTIME_FTE_THRESHOLD and contract == "D/E":
        week_offset = _fulltime_pool_week_offset(
            role=role,
            contract_line_type=contract,
            pool_index=bucket_index,
        )

    return PortageMasterLineSpec(
        role=base.role,
        line_number=line_number,
        contract_line_type=contract,
        target_fte=fte if fte else base.target_fte,
        cycle_pattern=base.cycle_pattern,
        week_offset=week_offset,
    )


def portage_master_line_spec(profile: EmployeeProfile) -> Optional[PortageMasterLineSpec]:
    vacant = parse_vacant_portage_line(profile.full_name)
    if vacant is not None:
        role, contract, line_num = vacant
        profile_fte = profile.fte if profile.fte else 1.0
        catalog = vacant_master_catalog_spec(profile)
        rotation_fte = _effective_vacant_rotation_fte(profile_fte, catalog)
        pool_index = max(0, line_num - 1)
        if contract == "D/N":
            if rotation_fte >= FULLTIME_FTE_THRESHOLD:
                cycle_pattern = _dn_fulltime_cycle_for_line(role=role, line=line_num)
                week_offset = 0
            elif role == "MLA":
                cycle_pattern = _MLA_DN_PT
                week_offset = pool_index % PORTAGE_CYCLE_WEEKS
            else:
                cycle_pattern = _MLT_DN_PT
                week_offset = pool_index % PORTAGE_CYCLE_WEEKS
            return PortageMasterLineSpec(
                role=role,
                line_number=line_num,
                contract_line_type=contract,
                target_fte=rotation_fte,
                cycle_pattern=cycle_pattern,
                week_offset=week_offset,
            )

        base = catalog
        if base is None:
            return None
        cycle_pattern = _de_cycle_pattern_for_role_fte(
            role,
            rotation_fte,
            fallback=base.cycle_pattern,
        )
        week_offset = base.week_offset
        if rotation_fte >= FULLTIME_FTE_THRESHOLD:
            week_offset = _fulltime_pool_week_offset(
                role=role,
                contract_line_type=contract,
                pool_index=pool_index,
            )
        else:
            week_offset = pool_index % PORTAGE_CYCLE_WEEKS
        return PortageMasterLineSpec(
            role=base.role,
            line_number=line_num,
            contract_line_type=contract,
            target_fte=rotation_fte,
            cycle_pattern=cycle_pattern,
            week_offset=week_offset,
        )

    line_num = roster_line_number(profile)
    if line_num is None:
        return None

    role = infer_qual_code(profile)
    if role == "MLT":
        if line_num <= 4:
            dn_spec = _mlt_dn_catalog_spec(line_num)
            if dn_spec is not None:
                return dn_spec
        if line_num <= 13:
            de_line = line_num - 4
            base = PORTAGE_MLT_LINE_SPECS.get(de_line)
            if base is None:
                return None
        else:
            wrapped = ((line_num - 1) % 9) + 1
            base = PORTAGE_MLT_LINE_SPECS.get(wrapped)
            if base is None:
                return None
        return PortageMasterLineSpec(
            role=base.role,
            line_number=line_num,
            contract_line_type=profile.contract_line_type or base.contract_line_type,
            target_fte=base.target_fte,
            cycle_pattern=base.cycle_pattern,
            week_offset=base.week_offset,
        )
    if line_num <= 12:
        return PORTAGE_MLA_LINE_SPECS[line_num]
    wrapped = ((line_num - 1) % 12) + 1
    base = PORTAGE_MLA_LINE_SPECS[wrapped]
    return PortageMasterLineSpec(
        role=base.role,
        line_number=line_num,
        contract_line_type=profile.contract_line_type or base.contract_line_type,
        target_fte=base.target_fte,
        cycle_pattern=base.cycle_pattern,
        week_offset=base.week_offset,
    )


def line_cycle_pattern(spec: PortageMasterLineSpec) -> CyclePattern:
    rotated = _rotate_cycle(spec.cycle_pattern, spec.week_offset)
    if (
        spec.contract_line_type == "D/E"
        and spec.target_fte >= FULLTIME_FTE_THRESHOLD
    ):
        return _normalize_de_fulltime_cycle(rotated)
    return rotated


def rotated_cycle_is_one_week_roll(reference: PortageMasterLineSpec, other: PortageMasterLineSpec) -> bool:
    """True when ``other`` is ``reference``'s 8-week pattern shifted forward by one week."""

    reference_cycle = line_cycle_pattern(reference)
    other_cycle = line_cycle_pattern(other)
    if len(reference_cycle) != PORTAGE_CYCLE_WEEKS or len(other_cycle) != PORTAGE_CYCLE_WEEKS:
        return False
    return reference_cycle[1:] + reference_cycle[:1] == other_cycle


def shift_token_for_day(
    spec: PortageMasterLineSpec,
    *,
    week_index: int,
    day_of_week: int,
) -> ShiftToken:
    cycle = line_cycle_pattern(spec)
    return cycle[week_index % PORTAGE_CYCLE_WEEKS][day_of_week]




def _catalog_shift_token_for_date(
    spec: PortageMasterLineSpec,
    assignment_date: date,
    period_start: date,
) -> ShiftToken:
    week_index = max((assignment_date - period_start).days // 7, 0)
    return shift_token_for_day(
        spec,
        week_index=week_index,
        day_of_week=assignment_date.weekday(),
    )


def _employee_calendar_band_on_date(
    employee_id: str,
    assignment_date: date,
    assignments: Optional[Sequence[object]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Optional[str]:
    """Return D/E/N calendar band for an employee on a date, if assigned."""

    if not assignments:
        return None
    from lab_scheduler.engine.demand import _day_night_calendar_band

    for assignment in assignments:
        if getattr(assignment, "employee_id", None) != employee_id:
            continue
        if getattr(assignment, "assignment_date", None) != assignment_date:
            continue
        template = shift_templates.get(getattr(assignment, "shift_template_id", ""))
        if template is None:
            return None
        return _day_night_calendar_band(template.code)
    return None


def _catalog_token_blocks_day_night_transition(
    spec: PortageMasterLineSpec,
    assignment_date: date,
    period_start: date,
    token: ShiftToken,
    *,
    employee_id: Optional[str] = None,
    assignments: Optional[Sequence[object]] = None,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
) -> bool:
    """True when stamping `token` on `assignment_date` would create D→N."""

    if token not in {"D", "N"}:
        return False
    if token == "N":
        if employee_id and assignments and shift_templates:
            prev_band = _employee_calendar_band_on_date(
                employee_id,
                assignment_date - timedelta(days=1),
                assignments,
                shift_templates,
            )
            if prev_band == "D":
                return True
        prev_token = _catalog_shift_token_for_date(
            spec,
            assignment_date - timedelta(days=1),
            period_start,
        )
        return prev_token == "D"
    if employee_id and assignments and shift_templates:
        next_band = _employee_calendar_band_on_date(
            employee_id,
            assignment_date + timedelta(days=1),
            assignments,
            shift_templates,
        )
        if next_band == "N":
            return True
    next_token = _catalog_shift_token_for_date(
        spec,
        assignment_date + timedelta(days=1),
        period_start,
    )
    return next_token == "N"


def vacant_master_scheduled_shift_code(
    profile: EmployeeProfile,
    assignment_date: date,
    period_start: date,
    *,
    assignments: Optional[Sequence[object]] = None,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
) -> Optional[str]:
    """
    Shift band (MORNING/EVENING/NIGHT) from the 8-week master catalog for one day.

    Returns None when the line is not a vacant Portage master row or the catalog
    token is blank (scheduled off).
    """

    if parse_vacant_portage_line(profile.full_name) is None:
        return None
    spec = portage_master_line_spec(profile)
    if spec is None:
        return None
    token = _catalog_shift_token_for_date(spec, assignment_date, period_start)
    if not token:
        return None
    if _catalog_token_blocks_day_night_transition(
        spec,
        assignment_date,
        period_start,
        token,
        employee_id=profile.id,
        assignments=assignments,
        shift_templates=shift_templates,
    ):
        return None
    return TOKEN_TO_SHIFT_CODE.get(token)


def vacant_master_catalog_period_hours(
    profile: EmployeeProfile,
    period_start: date,
    period_end: date,
    *,
    shift_hours: float = 8.0,
) -> float:
    """Payroll target from stamped 8-week catalog work days (not nominal FTE × 320h)."""

    if parse_vacant_portage_line(profile.full_name) is None:
        return 0.0
    work_days = 0
    day = period_start
    while day <= period_end:
        if vacant_master_scheduled_shift_code(profile, day, period_start):
            work_days += 1
        day += timedelta(days=1)
    return work_days * shift_hours


def vacant_master_catalog_period_weekend_shifts(
    profile: EmployeeProfile,
    period_start: date,
    period_end: date,
) -> int:
    """Stamped Sat/Sun catalog work days in the period (each day counts once)."""

    if parse_vacant_portage_line(profile.full_name) is None:
        return 0
    weekend_days = 0
    day = period_start
    while day <= period_end:
        if day.weekday() >= 5 and vacant_master_scheduled_shift_code(
            profile, day, period_start
        ):
            weekend_days += 1
        day += timedelta(days=1)
    return weekend_days


def vacant_master_rotation_permits_shift(
    profile: EmployeeProfile,
    assignment_date: date,
    period_start: date,
    shift_code: str,
) -> bool:
    """True when a vacant master line's catalog rotation calls for ``shift_code``."""

    if parse_vacant_portage_line(profile.full_name) is None:
        return True
    expected = vacant_master_scheduled_shift_code(profile, assignment_date, period_start)
    if expected is None:
        return False
    return expected == shift_code


def portage_roster_sort_key(profile: Mapping[str, object]) -> Tuple[int, int, int, str]:
    """
    Order breakroom rows: MLT before MLA, then contract line, then line number.
    """

    full_name = str(profile.get("full_name") or profile.get("Employee") or "")
    emp_id = str(profile.get("id") or "")

    vacant = parse_vacant_portage_line(full_name)
    if vacant is not None:
        role, contract, line_num = vacant
    else:
        role = "MLT" if "mlt" in emp_id.lower() or full_name.upper().startswith("MLT") else "MLA"
        if full_name.upper().startswith("MLA"):
            role = "MLA"
        line_num = roster_line_number(
            EmployeeProfile(
                id=emp_id or "unknown",
                full_name=full_name,
                fte=1.0,
                qualification_ids=set(),
            )
        ) or 999
        contract = profile.get("contract_line_type") or "D/E"

    role_rank = 0 if role == "MLT" else 1
    contract_rank = {"D/E": 0, "D/N": 1, "M-F": 2}.get(str(contract).upper(), 3)
    return (role_rank, contract_rank, line_num, full_name.lower())
