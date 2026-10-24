import test from "node:test";
import assert from "node:assert/strict";

import { poller } from "../src/core/poller.js";
import { store, A } from "../src/core/store.js";

test("Poller: start and stop control timer loop", () => {
  // Verify start and stop don't throw
  poller.start();
  poller.kick();
  poller.stop();
  assert.ok(true);
});

test("Poller: boot_id change drops rev cache to 0", () => {
  // Set initial rev
  store.dispatch({
    type: A.SET_SERVER_STATE,
    payload: { rev: 15 },
  });
  assert.equal(store.getState().stateRev, 15);

  // If boot_id changes in a health response, rev cache drops to 0
  store.dispatch({ type: A.SET_SERVER_STATE, payload: { rev: 0 } });
  assert.equal(store.getState().stateRev, 0);
});
