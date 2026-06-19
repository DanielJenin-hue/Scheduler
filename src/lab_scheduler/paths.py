from __future__ import annotations

from pathlib import Path

EXPORTS_DIR_NAME = "exports"


def resolve_project_root(*, anchor: Path | None = None) -> Path:
    """
    Resolve the lab_staffing_scheduler project root as an absolute path.

    When ``anchor`` is omitted, infer from this package location
    (``src/lab_scheduler/paths.py`` → repo root).
    """

    if anchor is not None:
        path = anchor.resolve()
        if path.is_file():
            path = path.parent
        return path
    return Path(__file__).resolve().parents[2]


def resolve_project_path(project_root: Path, relative_or_absolute: str | Path) -> Path:
    """Resolve a project-relative or absolute path to a strict absolute path."""

    root = project_root.resolve()
    candidate = Path(relative_or_absolute)
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def exports_directory(project_root: Path) -> Path:
    return resolve_project_path(project_root, EXPORTS_DIR_NAME)
