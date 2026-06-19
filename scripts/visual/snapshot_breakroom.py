"""Visual snapshot renderer for the breakroom printout (dev-only).

Renders the self-contained breakroom HTML through headless Chromium with print
media emulation, capturing a PNG screenshot and a print-fidelity PDF into
``artifacts/breakroom_visual/``. This lets the layout be graded visually by
``score_layout.py`` without a human eyeballing every regen.

Playwright is an OPTIONAL dependency (the ``viz`` extra). The core library stays
zero-dependency; this script lives under ``scripts/`` and is never imported by
``lab_scheduler``. If Chromium / Playwright is not provisioned, the script prints
the exact install steps and exits non-zero instead of crashing.

    pip install -e .[viz]
    python -m playwright install chromium
    python scripts/visual/snapshot_breakroom.py --paper legal --archetype STANDARD

Run:  python scripts/visual/snapshot_breakroom.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Importing the compliance package first resolves a known load-order circular
# import between contract_payroll and the compliance engine.
import lab_scheduler.compliance  # noqa: F401,E402
from lab_scheduler.scheduling.breakroom_print import (  # noqa: E402
    generate_breakroom_print_html,
)

ARTIFACT_DIR = ROOT / "artifacts" / "breakroom_visual"
PLAYWRIGHT_HINT = (
    "Playwright/Chromium is not available. Install the optional viz extra:\n"
    "    pip install -e .[viz]\n"
    "    python -m playwright install chromium\n"
)


def build_sample_html(*, archetype: str, paper_size: str) -> str:
    """Build a representative 2-week breakroom sheet with deliberate gaps.

    Self-contained (no DB) so the renderer is reproducible anywhere. For STANDARD
    we inject a few true coverage gaps so the dedicated Coverage Gaps row and the
    open-shift hatch are both exercised in the snapshot.
    """
    period_start = date(2026, 6, 1)
    dates = [period_start + timedelta(days=i) for i in range(14)]

    employees = [
        {"id": "emp-mlt-01", "full_name": "A. Okonkwo MLT", "fte": 1.0, "contract_line_type": "D/E"},
        {"id": "emp-mlt-02", "full_name": "B. Tremblay MLT", "fte": 1.0, "contract_line_type": "D/N"},
        {"id": "emp-mla-01", "full_name": "C. Nakamura MLA", "fte": 0.6, "contract_line_type": "M-F"},
        {"id": "emp-mla-02", "full_name": "D. Singh MLA", "fte": 0.8, "contract_line_type": "D/E"},
    ]
    patterns = {
        "emp-mlt-01": ["D", "D", "E", "", "D", "E", "", "D", "D", "E", "", "D", "E", ""],
        "emp-mlt-02": ["N", "N", "", "D", "N", "", "D", "N", "N", "", "D", "N", "", "D"],
        "emp-mla-01": ["D", "D", "D", "D", "D", "", "", "D", "D", "D", "D", "D", "", ""],
        "emp-mla-02": ["E", "", "E", "E", "", "D", "", "E", "", "E", "E", "", "D", ""],
    }
    schedule_rows = []
    for emp in employees:
        row = {
            "Employee": emp["full_name"],
            "employee_id": emp["id"],
            "fte": emp["fte"],
            "contract_line_type": emp["contract_line_type"],
        }
        for day, tok in zip(dates, patterns[emp["id"]]):
            row[day.isoformat()] = tok
        schedule_rows.append(row)

    # Demonstrate the recommended Option A representation: per-day true open seats.
    coverage_gaps_by_day = None
    if archetype.upper().startswith("STANDARD"):
        coverage_gaps_by_day = {
            dates[2]: 1,
            dates[5]: 2,
            dates[9]: 1,
        }

    return generate_breakroom_print_html(
        facility_name="Northstar Medical Laboratory",
        period_name=f"Visual Snapshot ({archetype} / {paper_size})",
        period_start=dates[0],
        period_end=dates[-1],
        week_count=2,
        employees=employees,
        dates=dates,
        schedule_rows=schedule_rows,
        schedule_archetype=archetype,
        coverage_gaps_by_day=coverage_gaps_by_day,
        paper_size=paper_size,
    )


def render_with_playwright(html: str, *, stem: str, paper_size: str) -> dict[str, Path]:
    """Render HTML to PNG + PDF using Chromium with print-media emulation."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(PLAYWRIGHT_HINT) from exc

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = ARTIFACT_DIR / f"{stem}.html"
    png_path = ARTIFACT_DIR / f"{stem}.png"
    pdf_path = ARTIFACT_DIR / f"{stem}.pdf"
    html_path.write_text(html, encoding="utf-8")

    pdf_format = {"legal": "Legal", "ledger": "Tabloid", "letter": "Letter"}.get(
        paper_size.lower(), "Legal"
    )

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment dependent
            raise SystemExit(PLAYWRIGHT_HINT) from exc
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.emulate_media(media="print")
        page.screenshot(path=str(png_path), full_page=True)
        page.pdf(path=str(pdf_path), format=pdf_format, landscape=True, print_background=True)
        browser.close()

    return {"html": html_path, "png": png_path, "pdf": pdf_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot the breakroom printout for visual grading.")
    parser.add_argument("--paper", default="legal", choices=["legal", "ledger", "letter"])
    parser.add_argument("--archetype", default="STANDARD")
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="Write only the HTML (skip Chromium); useful where no browser is provisioned.",
    )
    args = parser.parse_args(argv)

    html = build_sample_html(archetype=args.archetype, paper_size=args.paper)
    stem = f"breakroom_{args.archetype.lower()}_{args.paper}"

    if args.html_only:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        html_path = ARTIFACT_DIR / f"{stem}.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"Wrote {html_path} (html-only mode)")
        return 0

    outputs = render_with_playwright(html, stem=stem, paper_size=args.paper)
    for label, path in outputs.items():
        print(f"Wrote {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
