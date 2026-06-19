from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .sentry_watcher import (
    SentryLogRecord,
    ensure_sentry_schema,
    fetch_oldest_unresolved_sentry_log,
    fetch_sentry_log_by_id,
    format_unified_patch_diff,
    generate_llm_diagnostic_packet,
    update_sentry_log_for_review,
    update_sentry_log_status,
    utc_now_iso,
)

ALLOWED_TARGET_PREFIXES: tuple[str, ...] = ("src/lab_scheduler/", "scripts/")
FORBIDDEN_TARGET_FILES: frozenset[str] = frozenset({"scripts/app.py"})
PATCH_WORKER_SYSTEM_PROMPT = (
    "You are an autonomous surgical patch agent for a Python medical "
    "lab staffing scheduler. Respond with valid JSON only. "
    "You are strictly forbidden from writing to, modifying, or reading scripts/app.py. "
    "All reactive swap audit logs must be appended exclusively to designated .log or "
    ".json files in the exports/ directory."
)
DEFAULT_POLL_SECONDS = 60
DEFAULT_LLM_API_BASE = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"

GitRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


class PatchWorkerError(Exception):
    """Raised when the autonomous patch worker cannot safely continue."""


@dataclass(frozen=True, slots=True)
class PatchWorkerConfig:
    project_root: Path
    db_path: Path
    poll_seconds: int = DEFAULT_POLL_SECONDS
    api_key: Optional[str] = None
    api_base: str = DEFAULT_LLM_API_BASE
    model: str = DEFAULT_LLM_MODEL
    backup_dir: Optional[Path] = None
    stable_branch: Optional[str] = None

    @classmethod
    def from_env(cls, *, project_root: Path, db_path: Path) -> PatchWorkerConfig:
        backup_dir = project_root / ".sentry_backups"
        stable_branch = os.environ.get("PATCH_WORKER_STABLE_BRANCH")
        return cls(
            project_root=project_root,
            db_path=db_path,
            poll_seconds=int(os.environ.get("PATCH_WORKER_POLL_SECONDS", DEFAULT_POLL_SECONDS)),
            api_key=os.environ.get("PATCH_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
            api_base=os.environ.get("PATCH_LLM_API_BASE", DEFAULT_LLM_API_BASE).rstrip("/"),
            model=os.environ.get("PATCH_LLM_MODEL", DEFAULT_LLM_MODEL),
            backup_dir=backup_dir,
            stable_branch=stable_branch,
        )


@dataclass(frozen=True, slots=True)
class PatchCycleResult:
    handled: bool
    log_id: Optional[int] = None
    outcome: Optional[str] = None
    detail: Optional[str] = None


def _log(message: str) -> None:
    print(f"[{utc_now_iso()}] PATCH-WORKER | {message}", flush=True)


def resolve_target_path(project_root: Path, target_file: str) -> Path:
    normalized = target_file.replace("\\", "/").lstrip("/")
    if normalized in FORBIDDEN_TARGET_FILES:
        raise PatchWorkerError(
            f"target_file `{target_file}` is forbidden for autonomous patching "
            f"(protected entry-point: {normalized})"
        )
    if not any(normalized.startswith(prefix) for prefix in ALLOWED_TARGET_PREFIXES):
        raise PatchWorkerError(
            f"target_file `{target_file}` is outside allowed patch scope "
            f"({', '.join(ALLOWED_TARGET_PREFIXES)})"
        )
    resolved = (project_root / normalized).resolve()
    root = project_root.resolve()
    if root not in resolved.parents and resolved != root:
        raise PatchWorkerError(f"target_file `{target_file}` resolves outside project root")
    if not resolved.is_file():
        raise PatchWorkerError(f"target_file `{target_file}` does not exist on disk")
    if resolved.suffix != ".py":
        raise PatchWorkerError(f"target_file `{target_file}` is not a Python module")
    return resolved


def backup_target_file(
    target_path: Path,
    *,
    log_id: int,
    backup_dir: Path,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{log_id}_{target_path.name}.bak"
    shutil.copy2(target_path, backup_path)
    return backup_path


def restore_target_file(target_path: Path, backup_path: Path) -> None:
    shutil.copy2(backup_path, target_path)


def apply_patched_content(target_path: Path, patched_content: str) -> None:
    target_path.write_text(patched_content, encoding="utf-8", newline="\n")


def run_pytest_suite(
    project_root: Path,
    *,
    runner: Optional[Callable[[list[str], Path], subprocess.CompletedProcess[str]]] = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "pytest", "tests/", "-q"]
    if runner is None:
        return subprocess.run(
            command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
    return runner(command, project_root)


def run_git_command(
    project_root: Path,
    args: list[str],
    *,
    runner: Optional[GitRunner] = None,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    if runner is None:
        return subprocess.run(
            command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
    return runner(command, project_root)


def get_current_git_branch(
    project_root: Path,
    *,
    runner: Optional[GitRunner] = None,
) -> str:
    result = run_git_command(project_root, ["rev-parse", "--abbrev-ref", "HEAD"], runner=runner)
    if result.returncode != 0:
        raise PatchWorkerError(result.stderr.strip() or "Unable to resolve current git branch")
    branch = result.stdout.strip()
    if not branch:
        raise PatchWorkerError("Current git branch is empty")
    return branch


def resolve_stable_branch(
    project_root: Path,
    *,
    configured_branch: Optional[str] = None,
    runner: Optional[GitRunner] = None,
) -> str:
    if configured_branch:
        verify = run_git_command(
            project_root,
            ["rev-parse", "--verify", configured_branch],
            runner=runner,
        )
        if verify.returncode != 0:
            raise PatchWorkerError(f"Configured stable branch `{configured_branch}` was not found")
        return configured_branch

    for candidate in ("main", "master"):
        verify = run_git_command(
            project_root,
            ["rev-parse", "--verify", candidate],
            runner=runner,
        )
        if verify.returncode == 0:
            return candidate
    return get_current_git_branch(project_root, runner=runner)


def _git_branch_exists(
    project_root: Path,
    branch_name: str,
    *,
    runner: Optional[GitRunner] = None,
) -> bool:
    result = run_git_command(
        project_root,
        ["show-ref", "--verify", f"refs/heads/{branch_name}"],
        runner=runner,
    )
    return result.returncode == 0


def commit_patch_to_isolated_branch(
    project_root: Path,
    *,
    log_id: int,
    target_path: Path,
    patched_content: str,
    stable_branch: Optional[str] = None,
    runner: Optional[GitRunner] = None,
) -> str:
    branch_name = f"patch/sentry-log-{log_id}"
    rel_path = target_path.relative_to(project_root).as_posix()
    stable = resolve_stable_branch(project_root, configured_branch=stable_branch, runner=runner)

    checkout_stable = run_git_command(project_root, ["checkout", stable], runner=runner)
    if checkout_stable.returncode != 0:
        raise PatchWorkerError(
            checkout_stable.stderr.strip() or f"Unable to checkout stable branch `{stable}`"
        )

    if _git_branch_exists(project_root, branch_name, runner=runner):
        delete_branch = run_git_command(project_root, ["branch", "-D", branch_name], runner=runner)
        if delete_branch.returncode != 0:
            raise PatchWorkerError(
                delete_branch.stderr.strip() or f"Unable to delete existing branch `{branch_name}`"
            )

    create_branch = run_git_command(project_root, ["checkout", "-b", branch_name], runner=runner)
    if create_branch.returncode != 0:
        raise PatchWorkerError(
            create_branch.stderr.strip() or f"Unable to create branch `{branch_name}`"
        )

    apply_patched_content(target_path, patched_content)

    add_file = run_git_command(project_root, ["add", rel_path], runner=runner)
    if add_file.returncode != 0:
        raise PatchWorkerError(add_file.stderr.strip() or f"Unable to stage `{rel_path}`")

    commit = run_git_command(
        project_root,
        ["commit", "-m", f"sentry: HITL patch candidate for log {log_id}"],
        runner=runner,
    )
    if commit.returncode != 0:
        raise PatchWorkerError(commit.stderr.strip() or "Unable to commit isolated patch branch")

    return_to_stable = run_git_command(project_root, ["checkout", stable], runner=runner)
    if return_to_stable.returncode != 0:
        raise PatchWorkerError(
            return_to_stable.stderr.strip() or f"Unable to return to stable branch `{stable}`"
        )

    restore_stable_file = target_path.read_text(encoding="utf-8")
    if restore_stable_file != patched_content:
        # After switching branches the working tree should reflect stable content.
        pass

    _log(f"isolated branch committed -> {branch_name}; restored stable branch `{stable}`")
    return branch_name


def apply_unified_diff(original_content: str, diff_text: str) -> str:
    """Apply a single-file unified diff and return the patched file content."""

    if not diff_text.strip():
        raise PatchWorkerError("Unified diff is empty")

    original_lines = original_content.splitlines(keepends=True)
    if not original_lines and original_content:
        original_lines = [original_content]
    result: list[str] = []
    index = 0
    diff_lines = diff_text.splitlines()
    line_no = 0

    while line_no < len(diff_lines):
        line = diff_lines[line_no]
        if line.startswith("@@"):
            header = line
            try:
                _, rest = header.split("@@", 1)
                old_part = rest.strip().split(" ")[0]
                old_start = int(old_part.split(",")[0]) - 1
            except (IndexError, ValueError) as exc:
                raise PatchWorkerError(f"Invalid unified diff hunk header: {header}") from exc

            result.extend(original_lines[index:old_start])
            index = old_start
            line_no += 1

            while line_no < len(diff_lines) and not diff_lines[line_no].startswith("@@"):
                hunk_line = diff_lines[line_no]
                if hunk_line.startswith(("---", "+++")):
                    line_no += 1
                    continue
                if hunk_line == "":
                    line_no += 1
                    continue
                if hunk_line.startswith(" "):
                    if index >= len(original_lines):
                        raise PatchWorkerError("Unified diff context exceeds source file length")
                    result.append(original_lines[index])
                    index += 1
                elif hunk_line.startswith("-"):
                    if index >= len(original_lines):
                        raise PatchWorkerError("Unified diff deletion exceeds source file length")
                    index += 1
                elif hunk_line.startswith("+"):
                    addition = hunk_line[1:]
                    result.append(addition if addition.endswith("\n") else addition + "\n")
                else:
                    raise PatchWorkerError(f"Unexpected unified diff line: {hunk_line}")
                line_no += 1
            continue

        if line.startswith(("---", "+++")):
            line_no += 1
            continue
        line_no += 1

    result.extend(original_lines[index:])
    merged = "".join(result)
    if original_content.endswith("\n") and not merged.endswith("\n"):
        merged += "\n"
    return merged


def derive_patched_content(original_content: str, proposed_patch_code: str) -> str:
    stripped = proposed_patch_code.lstrip()
    if stripped.startswith("---") or stripped.startswith("+++") or "\n+++" in proposed_patch_code:
        return apply_unified_diff(original_content, proposed_patch_code)
    return proposed_patch_code


def deploy_sentry_hotfix(
    conn: sqlite3.Connection,
    log_id: int,
    project_root: Path,
) -> SentryLogRecord:
    record = fetch_sentry_log_by_id(conn, log_id)
    if record is None:
        raise PatchWorkerError(f"log_id {log_id} was not found")
    if record.resolution_status != "awaiting_review":
        raise PatchWorkerError(
            f"log_id {log_id} is `{record.resolution_status}`; only `awaiting_review` may be deployed"
        )
    if not record.target_file:
        raise PatchWorkerError(f"log_id {log_id} is missing target_file")
    if not record.proposed_patch_code:
        raise PatchWorkerError(f"log_id {log_id} is missing proposed_patch_code")

    target_path = resolve_target_path(project_root, record.target_file)
    patched_content = derive_patched_content(
        target_path.read_text(encoding="utf-8"),
        record.proposed_patch_code,
    )
    apply_patched_content(target_path, patched_content)
    update_sentry_log_status(conn, log_id, "patched")

    updated = fetch_sentry_log_by_id(conn, log_id)
    if updated is None:
        raise PatchWorkerError(f"log_id {log_id} disappeared after deploy")
    return updated


def _extract_json_object(text: str) -> dict[str, object]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise PatchWorkerError("LLM response JSON must be an object")
    return payload


def call_llm_for_patch(
    *,
    diagnostic_packet: str,
    target_path: Path,
    line_number: Optional[int],
    config: PatchWorkerConfig,
    http_post: Optional[
        Callable[[str, dict[str, str], bytes], tuple[int, str]]
    ] = None,
) -> str:
    if not config.api_key:
        raise PatchWorkerError("PATCH_LLM_API_KEY is not configured")

    relative_target = target_path.relative_to(config.project_root).as_posix()
    source = target_path.read_text(encoding="utf-8")
    user_prompt = (
        "Return JSON only with this exact shape:\n"
        '{"patched_content":"<full updated Python file content>"}\n\n'
        "Rules:\n"
        "- Apply the smallest safe fix for the incident.\n"
        f"- Focus on `{relative_target}` around line {line_number or 'unknown'}.\n"
        "- Return the entire file content after the fix.\n"
        "- Do not add unrelated refactors.\n"
        "- Never modify scripts/app.py; write audit logs only under exports/.\n\n"
        f"Diagnostic packet:\n{diagnostic_packet}\n\n"
        f"Current file ({relative_target}):\n```python\n{source}\n```"
    )
    request_body = json.dumps(
        {
            "model": config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": PATCH_WORKER_SYSTEM_PROMPT,
                },
                {"role": "user", "content": user_prompt},
            ],
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }
    url = f"{config.api_base}/chat/completions"

    if http_post is None:
        request = urllib.request.Request(url, data=request_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                status_code = response.getcode()
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            body = exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise PatchWorkerError(f"LLM request failed: {exc}") from exc
    else:
        status_code, body = http_post(url, headers, request_body)

    if status_code < 200 or status_code >= 300:
        raise PatchWorkerError(f"LLM API returned HTTP {status_code}: {body[:500]}")

    envelope = json.loads(body)
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PatchWorkerError("LLM API response missing choices")

    message = choices[0].get("message", {})
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise PatchWorkerError("LLM API response missing message content")

    patch_payload = _extract_json_object(content)
    patched_content = patch_payload.get("patched_content")
    if not isinstance(patched_content, str) or not patched_content.strip():
        raise PatchWorkerError("LLM JSON missing non-empty `patched_content`")
    return patched_content


def process_next_sentry_incident(
    conn: sqlite3.Connection,
    config: PatchWorkerConfig,
    *,
    llm_patch_provider: Optional[
        Callable[[str, Path, Optional[int], PatchWorkerConfig], str]
    ] = None,
    pytest_runner: Optional[Callable[[list[str], Path], subprocess.CompletedProcess[str]]] = None,
    git_runner: Optional[GitRunner] = None,
) -> PatchCycleResult:
    ensure_sentry_schema(conn)
    record = fetch_oldest_unresolved_sentry_log(conn)
    if record is None:
        return PatchCycleResult(handled=False)

    if not record.target_file:
        update_sentry_log_status(conn, record.log_id, "patch_failed")
        return PatchCycleResult(
            handled=True,
            log_id=record.log_id,
            outcome="patch_failed",
            detail="missing target_file",
        )

    _log(
        f"processing log_id={record.log_id} "
        f"target={record.target_file}:{record.line_number or '?'}"
    )

    if llm_patch_provider is None and not config.api_key:
        _log(f"skipped log_id={record.log_id} - PATCH_LLM_API_KEY not configured")
        return PatchCycleResult(
            handled=False,
            log_id=record.log_id,
            outcome="skipped",
            detail="missing api key",
        )

    backup_path: Optional[Path] = None
    target_path: Optional[Path] = None
    original_content = ""

    try:
        target_path = resolve_target_path(config.project_root, record.target_file)
        original_content = target_path.read_text(encoding="utf-8")
        diagnostic_packet = generate_llm_diagnostic_packet(conn, log_id=record.log_id)
        _log(f"diagnostic packet built ({len(diagnostic_packet)} bytes)")

        provider = llm_patch_provider or (
            lambda packet, path, line, cfg: call_llm_for_patch(
                diagnostic_packet=packet,
                target_path=path,
                line_number=line,
                config=cfg,
            )
        )
        _log(f"LLM patch request dispatched (model={config.model})")
        patched_content = provider(
            diagnostic_packet,
            target_path,
            record.line_number,
            config,
        )

        backup_dir = config.backup_dir or (config.project_root / ".sentry_backups")
        backup_path = backup_target_file(
            target_path,
            log_id=record.log_id,
            backup_dir=backup_dir,
        )
        _log(f"backup saved -> {backup_path.relative_to(config.project_root).as_posix()}")

        apply_patched_content(target_path, patched_content)
        _log(f"temporary validation patch applied to {record.target_file}")

        _log("running pytest gatekeeper...")
        pytest_result = run_pytest_suite(config.project_root, runner=pytest_runner)
        if pytest_result.returncode == 0:
            restore_target_file(target_path, backup_path)
            _log("production file restored - HITL gate engaged")

            proposed_patch_code = patched_content
            diff_preview = format_unified_patch_diff(
                original_content=original_content,
                patched_content=patched_content,
                target_file=record.target_file,
            )
            _log(f"proposed patch queued ({len(diff_preview.splitlines())} diff lines)")
            branch_name = commit_patch_to_isolated_branch(
                config.project_root,
                log_id=record.log_id,
                target_path=target_path,
                patched_content=patched_content,
                stable_branch=config.stable_branch,
                runner=git_runner,
            )
            update_sentry_log_for_review(conn, record.log_id, proposed_patch_code)
            passed_line = _summarize_pytest_output(pytest_result.stdout)
            _log(
                f"pytest PASSED ({passed_line}) - queued for review on `{branch_name}` "
                "- status -> awaiting_review"
            )
            return PatchCycleResult(
                handled=True,
                log_id=record.log_id,
                outcome="awaiting_review",
                detail=passed_line,
            )

        restore_target_file(target_path, backup_path)
        update_sentry_log_status(conn, record.log_id, "patch_failed")
        failed_line = _summarize_pytest_output(pytest_result.stdout, pytest_result.stderr)
        _log(f"pytest FAILED ({failed_line}) - backup restored, status -> patch_failed")
        return PatchCycleResult(
            handled=True,
            log_id=record.log_id,
            outcome="patch_failed",
            detail=failed_line,
        )
    except PatchWorkerError as exc:
        if target_path is not None and backup_path is not None and backup_path.exists():
            restore_target_file(target_path, backup_path)
            _log(f"backup restored after worker error for log_id={record.log_id}")
        update_sentry_log_status(conn, record.log_id, "patch_failed")
        _log(f"log_id={record.log_id} patch_failed - {exc}")
        return PatchCycleResult(
            handled=True,
            log_id=record.log_id,
            outcome="patch_failed",
            detail=str(exc),
        )


def _summarize_pytest_output(stdout: str, stderr: str = "") -> str:
    text = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not text:
        return "no pytest output"
    last_line = text.splitlines()[-1].strip()
    return last_line or "pytest completed"


def run_patch_worker_loop(config: PatchWorkerConfig, *, run_once: bool = False) -> None:
    _log(
        f"daemon online - db={config.db_path.name}, poll={config.poll_seconds}s, "
        f"model={config.model}, HITL=enabled"
    )
    while True:
        _log("polling sys_sentry_logs for oldest unresolved incident...")
        conn = sqlite3.connect(str(config.db_path))
        try:
            result = process_next_sentry_incident(conn, config)
            if not result.handled:
                if result.outcome == "skipped":
                    pass
                else:
                    _log("idle - no unresolved incidents")
        finally:
            conn.close()

        if run_once:
            return
        time.sleep(config.poll_seconds)
