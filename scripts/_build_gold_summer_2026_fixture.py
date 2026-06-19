"""Build authoritative summer-2026 fixture from June 8 manager screenshots."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "portage_manual_screenshot_summer_2026.json"

# Transcribed from Screenshot_2026-06-08_164637 (W1-W4) and _164642 (W5-W8).
# Week strings are Mon–Sun. MLA D/N mirrors MLT D/N.
GOLD: dict[str, list[str]] = {
    "portage-mlt-05": [
        "DDDDD--", "DDDDD--", "EEEEE--", "DDDDD--",
        "DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--",
    ],
    "portage-mlt-06": [
        "DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--",
        "EEE--EE", "----EE-", "EEE--EE", "----EE-",
    ],
    "portage-mlt-07": ["DDDDD--"] * 8,
    "portage-mlt-08": ["DDDDD--"] * 8,
    "portage-mlt-09": [
        "DDD--ED", "DD-EEED", "DDDDD--", "DDDDD--",
        "DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--",
    ],
    "portage-mlt-10": [
        "DDDDD--", "DDDDD--", "DD--EEE", "DD--EEE",
        "----EDD", "EEEE-DD", "----EDD", "EE---DD",
    ],
    "portage-mlt-11": [
        "-------", "-------", "-------", "-------",
        "------D", "-------", "------D", "-------",
    ],
    "portage-mlt-12": [
        "-------", "-------", "---D---", "-------",
        "-------", "-------", "-------", "-------",
    ],
    "portage-mlt-13": [
        "-----EE", "EEE----", "-----EE", "EEE----",
        "-------", "-------", "-------", "-------",
    ],
    "portage-mlt-01": [
        "DDDDDNN", "NNNDDNN", "NNNDDNN", "NNNDDNN",
        "NNNNN--", "DDDDN--", "NNNNN--", "DDDDN--",
    ],
    "portage-mlt-02": [
        "DDDDD--", "DDDDD--", "DDDDDNN", "NNNDDNN",
        "DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--",
    ],
    "portage-mlt-03": [
        "NNNNN--", "----NNN", "NNNNN--", "----NNN",
        "-----NN", "NNNNN--", "-----NN", "NNNNN--",
    ],
    "portage-mlt-04": [
        "-------", "-------", "NNNNN--", "----NNN",
        "NNNNN--", "-----NN", "NNNNN--", "-----NN",
    ],
    "portage-mla-01": [
        "EEEEE--", "EEEEE--", "EEEEE--", "EEEEE--",
        "DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--",
    ],
    "portage-mla-02": ["DDDDD--"] * 8,
    "portage-mla-03": ["DDDDD--"] * 8,
    "portage-mla-04": ["DDDDD--"] * 8,
    "portage-mla-05": [
        "DDDDD--", "DDDDDE-", "DDDDD--", "DDDDD--",
        "DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--",
    ],
    "portage-mla-10": ["DDDDD--"] * 8,
    "portage-mla-11": ["-------"] * 8,
    "portage-mla-12": [
        "-------", "-----DD", "DDD----", "-----DD",
        "DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--",
    ],
    "portage-mla-06": [
        "DDDDDNN", "NNNDDNN", "NNNDDNN", "NNNDDNN",
        "NNNNN--", "DDDDN--", "NNNNN--", "DDDDN--",
    ],
    "portage-mla-07": [
        "DDDDD--", "DDDDD--", "DDDDDNN", "NNNDDNN",
        "DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--",
    ],
    "portage-mla-08": [
        "NNNNN--", "----NNN", "NNNNN--", "----NNN",
        "-----NN", "NNNNN--", "-----NN", "NNNNN--",
    ],
    "portage-mla-09": [
        "-------", "-------", "NNNNN--", "----NNN",
        "NNNNN--", "-----NN", "NNNNN--", "-----NN",
    ],
}


def main() -> None:
    for employee_id, weeks in GOLD.items():
        if len(weeks) != 8:
            raise SystemExit(f"{employee_id}: expected 8 weeks")
        for index, week in enumerate(weeks, start=1):
            if len(week) != 7:
                raise SystemExit(f"{employee_id} W{index}: expected 7 days, got {week!r}")

    payload = {
        "archive_version": 1,
        "name": "portage-summer-2026-gold",
        "description": "Manual Portage summer 2026 schedule transcribed from June 8 manager screenshots.",
        "period_id": "period-2026-summer",
        "period_start": "2026-06-01",
        "period_end": "2026-07-26",
        "employees": GOLD,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
