/**
 * CONTROLLER: store
 * ─────────────────
 * Singleton state store with a pure reducer.
 * No React, no photoshop, no uxp imports.
 *
 * Usage:
 *   import { store } from "./store.js";
 *   store.dispatch({ type: "SET_CONNECTION", payload: { status: "ONLINE" } });
 *   const state = store.getState();
 *   const unsub = store.subscribe(state => console.log(state));
 */

import { makeDefaultState } from "../models/AppState.js";
import { makeDefaultCounters } from "../models/BatchPlan.js";

// ── Action types ──────────────────────────────────────────────────────────
export const A = Object.freeze({
  // Connection
  SET_CONNECTION:    "SET_CONNECTION",
  // Server state (from /state polling)
  SET_SERVER_STATE:  "SET_SERVER_STATE",
  // Resume summary (from /health)
  SET_RESUME_SUMMARY:"SET_RESUME_SUMMARY",
  // Run loop
  SET_RUN_LOOP:      "SET_RUN_LOOP",
  // Active job
  SET_ACTIVE_JOB:    "SET_ACTIVE_JOB",
  // Settings
  SET_SETTINGS:      "SET_SETTINGS",
  // Activity feed
  ADD_FEED_ROW:      "ADD_FEED_ROW",
  CLEAR_FEED:        "CLEAR_FEED",
  // After-Finishing
  SET_AFTER_FINISH:  "SET_AFTER_FINISH",
  // UI
  SET_ACTIVE_TAB:    "SET_ACTIVE_TAB",
  SET_PLAN_VIEW:     "SET_PLAN_VIEW",
  TOGGLE_LOGS:       "TOGGLE_LOGS",
  SET_DIALOG:        "SET_DIALOG",
  SET_BANNERS:       "SET_BANNERS",
  ADD_BANNER:        "ADD_BANNER",
  REMOVE_BANNER:     "REMOVE_BANNER",
});

// ── Pure reducer ──────────────────────────────────────────────────────────
function reducer(state, action) {
  const { type, payload } = action;

  switch (type) {

    case A.SET_CONNECTION:
      return {
        ...state,
        connectionStatus:  payload.status !== undefined ? payload.status : state.connectionStatus,
        connectionError:   payload.error !== undefined ? payload.error : (payload.status !== "BLOCKED" ? null : state.connectionError),
        serverVersion:     payload.version !== undefined ? payload.version : state.serverVersion,
        serverUptime:      payload.uptime !== undefined ? payload.uptime : state.serverUptime,
        bootId:            payload.bootId !== undefined ? payload.bootId : state.bootId,
        photoshopDetected: payload.photoshopDetected !== undefined ? payload.photoshopDetected : state.photoshopDetected,
      };

    case A.SET_SERVER_STATE:
      return {
        ...state,
        serverMode: payload.mode !== undefined ? payload.mode : state.serverMode,
        stateRev:   payload.rev !== undefined ? payload.rev : state.stateRev,
        planTree:   payload.planTree !== undefined ? payload.planTree : state.planTree,
        batches:    payload.batches !== undefined ? payload.batches : state.batches,
        counters:   payload.counters !== undefined ? payload.counters : state.counters,
        warnings:   payload.warnings !== undefined ? payload.warnings : state.warnings,
        csvLocked:  payload.csvLocked != null ? payload.csvLocked : state.csvLocked,
      };

    case A.SET_RESUME_SUMMARY:
      return { ...state, resumeSummary: payload };

    case A.SET_RUN_LOOP:
      return {
        ...state,
        runLoopState:              payload.runLoopState !== undefined ? payload.runLoopState : state.runLoopState,
        gapTargetMs:               payload.gapTargetMs !== undefined ? payload.gapTargetMs : state.gapTargetMs,
        jobsCompletedThisSession:  payload.jobsCompletedThisSession !== undefined ? payload.jobsCompletedThisSession : state.jobsCompletedThisSession,
      };

    case A.SET_ACTIVE_JOB:
      return { ...state, activeJob: payload };

    case A.SET_SETTINGS:
      return { ...state, settings: { ...state.settings, ...payload } };

    case A.ADD_FEED_ROW:
      return { ...state, activityFeed: [payload, ...state.activityFeed].slice(0, 500) };

    case A.CLEAR_FEED:
      return { ...state, activityFeed: [] };

    case A.SET_AFTER_FINISH:
      return {
        ...state,
        afterFinishAction:           payload.action !== undefined ? payload.action : state.afterFinishAction,
        afterFinishArmed:            payload.armed != null ? payload.armed : state.afterFinishArmed,
        afterFinishTargetMs:         payload.targetMs !== undefined ? payload.targetMs : state.afterFinishTargetMs,
        afterFinishCountdownSeconds: payload.countdownSeconds !== undefined ? payload.countdownSeconds : state.afterFinishCountdownSeconds,
      };

    case A.SET_ACTIVE_TAB:
      return { ...state, activeTab: payload };

    case A.SET_PLAN_VIEW:
      return { ...state, planView: payload };

    case A.TOGGLE_LOGS:
      return { ...state, logsDrawerOpen: !state.logsDrawerOpen };

    case A.SET_DIALOG:
      return { ...state, activeDialog: payload };

    case A.SET_BANNERS:
      return { ...state, banners: payload };

    case A.ADD_BANNER: {
      // Deduplicate by id
      const exists = state.banners.some(b => b.id === payload.id);
      if (exists) return state;
      return { ...state, banners: [...state.banners, payload] };
    }

    case A.REMOVE_BANNER:
      return { ...state, banners: state.banners.filter(b => b.id !== payload) };

    default:
      return state;
  }
}

// ── Store singleton ───────────────────────────────────────────────────────
function createStore(initialState) {
  let state = initialState;
  const listeners = new Set();

  function getState() { return state; }

  function dispatch(action) {
    if (!action || typeof action.type !== "string") {
      console.warn("[store] Invalid action:", action);
      return;
    }
    const next = reducer(state, action);
    if (next !== state) {
      state = next;
      listeners.forEach(fn => { try { fn(state); } catch(e) { console.error("[store] listener error:", e); } });
    }
  }

  function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return { getState, dispatch, subscribe };
}

export const store = createStore(makeDefaultState());
