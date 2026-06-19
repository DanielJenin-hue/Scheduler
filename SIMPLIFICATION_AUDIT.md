# Manual-First Simplification Audit

Generated as part of the Manual-First Scheduler Slimdown (June 2026).

## Top modules by lines of code

| LOC | Path | Manual app needs? |
|-----|------|-------------------|
| 20438 | `src/lab_scheduler/scheduling/auto_generate.py` | **No** — legacy Auto-Pilot engine |
| 14875 | `scripts/app.py` | **Yes** — UI (target: slim down) |
| 2404 | `src/lab_scheduler/solver/cpsat_fill.py` | No |
| 1278 | `src/lab_scheduler/scheduling/breakroom_print.py` | **Yes** — wall chart export |
| 925 | `src/lab_scheduler/scheduling/portage_template.py` | Partial — roster sort only |
| 774 | `src/lab_scheduler/scheduling/persist_validation.py` | **Yes** — export gate only |
| 747 | `src/lab_scheduler/scheduling/auto_pilot.py` | **No** — legacy |
| 682 | `src/lab_scheduler/scheduling/schedule_export.py` | **Yes** — export rows |
| 242 | `src/lab_scheduler/data/schedule_archive.py` | **Yes** — save/load JSON |
| 101 | `src/lab_scheduler/data/snapshots.py` | **Yes** — restore safety |

## `app.py` import graph

### Keep (manual path)

- `lab_scheduler.data.schedule_archive` — save/load
- `lab_scheduler.data.snapshots` — restore points
- `lab_scheduler.scheduling.breakroom_print` — Print tab
- `lab_scheduler.scheduling.schedule_export` — export rows
- `lab_scheduler.scheduling.persist_validation` — export readiness (not publish)
- `lab_scheduler.scheduling.shift_cell_locks` — optional locks
- `lab_scheduler.policy` — cell edit validation
- `lab_scheduler.scheduling.auto_generate` — **subset only**: `EmployeeProfile`, `PlannedAssignment`, `validate_assignment_change`
- `lab_scheduler.scheduling.portage_template.portage_roster_sort_key`
- `lab_scheduler.data` — roster import

### Remove from runtime UI

- `lab_scheduler.scheduling.portage_ui_autopilot`
- `lab_scheduler.scheduling.auto_pilot`
- `lab_scheduler.scheduling.shift_run_summary.compute_auto_pilot_shift_summary`
- `lab_scheduler.scheduling.deterministic_stamper` (12h Auto-Pilot path)
- `auto_generate_schedule`, `run_portage_auto_pilot_ladder`, `persist_auto_pilot_schedule`

## Redundancies removed

| Item | Location | Action |
|------|----------|--------|
| Duplicate `_is_self_serve_trial` | `app.py` ~410–415 | Remove duplicate |
| Auto-Pilot preview overlay | session keys + UI | Removed |
| `_run_auto_generate_and_persist` wrapper | `app.py` | Removed |
| Onboarding Auto-Pilot wizard | `app.py` | Skipped in manager mode |
| Trial/signup gate on manager entry | `app.py` | Auto-login + skip onboarding |
| Break-glass Auto-Pilot | `app.py` | Removed |
| Advanced Auto-Pilot expander | ribbon | Removed |

## Script consolidation map

| Keep | Archive |
|------|---------|
| `scripts/audit_breakroom.py` (new unified) | `_audit_tallies_html.py`, `_audit_breakroom_weekends.py`, `_audit_dn_shift_counts.py`, `audit_breakroom_html.py` |
| `_build_gold_summer_2026_fixture.py` | `_build_screenshot_fixture.py` |
| `_import_manual_summer_2026.py` | — |
| `_compare_gold_fixture.py` | `_compare_snap_grid.py`, `_verify_visible_grid.py` |
| `_extract_schedule_from_snap.py` | — |
| — | `compile_period.py`, `_check_compile_rows.py`, `_compare_db_vs_generate.py`, `_raw_generation_audit.py` |

## Test impact

Tests marked `@pytest.mark.legacy` (excluded from default `pytest` run):

- `test_auto_pilot*.py`, `test_auto_generate*.py`
- `test_adaptive_auto_pilot.py`, `test_portage_dn_screenshot_reference.py`
- `test_coverage_aggressor.py`, `test_cpsat_fill.py` (partial — engine internals)
- Most tests importing `auto_generate_schedule` directly

Default test suite (manual path):

- `test_schedule_archive.py`
- `test_shift_cell_locks.py`, `test_display_labels.py`
- `test_persist_validation.py` (export gates)
- Roster import tests

## Target session state (manual)

**Keep:** `schedule_draft_*`, `schedule_baseline_*`, `schedule_sync_*`, `schedule_pending_mutations_*`, `.last_schedule_import`

**Removed:** `auto_pilot_preview_assignments_*`, `auto_pilot_show_failed_preview_*`, `auto_pilot_shift_summary`, `trigger_auto_pilot_*`

## Publish vs export

- **Publish:** always writes staged grid edits to DB (no persist_validation block)
- **Breakroom export:** blocked when `SchedulePostingReadiness.is_ready` is false
