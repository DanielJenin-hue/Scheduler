# Dead code and legacy inventory

> **See also:** [`SIMPLIFICATION_AUDIT.md`](SIMPLIFICATION_AUDIT.md) for the manual-first slimdown map (June 2026).
> Auto-generate / Auto-Pilot engine code now lives under `src/lab_scheduler/legacy/`.

Items listed here are **documented but not removed** per project decision (June 2026).
Review before any large refactor of `legacy/auto_generate.py`.

## Disabled functions (unreachable or no-op)

### `_realign_dn_night_assignments` — `auto_generate.py` ~3414

- **Status:** Disabled — `return 0` at line 3436; ~140 lines of swap logic below are unreachable
- **Reason:** 8-week master catalog now owns D/N night placement; pass fought manager cell locks
- **Call sites:** Still invoked at 4 locations; always receive `0`
- **Test:** `tests/test_master_rotation_night_lock.py::test_realign_dn_night_assignments_is_disabled`
- **Replacement:** Master catalog stamp + `_enforce_dn_fulltime_master_catalog`
- **Action when ready:** Delete dead body and call sites

### `_score_candidate` — `auto_generate.py`

- **Status:** Unused — zero callers in repository
- **Reason:** Comment says *"Legacy deficit score — retained for diagnostics"*
- **Replacement:** `cba_rank_key`, `equitability_score.score_line`
- **Action when ready:** Delete function

### `_post_generate_portage_equity_and_caps` — `auto_generate.py` ~10068

- **Status:** No-op under default `CLINICAL_AND_HOURS_FIRST` policy (early `return` at ~10095)
- **Reason:** Equity work moved to `_post_clinical_alt_equity_pass` before finalize
- **Call sites:** 5 invocations; do nothing on default policy
- **Replacement:** `_post_clinical_alt_equity_pass`, `_finalize_fulltime_peer_alt_parity`
- **Action when ready:** Remove function and call sites, or wire alternate policy path

### `_prepare_vacant_lines_for_cpsat_fill` — `auto_generate.py`

- **Status:** Side-effect-only; always returns `0`
- **Action when ready:** Change return type to `None`

## Orphan modules

### `engine/metrics.py`

- Re-exports `schedule_tallies` symbols
- **Zero importers** — use `lab_scheduler.scheduling.schedule_tallies` directly
- **Action when ready:** Delete shim

## Defined but unused configuration

### `STRICT_UNION_EXPORT` — `portage_equity_policy.py`

- Second scheduling policy alongside `CLINICAL_AND_HOURS_FIRST`
- Never selected in `app.py` or engine paths
- **Action when ready:** Wire to UI or remove

### `adaptive_auto_pilot_attempts` — `adaptive_auto_pilot.py`

- Legacy wrapper around `resolve_adaptive_attempts`
- Only referenced from tests
- **Replacement:** `resolve_adaptive_attempts` / `run_adaptive_auto_pilot_ladder`

## Experimental stacks (not dead — evaluate separately)

| Stack | Production use | Notes |
|-------|----------------|-------|
| `coverage_aggressor.py` | **Yes** — Auto-Pilot preview tier | Exports flagged stretches |
| `flat_availability.py` + `prompts/router_8h.py` | **No** — shadow test only | LLM scheduling not wired |
| `finance/penalty_score.py` | **No** — shadow test only | Gainshare scoring |
| `autonomous_patch_worker.py` | Ops-only | Sentry LLM patch daemon |
| `balance_advisor.py` | UI panel only | Soft tally swap suggestions |
| `deterministic_stamper.py` | **Yes** — TWELVE_HOUR archetype | Intentional separate path |

## Diagnostic scripts archived

Dated `_*.py` incident scripts were consolidated into `scripts/_audit_*.py` and removed from `scripts/archive/` (2026-06-19).
Kept at repo root of `scripts/`:

- `_compare_db_vs_generate.py`
- `_raw_generation_audit.py`

## Intentional layering (not redundant)

These look duplicated but gate different lifecycle stages — **do not merge without a design doc**:

- `compliance/engine.py` — statutory labor
- `audit/compliance.py` — union + Portage wrapper
- `persist_validation.py` — persist/export block
- `streak_validator.py` — work-streak export checks
- `night_streak_corrector.py` — consecutive night cap healing
