# Publish Bundle — 2026-06-19

| Field | Value |
|-------|-------|
| Live URL | https://lablife.streamlit.app/ |
| RSI gate | **FAIL** — 5 operational tally violations, 16 rotation invariant violations (exit code 1) |
| Breakroom export | `exports/breakroom_schedule_period-2026-summer_9.html` |
| Operator | first-dollar-sprint (Cursor agent) |
| Notes | Demo roster; no PHI. **Do not cite RSI PASS in outbound until gate is green.** Fallback breakroom: `artifacts/breakroom_visual/breakroom_standard_ledger.html` |

## RSI stdout (excerpt)

```
Tier counts: {'master_catalog': 88, 'weekend_stagger': 27, 'seven_day_evening_blocks': 97, ...}
Operational tally violations: 5
  2026-06-05 N 1/2
  2026-06-06 N 1/2
  2026-06-07 N 1/2
  2026-07-11 N 1/2
  2026-07-12 N 1/2
Rotation invariant violations: 16
  [footer_night_2_2] 2026-06-05 night 1/2
  [footer_night_2_2] 2026-06-06 night 1/2
  ...
  [dn_ft_night_count] Vacant MLT D/N - Line 01: 13 N != 14
  [de_ft_evening_count] Vacant MLT D/E - Line 04: 7 E != target 8
  ...
Exit code: 1
```

## Smoke cross-ref

See `docs/FINISH_APP_ITERATIONS.md` → **First-Dollar Sprint — Live prod smoke (2026-06-19)** and **First-Dollar Sprint Scorecard — 2026-06-19**.

## Operator action before Batch 1 sends

1. Run `python scripts/rotation_rsi_gate.py` until **PASS** (0 violations).
2. Re-export breakroom HTML from live app (Distribute → Fill → Save → Export) or `python scripts/visual/snapshot_breakroom.py`.
3. Attach this bundle + breakroom HTML to pitch only after RSI PASS.
