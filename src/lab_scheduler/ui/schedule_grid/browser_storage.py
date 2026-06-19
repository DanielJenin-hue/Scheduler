"""Shared browser storage helpers for the schedule grid iframe and Python bridge."""

from __future__ import annotations

GRID_BROWSER_STORAGE_JS = """
function labStorageLayers() {
  const layers = [];
  const seen = new Set();
  function add(storage) {
    if (!storage || seen.has(storage)) return;
    seen.add(storage);
    layers.push(storage);
  }
  try { add(window.top && window.top.sessionStorage); } catch (err) {}
  try { add(window.top && window.top.localStorage); } catch (err) {}
  try {
    add(window.parent && window.parent !== window && window.parent.sessionStorage);
  } catch (err) {}
  try {
    add(window.parent && window.parent !== window && window.parent.localStorage);
  } catch (err) {}
  add(sessionStorage);
  add(localStorage);
  return layers;
}
function labSharedSessionStorage() {
  const layers = labStorageLayers();
  return {
    getItem: function (key) {
      for (var i = 0; i < layers.length; i++) {
        try {
          var value = layers[i].getItem(key);
          if (value) return value;
        } catch (err) {}
      }
      return null;
    },
    setItem: function (key, value) {
      for (var i = 0; i < layers.length; i++) {
        try { layers[i].setItem(key, value); } catch (err) {}
      }
    },
    removeItem: function (key) {
      for (var i = 0; i < layers.length; i++) {
        try { layers[i].removeItem(key); } catch (err) {}
      }
    }
  };
}
function labGridTopRoot() {
  try {
    if (window.top) return window.top;
  } catch (err) {}
  try {
    if (window.parent && window.parent !== window) return window.parent;
  } catch (err2) {}
  return window;
}
function labGridPendingStoreRoot() {
  var root = labGridTopRoot();
  if (!root.__labGridPendingStore) {
    root.__labGridPendingStore = {};
  }
  return root.__labGridPendingStore;
}
function labGridPendingStoreGet(storageKey) {
  var store = labGridPendingStoreRoot();
  return store[storageKey] || null;
}
function labGridPendingStoreSet(storageKey, payload) {
  var store = labGridPendingStoreRoot();
  store[storageKey] = payload;
}
function labGridPendingStoreClear(storageKey) {
  var store = labGridPendingStoreRoot();
  try { delete store[storageKey]; } catch (err) { store[storageKey] = undefined; }
}
function mergeGridStorePayload(target, incoming) {
  if (!incoming || !incoming.changes) return target;
  if (!target.changes) target.changes = [];
  incoming.changes.forEach(function (item) {
    if (!item || !item.employee_id || !item.date) return;
    var existing = target.changes.findIndex(function (entry) {
      return entry.employee_id === item.employee_id && entry.date === item.date;
    });
    if (existing >= 0) {
      target.changes[existing] = item;
    } else {
      target.changes.push(item);
    }
  });
  if (incoming.lock_toggles) target.lock_toggles = incoming.lock_toggles;
  if (incoming.tally_select) target.tally_select = incoming.tally_select;
  return target;
}
function ensureTopGridPersistListener() {
  var root = labGridTopRoot();
  if (root.__labGridPersistListenerReady) return;
  root.__labGridPersistListenerReady = true;
  root.addEventListener("message", function (event) {
    if (!event.data || event.data.type !== "lab-grid-persist") return;
    var storageKey = event.data.storageKey;
    var payload = event.data.payload;
    if (!storageKey || !payload) return;
    if (!root.__labGridPendingStore) root.__labGridPendingStore = {};
    var stored = root.__labGridPendingStore[storageKey] || { changes: [] };
    root.__labGridPendingStore[storageKey] = mergeGridStorePayload(stored, payload);
  });
}
function collectAllGridPending(storageKey) {
  ensureTopGridPersistListener();
  var payload = { changes: [] };
  var roots = [];
  try { roots.push(window.top); } catch (err) {}
  try { if (window.parent && window.parent !== window) roots.push(window.parent); } catch (err2) {}
  roots.push(window);
  roots.forEach(function (root) {
    if (!root) return;
    try {
      if (root.__labGridPendingStore && root.__labGridPendingStore[storageKey]) {
        payload = mergeGridStorePayload(payload, root.__labGridPendingStore[storageKey]);
      }
    } catch (err3) {}
    try {
      var raw = root.sessionStorage && root.sessionStorage.getItem(storageKey);
      if (raw) payload = mergeGridStorePayload(payload, JSON.parse(raw));
    } catch (err4) {}
    try {
      var localRaw = root.localStorage && root.localStorage.getItem(storageKey);
      if (localRaw) payload = mergeGridStorePayload(payload, JSON.parse(localRaw));
    } catch (err5) {}
  });
  try {
    var shared = labSharedSessionStorage().getItem(storageKey);
    if (shared) payload = mergeGridStorePayload(payload, JSON.parse(shared));
  } catch (err6) {}
  return payload;
}
function clearAllGridPending(storageKey) {
  labSharedSessionStorage().removeItem(storageKey);
  labGridPendingStoreClear(storageKey);
  var roots = [];
  try { roots.push(window.top); } catch (err) {}
  try { if (window.parent && window.parent !== window) roots.push(window.parent); } catch (err2) {}
  roots.push(window);
  roots.forEach(function (root) {
    if (!root) return;
    try {
      if (root.sessionStorage) root.sessionStorage.removeItem(storageKey);
    } catch (err3) {}
    try {
      if (root.localStorage) root.localStorage.removeItem(storageKey);
    } catch (err4) {}
  });
}
"""
