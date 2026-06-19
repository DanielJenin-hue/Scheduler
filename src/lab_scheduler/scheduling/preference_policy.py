"""Tenant-configurable scheduling preference policy for preference-driven fill."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional, Tuple

from lab_scheduler.models.employee import normalize_contract_line_type

SCHEDULING_PREFERENCE_POLICY_KEY = "scheduling_preference_policy"


class SlotTier(str, Enum):
    WEEKEND_ALT = "weekend_alt"
    WEEKEND_DAY = "weekend_day"
    WEEKDAY_ALT = "weekday_alt"
    WEEKDAY_DAY = "weekday_day"


class FillMode(str, Enum):
    FULL = "full"
    WEEKEND_STAGGER_SLICE = "weekend_stagger_slice"
    ALTERNATE_SHIFTS = "alternate_shifts"


DEFAULT_TIER_ORDER: Tuple[SlotTier, ...] = (
    SlotTier.WEEKEND_ALT,
    SlotTier.WEEKEND_DAY,
    SlotTier.WEEKDAY_ALT,
)


@dataclass(frozen=True, slots=True)
class SchedulingPreferencePolicy:
    tier_order: Tuple[SlotTier, ...]
    version: int = 1


PORTAGE_DEFAULT_POLICY = SchedulingPreferencePolicy(tier_order=DEFAULT_TIER_ORDER)


def resolve_slot_tier(
    day: date,
    band: str,
    contract_line_type: object,
) -> Optional[SlotTier]:
    normalized_band = str(band or "").upper()
    if normalized_band not in {"D", "E", "N"}:
        return None
    contract = normalize_contract_line_type(str(contract_line_type or "")) or "D/E"
    is_weekend = day.weekday() >= 5
    alt_band = "N" if contract == "D/N" else "E"
    if is_weekend:
        if normalized_band == alt_band:
            return SlotTier.WEEKEND_ALT
        if normalized_band == "D":
            return SlotTier.WEEKEND_DAY
        return None
    if normalized_band == alt_band:
        return SlotTier.WEEKDAY_ALT
    if normalized_band == "D":
        return SlotTier.WEEKDAY_DAY
    return None


def policy_to_json(policy: SchedulingPreferencePolicy) -> str:
    payload = {
        "version": policy.version,
        "tier_order": [tier.value for tier in policy.tier_order],
    }
    return json.dumps(payload)


def policy_from_json(raw: str) -> SchedulingPreferencePolicy:
    data = json.loads(raw)
    tier_order = tuple(
        SlotTier(value) for value in data.get("tier_order", [])
    ) or DEFAULT_TIER_ORDER
    version = int(data.get("version", 1))
    return SchedulingPreferencePolicy(tier_order=tier_order, version=version)


def load_tenant_preference_policy(
    conn: sqlite3.Connection,
    tenant_id: str,
) -> SchedulingPreferencePolicy:
    from lab_scheduler.tenant.configuration import get_tenant_config_value

    raw = get_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=SCHEDULING_PREFERENCE_POLICY_KEY,
    )
    if not raw:
        return PORTAGE_DEFAULT_POLICY
    try:
        return policy_from_json(raw)
    except (json.JSONDecodeError, KeyError, ValueError):
        return PORTAGE_DEFAULT_POLICY


def save_tenant_preference_policy(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    policy: SchedulingPreferencePolicy,
) -> None:
    from lab_scheduler.tenant.configuration import set_tenant_config_value

    set_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=SCHEDULING_PREFERENCE_POLICY_KEY,
        config_value=policy_to_json(policy),
    )


def tiers_for_mode(mode: FillMode) -> Tuple[SlotTier, ...]:
    if mode == FillMode.WEEKEND_STAGGER_SLICE:
        return (SlotTier.WEEKEND_ALT, SlotTier.WEEKEND_DAY)
    if mode == FillMode.ALTERNATE_SHIFTS:
        return (SlotTier.WEEKDAY_ALT,)
    return DEFAULT_TIER_ORDER
