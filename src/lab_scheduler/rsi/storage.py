from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, TypeVar

T = TypeVar("T")

RSI_DIR_NAME = ".rsi"


def rsi_root(project_root: Path) -> Path:
    return project_root / RSI_DIR_NAME


def ensure_rsi_layout(project_root: Path) -> Path:
    root = rsi_root(project_root)
    for sub in ("manifests", "reports", "prospector", "dashboard"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_default(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default),
        encoding="utf-8",
    )


def read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
