/**
 * CONTROLLER: core/poller
 * ───────────────────────
 * Polls /health and /state on separate schedules.
 * Handles exponential back-off (1 → 2 → 4 → 10 s) on failure.
 * Detects boot_id change → drops rev cache → full re-fetch.
 *
 * No React, no photoshop, no uxp imports.
 *
 * Usage:
 *   import { poller } from "./poller.js";
 *   poller.start();
 *   poller.stop();
 */

import { store, A } from "./store.js";
import { getHealth, getState } from "../api/endpoints.js";
import { normalizeStateResponse } from "../models/BatchPlan.js";

// ── Constants ─────────────────────────────────────────────────────────────
const HEALTH_INTERVAL_MS  = 5000;    // poll /health every 5 s
const STATE_INTERVAL_MS   = 1500;    // poll /state every 1.5 s
const BACKOFF_STEPS_MS    = [1000, 2000, 4000, 10000];

// ── Module state ──────────────────────────────────────────────────────────
let _healthTimer  = null;
let _stateTimer   = null;
let _healthFails  = 0;
let _stateFails   = 0;
let _running      = false;
let _knownBootId  = null;

// ── Public API ────────────────────────────────────────────────────────────

export const poller = {
  start() {
    if (_running) return;
    _running = true;
    _scheduleHealth(0);
    _scheduleState(0);
  },

  stop() {
    _running = false;
    clearTimeout(_healthTimer);
    clearTimeout(_stateTimer);
    _healthTimer = null;
    _stateTimer  = null;
  },

  /** Force an immediate re-poll (e.g. after settings change). */
  kick() {
    if (!_running) return;
    clearTimeout(_healthTimer);
    clearTimeout(_stateTimer);
    _knownBootId = null;  // force full re-fetch
    _scheduleHealth(0);
    _scheduleState(0);
  },
};

// ── Health polling ────────────────────────────────────────────────────────

function _scheduleHealth(delayMs) {
  clearTimeout(_healthTimer);
  _healthTimer = setTimeout(_pollHealth, delayMs);
}

async function _pollHealth() {
  if (!_running) return;

  const res = await getHealth();

  if (!res.ok || !res.data) {
    _healthFails++;
    const delay = BACKOFF_STEPS_MS[Math.min(_healthFails - 1, BACKOFF_STEPS_MS.length - 1)];
    // Don't overwrite BLOCKED status
    const cur = store.getState().connectionStatus;
    if (cur !== "BLOCKED") {
      store.dispatch({ type: A.SET_CONNECTION, payload: { status: "OFFLINE" } });
    }
    _scheduleHealth(delay);
    return;
  }

  _healthFails = 0;
  const h = res.data;

  // Detect server restart: boot_id changed
  if (_knownBootId && h.boot_id && h.boot_id !== _knownBootId) {
    console.log("[poller] Server restarted (boot_id changed). Dropping rev cache.");
    store.dispatch({ type: A.SET_SERVER_STATE, payload: { rev: 0 } });
  }
  _knownBootId = h.boot_id || _knownBootId;

  store.dispatch({
    type: A.SET_CONNECTION,
    payload: {
      status:            "ONLINE",
      error:             null,
      bootId:            h.boot_id            || null,
      version:           h.version            || null,
      uptime:            h.uptime_seconds      || null,
      photoshopDetected: Boolean(h.photoshop_running),
    },
  });

  // Resume summary — show dialog once on first detection
  if (h.resume_summary && h.resume_summary.interrupted_batches && h.resume_summary.interrupted_batches.length > 0) {
    const cur = store.getState().resumeSummary;
    if (!cur) {
      store.dispatch({ type: A.SET_RESUME_SUMMARY, payload: h.resume_summary });
    }
  }

  _scheduleHealth(HEALTH_INTERVAL_MS);
}

// ── State polling ─────────────────────────────────────────────────────────

function _scheduleState(delayMs) {
  clearTimeout(_stateTimer);
  _stateTimer = setTimeout(_pollState, delayMs);
}

async function _pollState() {
  if (!_running) return;

  const cur = store.getState();

  // Don't bother polling state if we know we're offline or blocked
  if (cur.connectionStatus === "OFFLINE" || cur.connectionStatus === "BLOCKED") {
    _scheduleState(STATE_INTERVAL_MS * 2);
    return;
  }

  const res = await getState(cur.stateRev);

  if (res.status === 304) {
    // No change — just reschedule
    _stateFails = 0;
    _scheduleState(STATE_INTERVAL_MS);
    return;
  }

  if (!res.ok || !res.data) {
    _stateFails++;
    const delay = BACKOFF_STEPS_MS[Math.min(_stateFails - 1, BACKOFF_STEPS_MS.length - 1)];
    _scheduleState(delay);
    return;
  }

  _stateFails = 0;
  const { planTree, batches, counters, warnings, csvLocked } = normalizeStateResponse(res.data);

  store.dispatch({
    type: A.SET_SERVER_STATE,
    payload: {
      mode:      res.data.mode || res.data.server_mode || "idle",
      rev:       res.data.rev  || res.data.revision    || cur.stateRev,
      planTree,
      batches,
      counters,
      warnings,
      csvLocked,
    },
  });

  // Keep run-loop state in sync with server mode
  _syncRunLoopToServerMode(res.data.mode || res.data.server_mode);

  _scheduleState(STATE_INTERVAL_MS);
}

/**
 * When the server mode changes, ensure the run loop state stays coherent.
 * The full state machine lives in runLoop.js — this just handles the
 * server-driven transitions the poller observes.
 */
function _syncRunLoopToServerMode(serverMode) {
  const cur = store.getState();

  if (serverMode === "idle" || serverMode === "stopped" || serverMode === "paused") {
    if (cur.runLoopState === "WAITING") {
      store.dispatch({ type: A.SET_RUN_LOOP, payload: { runLoopState: "IDLE" } });
    }
  }
}
