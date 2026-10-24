import test from "node:test";
import assert from "node:assert/strict";

import { apiFetch } from "../src/api/client.js";
import { store, A } from "../src/core/store.js";

// Mock global fetch for testing
let originalFetch = global.fetch;

test("API Client: injects X-Token on authenticated calls", async () => {
  store.dispatch({
    type: A.SET_SETTINGS,
    payload: { host: "127.0.0.1", port: 49200, token: "secret-token-123" },
  });

  let capturedUrl = null;
  let capturedHeaders = null;

  global.fetch = async (url, options) => {
    capturedUrl = url;
    capturedHeaders = options.headers;
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ mode: "idle" }),
    };
  };

  try {
    const res = await apiFetch("/state?rev=0");
    assert.equal(res.ok, true);
    assert.equal(capturedUrl, "http://127.0.0.1:49200/state?rev=0");
    assert.equal(capturedHeaders["X-Token"], "secret-token-123");
  } finally {
    global.fetch = originalFetch;
  }
});

test("API Client: /health does NOT send X-Token", async () => {
  let capturedHeaders = null;

  global.fetch = async (url, options) => {
    capturedHeaders = options.headers;
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ status: "ok" }),
    };
  };

  try {
    const res = await apiFetch("/health");
    assert.equal(res.ok, true);
    assert.equal(capturedHeaders["X-Token"], undefined);
  } finally {
    global.fetch = originalFetch;
  }
});

test("API Client: 401 Unauthorized dispatches BLOCKED status", async () => {
  global.fetch = async () => {
    return {
      ok: false,
      status: 401,
      text: async () => JSON.stringify({ error: { code: "UNAUTHORIZED", message: "Invalid token" } }),
    };
  };

  try {
    const res = await apiFetch("/state");
    assert.equal(res.ok, false);
    assert.equal(res.status, 401);
    const s = store.getState();
    assert.equal(s.connectionStatus, "BLOCKED");
  } finally {
    global.fetch = originalFetch;
  }
});

test("API Client: network error dispatches OFFLINE status", async () => {
  // First set status to ONLINE
  store.dispatch({ type: A.SET_CONNECTION, payload: { status: "ONLINE" } });

  global.fetch = async () => {
    throw new Error("Failed to connect to 127.0.0.1:49200");
  };

  try {
    const res = await apiFetch("/health");
    assert.equal(res.ok, false);
    assert.equal(res.status, 0);
    assert.equal(res.error.code, "NETWORK_ERROR");
    const s = store.getState();
    assert.equal(s.connectionStatus, "OFFLINE");
  } finally {
    global.fetch = originalFetch;
  }
});
