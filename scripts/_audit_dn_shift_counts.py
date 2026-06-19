"""Count total/N/D/E shifts per D/N line in HTML export vs fresh generate."""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from lab_scheduler.scheduling.auto_generate import auto_generate_schedule
from portage_fixtures import portage_generate_kwargs


def day_tokens(body: str) -> list[str]:
    cells = re.findall(r"<td class='shift-cell[^']*'>(.*?)</td>", body, re.S)
    tokens: list[str] = []
    for cell in cells:
        match = re.search(r"print-token-([den])'[^>]*>([DEN])<", cell)
        tokens.append(match.group(2) if match else "-")
    return tokens


def summarize(label: str, name: str, tokens: list[str]) -> None:
    clipped = tokens[:56]
    total = sum(1 for token in clipped if token != "-")
    print(
        f"{label} {name}: total={total} N={clipped.count('N')} "
        f"D={clipped.count('D')} E={clipped.count('E')}"
    )


def main() -> None:
    path = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else Path.home() / "Downloads" / "breakroom_schedule_period-2026-summer.html"
    )
    html = path.read_text(encoding="utf-8")
    rows = re.findall(
        r"<tr><td class='emp-col'>(Vacant[^<]+)</td>(.*?)</tr>",
        html,
        re.S,
    )
    print(f"=== HTML: {path.name} ===")
    for name, body in rows:
        if "D/N" not in name:
            continue
        summarize("HTML", name.strip(), day_tokens(body))

    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = auto_generate_schedule(**kwargs)
    templates = kwargs["shift_templates"]
    code_map = {"MORNING": "D", "EVENING": "E", "NIGHT": "N"}
    print("=== FRESH HEADLESS GENERATE ===")
    for employee in sorted(kwargs["employees"], key=lambda row: row.full_name):
        if "D/N" not in employee.full_name:
            continue
        tokens: list[str] = []
        day = date(2026, 6, 1)
        while day <= date(2026, 7, 26):
            assignment = next(
                (
                    row
                    for row in result.assignments
                    if row.employee_id == employee.id and row.assignment_date == day
                ),
                None,
            )
            if assignment is None:
                tokens.append("-")
            else:
                tokens.append(code_map[templates[assignment.shift_template_id].code])
            day += timedelta(days=1)
        summarize("GEN", employee.full_name, tokens)


if __name__ == "__main__":
    main()
