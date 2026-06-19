"""Audit weekend evening distribution in breakroom HTML vs D/E catalog."""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_scheduler.scheduling.portage_template import (  # noqa: E402
    vacant_master_rotation_permits_shift,
)
from lab_scheduler.simulation.load_test import build_portage_roster  # noqa: E402

path = Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else Path.home() / "Downloads" / "breakroom_schedule_period-2026-summer.html"
)
html = path.read_text(encoding="utf-8")
dates_raw = re.findall(
    r"<th class='day-col[^']*'>(\d+/\d+)<br>(Mon|Tue|Wed|Thu|Fri|Sat|Sun)</th>",
    html,
)
period_start = date(2026, 6, 1)
dates = [period_start + timedelta(days=i) for i in range(len(dates_raw))]

rows = re.findall(
    r"<tr><td class='emp-col'>(Vacant[^<]+)</td>(.*?)</tr>",
    html,
    re.S,
)


def day_tokens(body: str) -> list[str]:
    cells = re.findall(r"<td class='shift-cell[^']*'>(.*?)</td>", body, re.S)
    tokens: list[str] = []
    for cell in cells:
        match = re.search(r"print-token-([den])'[^>]*>([DEN])<", cell)
        tokens.append(match.group(2) if match else "-")
    return tokens


emp_by_name = {e.full_name: e for e in build_portage_roster()}

de_rows: list[tuple[str, list[str]]] = []
for name, body in rows:
    if "D/E" not in name:
        continue
    clean = re.sub(r"\s*\(\d+h\)\s*$", "", name.strip())
    de_rows.append((clean, day_tokens(body)))

weekend_e_actual: Counter[date] = Counter()
weekend_e_by_line: dict[date, list[str]] = defaultdict(list)
e_per_line = Counter()

for name, tokens in de_rows:
    short = name.split(" - ")[-1]
    for i, d in enumerate(dates):
        if i >= len(tokens):
            break
        if tokens[i] == "E":
            e_per_line[short] += 1
            if d.weekday() >= 5:
                weekend_e_actual[d] += 1
                weekend_e_by_line[d].append(short)

print(f"File: {path.name}")
print(f"Days: {len(dates)}")
print("\nWeekend E actual / needed (2):")
for d in dates:
    if d.weekday() < 5:
        continue
    print(f"  {d} ({dates_raw[(d - period_start).days][1]}): {weekend_e_actual[d]}/2")

print("\nWeekend E lines (first 6 weekends):")
for d in dates:
    if d.weekday() < 5:
        continue
    if weekend_e_actual[d]:
        print(f"  {d}: {weekend_e_by_line[d]}")

print("\nTotal E shifts per D/E line (all days):")
for line, count in sorted(e_per_line.items()):
    print(f"  {line}: {count}")

print("\nCatalog vs export mismatches on weekend E (FT D/E only):")
mism = 0
for name, tokens in de_rows:
    if "Line 0" not in name and "Line 1" not in name:
        continue
    emp = emp_by_name.get(name)
    if emp is None or emp.fte < 0.99:
        continue
    for i, d in enumerate(dates):
        if i >= len(tokens):
            break
        if d.weekday() < 5:
            continue
        cat_e = vacant_master_rotation_permits_shift(emp, d, period_start, "EVENING")
        exp = "E" if cat_e else "-"
        if tokens[i] != exp and (tokens[i] == "E" or exp == "E"):
            mism += 1
            if mism <= 15:
                print(f"  {name} {d}: catalog={exp} export={tokens[i]}")
print(f"  total weekend E mismatches: {mism}")
