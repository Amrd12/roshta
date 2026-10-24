import test from "node:test";
import assert from "node:assert/strict";

import { store, A } from "../src/core/store.js";

test("Store: initial state is default", () => {
  const s = store.getState();
  assert.equal(s.connectionStatus, "OFFLINE");
  assert.equal(s.serverMode, "idle");
});

test("Store: SET_CONNECTION updates connection slice", () => {
  store.dispatch({
    type: A.SET_CONNECTION,
    payload: {
      status: "ONLINE",
      version: "1.2.0",
      uptime: 3600,
      bootId: "boot-xyz-123",
      photoshopDetected: true,
    },
  });

  const s = store.getState();
  assert.equal(s.connectionStatus, "ONLINE");
  assert.equal(s.serverVersion, "1.2.0");
  assert.equal(s.serverUptime, 3600);
  assert.equal(s.bootId, "boot-xyz-123");
  assert.equal(s.photoshopDetected, true);
});

test("Store: SET_SERVER_STATE updates mode, rev, counters and batches", () => {
  store.dispatch({
    type: A.SET_SERVER_STATE,
    payload: {
      mode: "running",
      rev: 42,
      counters: { ready: 1, waiting: 3, done: 5, flagged: 0 },
      batches: [{ batchKey: "B1" }],
      warnings: ["disk space low"],
      csvLocked: false,
    },
  });

  const s = store.getState();
  assert.equal(s.serverMode, "running");
  assert.equal(s.stateRev, 42);
  assert.equal(s.counters.ready, 1);
  assert.equal(s.batches.length, 1);
  assert.equal(s.warnings[0], "disk space low");
});

test("Store: subscribe notifies listeners of state updates", () => {
  let callCount = 0;
  let lastTab = null;

  const unsub = store.subscribe((newState) => {
    callCount++;
    lastTab = newState.activeTab;
  });

  store.dispatch({ type: A.SET_ACTIVE_TAB, payload: "setup" });
  assert.equal(callCount, 1);
  assert.equal(lastTab, "setup");

  unsub();
  store.dispatch({ type: A.SET_ACTIVE_TAB, payload: "plan" });
  assert.equal(callCount, 1, "Unsubscribed listener should not be called again");
});
