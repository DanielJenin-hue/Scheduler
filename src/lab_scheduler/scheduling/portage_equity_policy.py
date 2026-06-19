"""Portage equity stabilization presets and scheduling policy profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal, Optional, Tuple


@dataclass(frozen=True, slots=True)
class PortageStabilizationOption:
    id: str
    title: str
    summary: str
    tradeoffs: str


@dataclass(frozen=True, slots=True)
class PortageSchedulingPolicy:
    """Ranked goals for Portage vacant-line Auto-Pilot when rules conflict."""

    id: str
    title: str
    summary: str
    compliance_first: bool
    weekend_mode: Literal["proportional", "catalog_ideal"]
    alt_equity_scope: Literal["all_peers", "ft_peers_only"]
    primary_objectives: Tuple[str, ...]
    stabilization_id: str


# Pick one primary lever when feasibility report shows hard conflicts.
STABILIZATION_OPTIONS: Tuple[PortageStabilizationOption, ...] = (
    PortageStabilizationOption(
        id="feasible_weekends_proportional",
        title="Proportional weekends (current default)",
        summary=(
            "Keep 8 weekend shifts as the FT catalog ideal but scale down per line "
            "when the qual weekend cap (2 MLT / 1 MLA per day) is exceeded."
        ),
        tradeoffs=(
            "FT MLT D/E lines land near ~4 weekend shifts each, not 8. "
            "Alt 20% and contract hours can still be pursued."
        ),
    ),
    PortageStabilizationOption(
        id="raise_mlt_weekend_cap",
        title="Raise MLT weekend cap to 3/day",
        summary="Change WEEKEND_CLINICAL_MAX_PER_QUAL['MLT'] from 2 to 3.",
        tradeoffs=(
            "Adds 16 MLT weekend slots per 8-week block (48 total vs 32). "
            "Still short of 48 demand for 6 FT lines at 8 each, but closer. "
            "Requires union/ops sign-off."
        ),
    ),
    PortageStabilizationOption(
        id="lower_ft_weekend_catalog",
        title="Lower FT weekend catalog to 4",
        summary="Set PORTAGE_FULLTIME_WEEKEND_SHIFTS = 4 to match current ops caps.",
        tradeoffs=(
            "Aligns catalog math with physical caps; removes false 'under-weekend' flags. "
            "Breaks from historical 8-weekend union language if that is contractual."
        ),
    ),
    PortageStabilizationOption(
        id="maximum_coverage_mode",
        title="Maximum Coverage (disable compliance-first)",
        summary="Run full finalize: exact 2E/2N daily, aggressive gap close.",
        tradeoffs=(
            "Better nightly/weekend clinical tallies; more union warnings. "
            "Use Break-Glass Auto-Pilot when persist blocks."
        ),
    ),
    PortageStabilizationOption(
        id="split_mla_weekend_cap",
        title="Raise MLA weekend cap to 2/day",
        summary="Change WEEKEND_CLINICAL_MAX_PER_QUAL['MLA'] from 1 to 2.",
        tradeoffs=(
            "Doubles MLA weekend pool (32 slots). Needed for 6 FT MLA D/E lines at 8 weekends. "
            "Conflicts with 'never 2 MLA' note in demand.py unless ops approves."
        ),
    ),
)

CLINICAL_AND_HOURS_FIRST = PortageSchedulingPolicy(
    id="clinical_and_hours_first",
    title="Clinical + hours first",
    summary=(
        "Fill 2 evening + 2 night seats daily, reach catalog hours per line, "
        "then even FT alternate shifts and proportional weekend share. "
        "Part-time lines may carry higher alt share as gap-fillers."
    ),
    compliance_first=False,
    weekend_mode="proportional",
    alt_equity_scope="ft_peers_only",
    primary_objectives=("clinical_2en", "catalog_hours"),
    stabilization_id="feasible_weekends_proportional",
)

STRICT_UNION_EXPORT = PortageSchedulingPolicy(
    id="strict_union_export",
    title="Strict union export (compliance-first)",
    summary=(
        "Prioritize union-clean persist and soft clinical caps. "
        "Skips aggressive post-CP-SAT healing; contract hours may remain under target."
    ),
    compliance_first=True,
    weekend_mode="proportional",
    alt_equity_scope="all_peers",
    primary_objectives=("union_clean",),
    stabilization_id="feasible_weekends_proportional",
)

PORTAGE_SCHEDULING_POLICIES: Dict[str, PortageSchedulingPolicy] = {
    CLINICAL_AND_HOURS_FIRST.id: CLINICAL_AND_HOURS_FIRST,
    STRICT_UNION_EXPORT.id: STRICT_UNION_EXPORT,
}

DEFAULT_PORTAGE_SCHEDULING_POLICY = CLINICAL_AND_HOURS_FIRST

# Per-line slider overrides (alt %, weekend count, hours) are intentionally deferred.
# Vacant master lines use blueprint equity roles (core_ft / gap_fill_pt / light_pt) and
# pool-scaled targets instead of 25× independent UI knobs. See portage_blueprint and
# build_portage_pool_budget_rows for the supported control surface.
# D/N night blocks are owned by the 8-week master stamp — post-solver equity adjusts
# D/E evenings and day-band weekend swaps only; it does not reshuffle frozen nights.


def resolve_portage_scheduling_policy(
    policy_id: Optional[str] = None,
) -> PortageSchedulingPolicy:
    if not policy_id:
        return DEFAULT_PORTAGE_SCHEDULING_POLICY
    return PORTAGE_SCHEDULING_POLICIES.get(policy_id, DEFAULT_PORTAGE_SCHEDULING_POLICY)
