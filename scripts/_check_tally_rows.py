from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows
from lab_scheduler.simulation.load_test import build_portage_roster
from lab_scheduler.simulation.hospital_stress import shift_templates

# minimal smoke: empty assignments still produce tally rows
employees = [
    {
        "id": e.id,
        "full_name": e.full_name,
        "fte": e.fte,
        "contract_line_type": e.contract_line_type,
    }
    for e in build_portage_roster()[:3]
]
dates = [date(2026, 6, 1) + timedelta(days=i) for i in range(7)]
templates = {
    tid: {"id": tid, "code": t.code, "short": t.code[:1], "name": t.name}
    for tid, t in shift_templates().items()
}
rows = build_schedule_export_rows(
    employees, dates, [], templates, include_daily_tallies=True
)
print("rows", len(rows))
for row in rows:
    print(row.get("Employee"), row.get("employee_id"))
