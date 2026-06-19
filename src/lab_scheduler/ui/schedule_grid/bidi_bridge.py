"""Streamlit Components v2 bridge for grid edits (replaces broken v1 setComponentValue)."""

from __future__ import annotations

from typing import Any, Mapping, Optional

import streamlit as st

from lab_scheduler.ui.schedule_grid.browser_storage import GRID_BROWSER_STORAGE_JS

_GRID_BRIDGE_JS = (
    GRID_BROWSER_STORAGE_JS
    + """
export default function(component) {
  const { setStateValue, data } = component;
  const storageKey = data.storageKey;
  const shouldClear = Boolean(data.clearStorage);
  const readPending = Boolean(data.readPending);

  function publishPending(source) {
    const payload = collectAllGridPending(storageKey);
    const count = (payload.changes || []).length;
    const signature = count + ":" + JSON.stringify((payload.changes || []).slice(-3));
    if (source === "message" && signature === lastPublishedSignature) {
      return;
    }
    lastPublishedSignature = signature;
    setStateValue("pending", {
      changes: payload.changes || [],
      lock_toggles: payload.lock_toggles || null,
      tally_select: payload.tally_select || null,
      source: source || "bridge",
      count: count,
    });
  }

  function schedulePublish(source) {
    if (publishTimer) {
      clearTimeout(publishTimer);
    }
    publishTimer = setTimeout(function () {
      publishTimer = null;
      publishPending(source);
    }, 400);
  }

  var publishTimer = null;
  var lastPublishedSignature = "";

  function ensureBridgeListener() {
    const root = labGridTopRoot();
    if (root.__labGridBridgeListenerReady) return;
    root.__labGridBridgeListenerReady = true;
    root.addEventListener("message", function (event) {
      if (!event.data || event.data.type !== "lab-grid-persist") return;
      if (event.data.storageKey !== storageKey) return;
      schedulePublish("message");
    });
  }

  ensureTopGridPersistListener();
  ensureBridgeListener();
  if (shouldClear) {
    publishPending("save-drain");
    clearAllGridPending(storageKey);
    publishPending("cleared");
  } else if (readPending) {
    publishPending("save-drain");
  }
}
"""
)

_BRIDGE_COMPONENT = None


def _bridge_component():
    global _BRIDGE_COMPONENT
    if _BRIDGE_COMPONENT is None:
        _BRIDGE_COMPONENT = st.components.v2.component(
            "lab_scheduler_grid_storage_bridge",
            js=_GRID_BRIDGE_JS,
            html='<div aria-hidden="true" style="display:none"></div>',
            isolate_styles=False,
        )
    return _BRIDGE_COMPONENT


def mount_lab_grid_storage_bridge(
    period_id: str,
    *,
    clear: bool = False,
    read_pending: bool = False,
    key_suffix: str = "",
) -> Optional[Mapping[str, Any]]:
    """Read pending grid edits from browser storage via Components v2."""

    storage_key = f"lab_grid_pending_{period_id}"
    result = _bridge_component()(
        key=f"lab_grid_storage_bridge_{period_id}{key_suffix}",
        data={
            "storageKey": storage_key,
            "clearStorage": clear,
            "readPending": read_pending,
            "periodId": period_id,
        },
        height=1,
        on_pending_change=lambda: None,
    )
    pending = getattr(result, "pending", None)
    if isinstance(pending, Mapping):
        return pending
    if isinstance(pending, dict):
        return pending
    return None
