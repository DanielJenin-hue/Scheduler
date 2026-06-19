import sqlite3
import subprocess
from pathlib import Path

from lab_scheduler.telemetry.patch_worker import (
    PatchWorkerConfig,
    PatchWorkerError,
    apply_patched_content,
    backup_target_file,
    commit_patch_to_isolated_branch,
    deploy_sentry_hotfix,
    process_next_sentry_incident,
    resolve_target_path,
    restore_target_file,
)
from lab_scheduler.telemetry.sentry_watcher import (
    ensure_sentry_schema,
    fetch_oldest_unresolved_sentry_log,
    fetch_sentry_log_by_id,
    format_unified_patch_diff,
    log_unhandled_exception,
    update_sentry_log_for_review,
    update_sentry_log_status,
)


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_sentry_schema(conn)
    return conn


def test_fetch_oldest_unresolved_orders_by_log_id() -> None:
    conn = _memory_db()
    log_unhandled_exception(conn, RuntimeError("first"), tenant_id="t1", username="u1")
    log_unhandled_exception(conn, RuntimeError("second"), tenant_id="t1", username="u1")

    oldest = fetch_oldest_unresolved_sentry_log(conn)
    assert oldest is not None
    assert oldest.log_id == 1
    assert oldest.error_message == "first"


def test_update_sentry_log_status_accepts_patch_outcomes() -> None:
    conn = _memory_db()
    log_id = log_unhandled_exception(conn, RuntimeError("boom"))

    update_sentry_log_status(conn, log_id, "patched")
    record = fetch_sentry_log_by_id(conn, log_id)
    assert record is not None
    assert record.resolution_status == "patched"

    log_id_2 = log_unhandled_exception(conn, RuntimeError("boom-2"))
    update_sentry_log_status(conn, log_id_2, "patch_failed")
    record_2 = fetch_sentry_log_by_id(conn, log_id_2)
    assert record_2 is not None
    assert record_2.resolution_status == "patch_failed"

    log_id_3 = log_unhandled_exception(conn, RuntimeError("boom-3"))
    update_sentry_log_for_review(conn, log_id_3, "--- diff ---")
    record_3 = fetch_sentry_log_by_id(conn, log_id_3)
    assert record_3 is not None
    assert record_3.resolution_status == "awaiting_review"
    assert record_3.proposed_patch_code == "--- diff ---"


def _fake_git_runner_factory(project_root: Path, target_path: Path, patched_content: str):
    state = {"branch": "main", "branch_exists": False, "branch_has_patch": False}

    def _runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert cwd == project_root
        op = command[1:]
        if op[:2] == ["rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{state['branch']}\n", stderr="")
        if op[:2] == ["rev-parse", "--verify"]:
            ref = op[2]
            if ref in {"main", "master"}:
                return subprocess.CompletedProcess(command, 0, stdout=ref + "\n", stderr="")
            if ref == "refs/heads/patch/sentry-log-1" and state["branch_exists"]:
                return subprocess.CompletedProcess(command, 0, stdout=ref + "\n", stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing ref")
        if op[:2] == ["show-ref", "--verify"]:
            branch = op[2].removeprefix("refs/heads/")
            if state["branch_exists"] and branch == "patch/sentry-log-1":
                return subprocess.CompletedProcess(command, 0, stdout=op[2] + "\n", stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        if op[:2] == ["checkout", "main"]:
            state["branch"] = "main"
            target_path.write_text(
                "def broken():\n    raise ValueError('bad')\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if op[:2] == ["branch", "-D"]:
            state["branch_exists"] = False
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if op[:3] == ["checkout", "-b", "patch/sentry-log-1"]:
            state["branch"] = "patch/sentry-log-1"
            state["branch_exists"] = True
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if op[0] == "add":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if op[0] == "commit":
            state["branch_has_patch"] = True
            target_path.write_text(patched_content, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=f"unexpected git op {op}")

    return _runner


def test_process_incident_queues_hitl_review_when_pytest_passes(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    target_dir = project_root / "scripts"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "broken.py"
    original = "def broken():\n    raise ValueError('bad')\n"
    target_file.write_text(original, encoding="utf-8")

    db_path = project_root / "demo.sqlite3"
    conn = sqlite3.connect(str(db_path))
    ensure_sentry_schema(conn)
    conn.execute(
        """
        INSERT INTO sys_sentry_logs (
          recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status
        ) VALUES ('2026-01-01T00:00:00Z', 'tenant-a', 'admin', 'ValueError',
                  'bad', 'scripts/broken.py', 2, 'trace', 'unresolved')
        """
    )
    conn.commit()

    config = PatchWorkerConfig(
        project_root=project_root,
        db_path=db_path,
        api_key="test-key",
        backup_dir=project_root / ".sentry_backups",
        stable_branch="main",
    )
    patched = "def broken():\n    return 'ok'\n"

    def fake_llm(_packet: str, path: Path, _line: int | None, _cfg: PatchWorkerConfig) -> str:
        assert path == target_file
        return patched

    def fake_pytest(_command: list[str], _cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(_command, 0, stdout="47 passed in 0.5s\n", stderr="")

    result = process_next_sentry_incident(
        conn,
        config,
        llm_patch_provider=fake_llm,
        pytest_runner=fake_pytest,
        git_runner=_fake_git_runner_factory(project_root, target_file, patched),
    )

    assert result.handled is True
    assert result.outcome == "awaiting_review"
    assert target_file.read_text(encoding="utf-8") == original
    record = fetch_sentry_log_by_id(conn, 1)
    assert record is not None
    assert record.resolution_status == "awaiting_review"
    assert record.proposed_patch_code is not None
    assert "return 'ok'" in (record.proposed_patch_code or "")


def test_process_incident_restores_backup_when_pytest_fails(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    target_dir = project_root / "src" / "lab_scheduler"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "module.py"
    original = "VALUE = 1\n"
    target_file.write_text(original, encoding="utf-8")

    db_path = project_root / "demo.sqlite3"
    conn = sqlite3.connect(str(db_path))
    ensure_sentry_schema(conn)
    conn.execute(
        """
        INSERT INTO sys_sentry_logs (
          recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status
        ) VALUES ('2026-01-01T00:00:00Z', 'tenant-a', 'admin', 'RuntimeError',
                  'fail', 'src/lab_scheduler/module.py', 1, 'trace', 'unresolved')
        """
    )
    conn.commit()

    config = PatchWorkerConfig(
        project_root=project_root,
        db_path=db_path,
        api_key="test-key",
        backup_dir=project_root / ".sentry_backups",
    )

    def fake_llm(_packet: str, _path: Path, _line: int | None, _cfg: PatchWorkerConfig) -> str:
        return "VALUE = 2\n"

    def fake_pytest(_command: list[str], _cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            _command,
            1,
            stdout="1 failed, 46 passed in 0.5s\n",
            stderr="",
        )

    result = process_next_sentry_incident(
        conn,
        config,
        llm_patch_provider=fake_llm,
        pytest_runner=fake_pytest,
    )

    assert result.handled is True
    assert result.outcome == "patch_failed"
    assert target_file.read_text(encoding="utf-8") == original
    record = fetch_sentry_log_by_id(conn, 1)
    assert record is not None
    assert record.resolution_status == "patch_failed"


def test_deploy_sentry_hotfix_applies_awaiting_review_patch(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    target_dir = project_root / "scripts"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "module.py"
    original = "VALUE = 1\n"
    target_file.write_text(original, encoding="utf-8")

    conn = _memory_db()
    conn.execute(
        """
        INSERT INTO sys_sentry_logs (
          recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, proposed_patch_code
        ) VALUES ('2026-01-01T00:00:00Z', 'tenant-a', 'admin', 'RuntimeError',
                  'fail', 'scripts/module.py', 1, 'trace', 'awaiting_review', 'VALUE = 99\n')
        """
    )
    conn.commit()

    updated = deploy_sentry_hotfix(conn, 1, project_root)
    assert updated.resolution_status == "patched"
    assert target_file.read_text(encoding="utf-8") == "VALUE = 99\n"


def test_format_unified_patch_diff_contains_changed_lines() -> None:
    original = "alpha\nbeta\ngamma\n"
    patched = "alpha\nBETA\ngamma\n"
    diff = format_unified_patch_diff(
        original_content=original,
        patched_content=patched,
        target_file="scripts/sample.py",
    )
    assert "-beta" in diff
    assert "+BETA" in diff


def test_resolve_target_path_rejects_outside_scope(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = project_root / "secrets.py"
    outside.write_text("SECRET = True\n", encoding="utf-8")

    try:
        resolve_target_path(project_root, "secrets.py")
        raise AssertionError("expected PatchWorkerError")
    except PatchWorkerError as exc:
        assert "outside allowed patch scope" in str(exc)


def test_resolve_target_path_rejects_scripts_app_py(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    app_path = project_root / "scripts"
    app_path.mkdir(parents=True)
    (app_path / "app.py").write_text("import streamlit\n", encoding="utf-8")

    try:
        resolve_target_path(project_root, "scripts/app.py")
        raise AssertionError("expected PatchWorkerError")
    except PatchWorkerError as exc:
        assert "forbidden" in str(exc)
        assert "scripts/app.py" in str(exc)


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    target_dir = project_root / "scripts"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "sample.py"
    target_file.write_text("before\n", encoding="utf-8")

    backup_path = backup_target_file(
        target_file,
        log_id=7,
        backup_dir=project_root / ".sentry_backups",
    )
    apply_patched_content(target_file, "after\n")
    restore_target_file(target_file, backup_path)

    assert target_file.read_text(encoding="utf-8") == "before\n"


def _fake_git_runner_for_branch(project_root: Path, target_path: Path, patched_content: str, log_id: int):
    branch_name = f"patch/sentry-log-{log_id}"
    state = {"branch": "main", "branch_exists": False}

    def _runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert cwd == project_root
        op = command[1:]
        if op[:2] == ["rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{state['branch']}\n", stderr="")
        if op[:2] == ["rev-parse", "--verify"]:
            ref = op[2]
            if ref in {"main", "master"}:
                return subprocess.CompletedProcess(command, 0, stdout=ref + "\n", stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing ref")
        if op[:2] == ["show-ref", "--verify"]:
            if state["branch_exists"] and op[2] == f"refs/heads/{branch_name}":
                return subprocess.CompletedProcess(command, 0, stdout=op[2] + "\n", stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        if op[:2] == ["checkout", "main"]:
            state["branch"] = "main"
            target_path.write_text("VALUE = 1\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if op[:2] == ["branch", "-D"]:
            state["branch_exists"] = False
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if op[:3] == ["checkout", "-b", branch_name]:
            state["branch"] = branch_name
            state["branch_exists"] = True
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if op[0] == "add":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if op[0] == "commit":
            target_path.write_text(patched_content, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=f"unexpected git op {op}")

    return _runner


def test_commit_patch_to_isolated_branch_keeps_stable_clean(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    target_dir = project_root / "scripts"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "module.py"
    original = "VALUE = 1\n"
    target_file.write_text(original, encoding="utf-8")

    branch = commit_patch_to_isolated_branch(
        project_root,
        log_id=42,
        target_path=target_file,
        patched_content="VALUE = 2\n",
        stable_branch="main",
        runner=_fake_git_runner_for_branch(project_root, target_file, "VALUE = 2\n", 42),
    )
    assert branch == "patch/sentry-log-42"
    assert target_file.read_text(encoding="utf-8") == original
