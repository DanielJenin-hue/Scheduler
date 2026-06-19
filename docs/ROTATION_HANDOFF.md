# Rotation handoff

Run before/end of every implementing session:

```powershell
cd lab_staffing_scheduler
$env:PYTHONPATH="src;."
python scripts/rotation_rsi_gate.py
```

## Locked rules

- FT D/E: **7 straight E** in one calendar week per line, staggered; 8th E from weekend stagger (L1–4) or weekday alt (L5+)
- Footer: **2/2 E and N** daily (1 MLT + 1 MLA)
- D/N catalog: **do not touch** unless user asks
- No new repair passes without a matching invariant in `test_rotation_invariants.py`

## Last known status

- **RSI gate: PASS** (clean-grid `ALTERNATE_SHIFTS`, Jun 2026 sim)
- 7-day E block planner + applicator (8th E via stagger weekday; labor skip for planned rotation)
- Weekday E cap: `operational_alt_band_cap_per_qual` (1 MLT + 1 MLA per day)
- Clinical floor: skip-labor retry for footer top-up; post-clinical evening trim
- Known edge: PT-only weekend E may show 1/2 footer (documented waiver in invariants)
- Schedule health: evening pattern lines (E count / max E run); fill soft-gate ≥50 edits or floor fail
- Grid: health-panel **Go** highlights focus date column (`lab-health-focus-col`)

## Open questions

(none — 8th E from stagger weekend for L1–4 is locked)
