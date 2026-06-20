# Portage alternate-shift rotation rules

Single reference for DE evening shape and footer invariants. Supersedes 5+2+1 / 6+2 heuristics.

## FT D/E evening shape (locked)

- **7 straight E shifts** in one calendar week (Mon–Sun) per FT D/E line
- **Stagger:** Line 01 → week 1, Line 02 → week 2, … within each qual pool (MLT / MLA)
- **8th E:** L1–4 get +1 from **stagger weekend E**; L5+ from weekday alt budget
- **7-day streak:** D/E evening blocks may use 7 consecutive work days (exception to default 6-day Portage cap)

## Footer (hard)

| Band | Target | Meaning |
|------|--------|---------|
| E | 2/2 every day | 1 MLT + 1 MLA on clinical floor |
| N | 2/2 every day | 1 MLT + 1 MLA |
| D weekdays | ~16 | Payroll / catalog fill |
| D weekends | 2/day | L5–8 D/E may carry weekend D when needed |

Weekday E/N placement uses **1 per qual per day** (`operational_alt_band_cap_per_qual`), not 2 per qual.

## Weekend stagger (unchanged)

| Line type | Weekend token |
|-----------|---------------|
| D/E L1–4 | E on stagger block |
| D/E L5–8 | D on stagger block |
| D/N | N only |

Operational reality: **4 weekend shift-days** per FT line (`FT_WEEKEND_SHIFT_DAYS`); catalog may label 8 aspirational.

## Weekend Sat/Sun mirror (hard)

Portage gold standard: **same employee, same D/E/N band on both Saturday and Sunday**, or neither day. If someone works Saturday evening, they work Sunday evening too — not a single-day orphan. D/N weekend nights follow the same mirror; `preference_fill` repairs split weekends after trim passes.

Legacy `auto_generate` enforces the same rule via `_apply_portage_weekend_pairing_policy`.

## D/N nights (frozen)

- 14 N per FT D/N line from `portage_dn_reference.py` / master catalog
- Do not add post-pass night gap repair without clean-grid verification

## Modules

| Module | Role |
|--------|------|
| `rotation_spec.py` | Declarative constants |
| `rotation_planner.py` | Pure 7-day E block planner |
| `rotation_applicator.py` | Apply planned shifts with caps |
| `rotation_invariants.py` | Pattern checks for RSI gate |

## RSI gate

```powershell
cd lab_staffing_scheduler
$env:PYTHONPATH="src;."
pytest tests/test_rotation_invariants.py tests/test_schedule_health.py tests/test_preference_fill.py tests/test_distribute_alternate_shifts.py -q
python scripts/rotation_rsi_gate.py
```

Tests run on **clean empty grid** only. Dirty UI drafts may diverge — use Schedule Health panel.
