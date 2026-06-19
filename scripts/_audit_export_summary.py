"""Summarize a breakroom HTML export."""
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
html = path.read_text(encoding="utf-8")
rows = re.findall(r"<tr><td class='emp-col'>([^<]+)</td>", html)
print(f"File: {path}")
print(f"Employee rows: {len(rows)}")
print(f"Tally rows: {sum(1 for row in rows if row.startswith('Total'))}")
for row in rows[:3]:
    print(f"  first: {row[:72]}")
for row in rows[-3:]:
    print(f"  last: {row[:72]}")
meta = re.search(r'class="meta"[^>]*>(.*?)</div>', html, re.S)
if meta:
    text = re.sub(r"<[^>]+>", " ", meta.group(1))
    print("meta:", " ".join(text.split())[:200])
