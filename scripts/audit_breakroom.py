"""Consolidated breakroom HTML audit entry point.

Subcommands delegate to ``scripts/_audit_*`` helpers. Usage::

    python scripts/audit_breakroom.py html [path/to/breakroom.html]
    python scripts/audit_breakroom.py weekends [path]
    python scripts/audit_breakroom.py tallies [path]
    python scripts/audit_breakroom.py dn-counts
    python scripts/audit_breakroom.py export-summary [path]
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent

COMMANDS = {
    "html": SCRIPTS / "_audit_breakroom_html.py",
    "weekends": SCRIPTS / "_audit_breakroom_weekends.py",
    "tallies": SCRIPTS / "_audit_tallies_html.py",
    "dn-counts": SCRIPTS / "_audit_dn_shift_counts.py",
    "export-summary": SCRIPTS / "_audit_export_summary.py",
}


def main(argv: list[str] | None = None) -> None:
    argv = list(argv or sys.argv[1:])
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        print("Commands:", ", ".join(sorted(COMMANDS)))
        raise SystemExit(0 if argv and argv[0] in {"-h", "--help"} else 1)

    command = argv[0]
    script = COMMANDS.get(command)
    if script is None:
        print(f"Unknown command: {command}", file=sys.stderr)
        print("Commands:", ", ".join(sorted(COMMANDS)), file=sys.stderr)
        raise SystemExit(2)

    sys.argv = [str(script), *argv[1:]]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
