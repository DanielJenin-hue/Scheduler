from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


class SnapshotError(Exception):
    """Raised when a snapshot cannot be created or restored."""


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    path: Path
    filename: str
    label: str
    recorded_at_utc: str
    size_bytes: int


def default_snapshots_dir(project_root: Optional[Path] = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[3]
    return root / "snapshots"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", label.strip()).strip("-")
    return cleaned[:64] or "snapshot"


def create_snapshot(
    db_path: Path,
    *,
    label: str,
    snapshots_dir: Optional[Path] = None,
) -> Path:
    """Copy the live SQLite database into /snapshots/ with a UTC timestamp."""

    source = Path(db_path)
    if not source.is_file():
        raise SnapshotError(f"Database file not found: {source}")

    target_dir = snapshots_dir or default_snapshots_dir(source.parent)
    target_dir.mkdir(parents=True, exist_ok=True)

    recorded_at = _utc_stamp()
    stamp = recorded_at.replace(":", "").replace("-", "")
    safe = _safe_label(label)
    filename = f"{stamp}_{safe}.sqlite3"
    destination = target_dir / filename

    shutil.copy2(source, destination)
    meta = {
        "label": label,
        "recorded_at_utc": recorded_at,
        "source_db": str(source.resolve()),
        "filename": filename,
    }
    destination.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return destination


def list_recent_snapshots(
    snapshots_dir: Optional[Path] = None,
    *,
    project_root: Optional[Path] = None,
    limit: int = 5,
) -> List[SnapshotInfo]:
    directory = snapshots_dir or default_snapshots_dir(project_root)
    if not directory.is_dir():
        return []

    entries: List[SnapshotInfo] = []
    for db_file in sorted(directory.glob("*.sqlite3"), reverse=True):
        meta_path = db_file.with_suffix(".json")
        label = db_file.stem
        recorded_at = datetime.fromtimestamp(db_file.stat().st_mtime, tz=timezone.utc).isoformat()
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                label = str(meta.get("label", label))
                recorded_at = str(meta.get("recorded_at_utc", recorded_at))
            except json.JSONDecodeError:
                pass
        entries.append(
            SnapshotInfo(
                path=db_file,
                filename=db_file.name,
                label=label,
                recorded_at_utc=recorded_at,
                size_bytes=db_file.stat().st_size,
            )
        )
        if len(entries) >= limit:
            break
    return entries


def restore_snapshot(
    db_path: Path,
    snapshot_path: Path,
    *,
    snapshots_dir: Optional[Path] = None,
) -> None:
    """Replace the live database file with a snapshot copy."""

    live = Path(db_path)
    snapshot = Path(snapshot_path)
    if not snapshot.is_file():
        raise SnapshotError(f"Snapshot not found: {snapshot}")

    if live.is_file():
        create_snapshot(
            live,
            label="pre-restore-automatic",
            snapshots_dir=snapshots_dir or default_snapshots_dir(live.parent),
        )

    shutil.copy2(snapshot, live)
