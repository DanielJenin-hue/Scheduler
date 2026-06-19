"""Audit a breakroom HTML export against Portage D/N rules and catalog."""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_scheduler.scheduling.portage_template import (  # noqa: E402
    line_cycle_pattern,
    portage_master_line_spec,
)
from lab_scheduler.simulation.load_test import build_portage_roster  # noqa: E402


def main() -> None:
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
    def normalize_line_name(name: str) -> str:
        return re.sub(r"\s*\(\d+h\)\s*$", "", name.strip())

    dn_rows = [(normalize_line_name(name), body) for name, body in rows if "D/N" in name]

    def day_tokens(body: str) -> list[str]:
        cells = re.findall(
            r"<td class='shift-cell[^']*'>(.*?)</td>",
            body,
            re.S,
        )
        tokens: list[str] = []
        for cell in cells:
            match = re.search(r"print-token-([den])'[^>]*>([DEN])<", cell)
            tokens.append(match.group(2) if match else "-")
        return tokens

    dn_violations: list[str] = []
    evening_on_dn: list[str] = []
    for name, body in dn_rows:
        tokens = day_tokens(body)
        for i in range(1, min(len(tokens), len(dates))):
            if tokens[i - 1] == "D" and tokens[i] == "N":
                dn_violations.append(
                    f"{name}: {dates[i - 1].isoformat()} D -> {dates[i].isoformat()} N"
                )
        for i, tok in enumerate(tokens[: len(dates)]):
            if tok == "E":
                evening_on_dn.append(f"{name}: {dates[i].isoformat()} E")

    print(f"File: {path}")
    print(f"Period: {len(dates)} days ({period_start} .. {dates[-1]})")
    print(f"D/N lines: {len(dn_rows)}")
    print(f"D->N violations: {len(dn_violations)}")
    for item in dn_violations[:8]:
        print(f"  {item}")
    print(f"Evening on D/N: {len(evening_on_dn)}")
    for item in evening_on_dn[:5]:
        print(f"  {item}")

    emp_by_name = {e.full_name: e for e in build_portage_roster()}
    row_by_name = {name: body for name, body in dn_rows}

    def week_string(body: str, week_index: int) -> str:
        tokens = day_tokens(body)
        chunk = tokens[week_index * 7 : week_index * 7 + 7]
        if len(chunk) != 7:
            return "SHORT"
        return "".join(chunk)

    mismatches = 0
    for line in range(1, 5):
        for role in ("MLT", "MLA"):
            key = f"Vacant {role} D/N - Line {line:02d}"
            body = row_by_name.get(key)
            if body is None:
                print(f"Missing row: {key}")
                continue
            employee = emp_by_name.get(key)
            if employee is None or employee.fte < 0.99:
                continue
            spec = portage_master_line_spec(employee)
            if spec is None:
                continue
            cycle = line_cycle_pattern(spec)
            for week_index in range(4):
                expected = "".join("-" if t == "" else t for t in cycle[week_index])
                actual = week_string(body, week_index)
                if actual != expected:
                    mismatches += 1
                    if mismatches <= 12:
                        print(
                            f"Catalog W{week_index + 1} {key}: "
                            f"expected {expected} got {actual}"
                        )
    print(f"Catalog mismatches (weeks 1-4, 8 FT D/N lines): {mismatches}")


if __name__ == "__main__":
    main()
