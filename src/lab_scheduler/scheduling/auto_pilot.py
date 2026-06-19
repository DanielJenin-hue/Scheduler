"""Compatibility shim — implementation lives in lab_scheduler.legacy.auto_pilot."""
from __future__ import annotations

import importlib
import sys

_legacy = importlib.import_module("lab_scheduler.legacy.auto_pilot")
sys.modules[__name__] = _legacy
