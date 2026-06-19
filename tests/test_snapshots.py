import sqlite3
from pathlib import Path

from lab_scheduler.data.snapshots import create_snapshot, list_recent_snapshots, restore_snapshot


def test_create_list_and_restore_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "live.sqlite3"
    snapshots_dir = tmp_path / "snapshots"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO demo (value) VALUES ('before')")
    conn.commit()
    conn.close()

    snapshot_path = create_snapshot(
        db_path,
        label="pre-auto-pilot-test",
        snapshots_dir=snapshots_dir,
    )
    assert snapshot_path.is_file()

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE demo SET value = 'after'")
    conn.commit()
    conn.close()

    recent = list_recent_snapshots(snapshots_dir, limit=5)
    assert len(recent) == 1
    assert recent[0].label == "pre-auto-pilot-test"

    restore_snapshot(db_path, snapshot_path, snapshots_dir=snapshots_dir)

    conn = sqlite3.connect(db_path)
    value = conn.execute("SELECT value FROM demo").fetchone()[0]
    conn.close()
    assert value == "before"

    assert len(list_recent_snapshots(snapshots_dir, limit=5)) >= 2
