"""Audit weekend shift counts from breakroom HTML export."""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = Path.home() / "Downloads" / "breakroom_schedule_period-2026-summer.html"


def _period_days() -> list[date]:
    days: list[date] = []
    cursor = date(2026, 6, 1)
    end = date(2026, 7, 26)
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _cell_token(cell_html: str) -> str:
    match = re.search(r">([DEN\-])<", cell_html)
    if match:
        return match.group(1)
    if "&nbsp;" in cell_html or not cell_html.strip():
        return "-"
    return "?"


def audit_html(path: Path) -> None:
    html = path.read_text(encoding="utf-8")
    period_days = _period_days()
    saturdays = [day for day in period_days if day.weekday() == 5]

    for row_html in re.findall(r"<tr>.*?</tr>", html, re.DOTALL):
        if "emp-col" not in row_html or "tally-row" in row_html:
            continue
        name_match = re.search(r"class='emp-col'>([^<]+)", row_html)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        cells = re.findall(r"class='shift-cell[^']*'>(.*?)</td>", row_html, re.DOTALL)
        if len(cells) < len(period_days):
            continue

        vals = [_cell_token(cell) for cell in cells[: len(period_days)]]
        wk = {"D": 0, "E": 0, "N": 0}
        splits: list[str] = []
        paired_blocks = 0
        for saturday in saturdays:
            sunday = saturday + timedelta(days=1)
            if sunday > period_days[-1]:
                continue
            sat_t = vals[period_days.index(saturday)]
            sun_t = vals[period_days.index(sunday)]
            for token in (sat_t, sun_t):
                if token in wk:
                    wk[token] += 1
            if sat_t in "DEN" and sat_t == sun_t:
                paired_blocks += 1
            elif (sat_t in "DEN") != (sun_t in "DEN"):
                splits.append(f"{saturday.isoformat()} {sat_t}/{sun_t}")

        if "D/N" in name or wk["N"] > 0 or splits:
            total = sum(wk.values())
            print(
                f"{name}: weekend D={wk['D']} E={wk['E']} N={wk['N']} "
                f"total={total} paired={paired_blocks} splits={len(splits)}"
            )
            for split in splits[:6]:
                print(f"  split {split}")
            if len(splits) > 6:
                print(f"  ... +{len(splits) - 6} more splits")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_HTML
    if not target.is_file():
        print(f"Missing file: {target}")
        sys.exit(1)
    audit_html(target)
