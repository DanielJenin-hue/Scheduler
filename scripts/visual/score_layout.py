"""Visual layout scorecard harness for the breakroom printout (dev-only).

Takes a rendered PNG snapshot (from ``snapshot_breakroom.py``) and grades its
visual layout, emitting a ``breakroom_layout_scorecard.v1`` JSON document and a
pass/fail decision against a threshold gate.

The model call lives behind a single pluggable seam (``grade_image``):

    - In production you pass a ``vision_fn`` that base64-encodes the PNG, sends it
      with VISION_SYSTEM_PROMPT to a vision-capable model (Claude / GPT-4o), and
      returns the raw JSON text the model produced.
    - There is NO live vision model in this sandbox, so the default seam is an
      offline deterministic STUB that returns a fixed, schema-valid scorecard.
      This keeps CI green and the contract exercised without network access.

Run:  python scripts/visual/score_layout.py --image artifacts/breakroom_visual/breakroom_standard_legal.png
      python scripts/visual/score_layout.py   # offline stub, no image required
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = ROOT / "artifacts" / "breakroom_visual"
SCHEMA_PATH = Path(__file__).resolve().parent / "breakroom_layout_scorecard.v1.schema.json"
SCHEMA_ID = "breakroom_layout_scorecard.v1"

# Mirrors the ROUTER-8H prompt-as-asset pattern: a strict, grade-only prompt that
# forbids prose and forces a single JSON object matching the scorecard schema.
VISION_SYSTEM_PROMPT = """\
=== SYSTEM PERSONA ===
You are a print-layout QA grader for a hospital lab breakroom schedule poster.
You receive ONE image of a printed schedule grid (employees x dates) and grade
its visual quality for a wall-mounted, glance-readable poster.

=== WHAT TO GRADE (each 0-100, higher is better) ===
1. readability      - can a nurse read shift letters (D/E/N/T) from a few feet?
2. text_clipping    - is any text cut off, overflowing a cell, or wrapping badly? (100 = none)
3. contrast         - are shift tokens and OPEN markers high-contrast vs background?
4. layout_density   - are columns/rows balanced, not cramped or wildly uneven?
5. gap_legibility   - are unfilled "Coverage Gaps" / OPEN cells clearly distinct from worked/off cells?

=== OUTPUT CONTRACT ===
Return ONE JSON object and NOTHING else. No markdown, no prose. It MUST match:
{
  "schema": "breakroom_layout_scorecard.v1",
  "image_path": "<echo the provided path>",
  "paper_size": "<legal|ledger|letter>",
  "scores": {"readability": int, "text_clipping": int, "contrast": int,
             "layout_density": int, "gap_legibility": int},
  "issues": [{"severity": "low|med|high", "category": "<one category>",
              "where": "<locator>", "note": "<short note>"}],
  "overall": int,
  "pass": bool
}
Compute "overall" as the rounded mean of the five scores. Set "pass" true only if
"overall" >= the threshold you are told to use (default 80) AND no "high" issue exists.
"""

DEFAULT_THRESHOLD = 80
_SCORE_KEYS = ("readability", "text_clipping", "contrast", "layout_density", "gap_legibility")

# Type of the model seam: (image_path, paper_size, prompt) -> raw JSON string.
VisionFn = Callable[[Optional[Path], str, str], str]


def _stub_vision_fn(image_path: Optional[Path], paper_size: str, _prompt: str) -> str:
    """Offline deterministic stand-in for a vision model. Returns schema-valid JSON."""
    scores = {
        "readability": 88,
        "text_clipping": 92,
        "contrast": 90,
        "layout_density": 84,
        "gap_legibility": 86,
    }
    overall = round(sum(scores.values()) / len(scores))
    card = {
        "schema": SCHEMA_ID,
        "image_path": str(image_path) if image_path else "(stub: no image)",
        "paper_size": paper_size,
        "scores": scores,
        "issues": [
            {
                "severity": "low",
                "category": "layout_density",
                "where": "date header row",
                "note": "Stub grade - 14 columns comfortable; verify density at 56 columns.",
            }
        ],
        "overall": overall,
        "pass": overall >= DEFAULT_THRESHOLD,
    }
    return json.dumps(card)


def validate_scorecard(card: Dict[str, object]) -> None:
    """Lightweight, dependency-free validation against the v1 schema contract."""
    if card.get("schema") != SCHEMA_ID:
        raise ValueError(f"scorecard.schema must be '{SCHEMA_ID}', got {card.get('schema')!r}")
    scores = card.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("scorecard.scores must be an object")
    for key in _SCORE_KEYS:
        val = scores.get(key)
        if not isinstance(val, int) or not (0 <= val <= 100):
            raise ValueError(f"scorecard.scores.{key} must be int in [0,100], got {val!r}")
    overall = card.get("overall")
    if not isinstance(overall, int) or not (0 <= overall <= 100):
        raise ValueError(f"scorecard.overall must be int in [0,100], got {overall!r}")
    if not isinstance(card.get("pass"), bool):
        raise ValueError("scorecard.pass must be a bool")
    for issue in card.get("issues", []) or []:
        if issue.get("severity") not in {"low", "med", "high"}:
            raise ValueError(f"issue.severity invalid: {issue.get('severity')!r}")


def apply_threshold_gate(card: Dict[str, object], threshold: int) -> Dict[str, object]:
    """Re-derive overall + pass from the scores so the gate is authoritative."""
    scores = card["scores"]  # type: ignore[index]
    overall = round(sum(int(scores[k]) for k in _SCORE_KEYS) / len(_SCORE_KEYS))
    has_high = any(
        (issue.get("severity") == "high") for issue in (card.get("issues") or [])
    )
    card["overall"] = overall
    card["pass"] = overall >= threshold and not has_high
    return card


def grade_image(
    image_path: Optional[Path],
    *,
    paper_size: str = "legal",
    threshold: int = DEFAULT_THRESHOLD,
    vision_fn: VisionFn | None = None,
) -> Dict[str, object]:
    """Grade a rendered snapshot. ``vision_fn`` defaults to the offline stub seam."""
    seam = vision_fn or _stub_vision_fn
    raw = seam(image_path, paper_size, VISION_SYSTEM_PROMPT)
    card = json.loads(raw)
    card.setdefault("rendered_at", datetime.now(timezone.utc).isoformat())
    validate_scorecard(card)
    return apply_threshold_gate(card, threshold)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade a breakroom snapshot's visual layout.")
    parser.add_argument("--image", type=Path, default=None, help="PNG snapshot to grade.")
    parser.add_argument("--paper", default="legal", choices=["legal", "ledger", "letter"])
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--out", type=Path, default=None, help="Where to write the scorecard JSON.")
    args = parser.parse_args(argv)

    if args.image is not None and not args.image.exists():
        print(f"Image not found: {args.image} (running stub seam anyway)", file=sys.stderr)

    card = grade_image(args.image, paper_size=args.paper, threshold=args.threshold)

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = args.out or (ARTIFACT_DIR / "breakroom_layout_scorecard.json")
    out_path.write_text(json.dumps(card, indent=2), encoding="utf-8")

    print(f"Wrote scorecard: {out_path}")
    print(f"overall={card['overall']}  pass={card['pass']}  threshold={args.threshold}")
    return 0 if card["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
