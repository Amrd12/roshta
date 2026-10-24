/**
 * CONTROLLER: api/client
 * ──────────────────────
 * fetch() wrapper that injects X-Token, handles timeouts, and maps
 * HTTP responses to a structured result object.
 *
 * All side-effects on auth failure (401) and network failure are
 * handled here by dispatching to the store so the poller stays clean.
 *
 * No React, no photoshop, no uxp imports.
 */

import { store, A } from "../core/store.js";

const DEFAULT_TIMEOUT_MS = 8000;

/**
 * @typedef {Object} ApiResponse
 * @property {boolean} ok
 * @property {number}  status
 * @property {any}     data     — parsed JSON or null
 * @property {{ code: string, message: string }|null} error
 */

/**
 * Core fetch wrapper.
 *
 * @param {string} path              — e.g. "/health" or "/state?rev=5"
 * @param {RequestInit} [options]    — fetch options (method, body, etc.)
 * @param {number} [timeoutMs]
 * @returns {Promise<ApiResponse>}
 */
export async function apiFetch(path, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const { settings } = store.getState();
  const base = `http://${settings.host}:${settings.port}`;
  const url  = `${base}${path}`;

  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };

  // Inject token for every call except /health
  if (path !== "/health" && settings.token) {
    headers["X-Token"] = settings.token;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response;
  try {
    response = await fetch(url, {
      ...options,
      headers,
      signal: controller.signal,
    });
    clearTimeout(timer);
  } catch (err) {
    clearTimeout(timer);
    // Network error or timeout → OFFLINE
    const isTimeout = err.name === "AbortError";
    _dispatchOffline(isTimeout ? "Request timed out." : err.message);
    return { ok: false, status: 0, data: null, error: { code: "NETWORK_ERROR", message: err.message } };
  }

  // Parse body
  let data = null;
  try {
    const text = await response.text();
    if (text) data = JSON.parse(text);
  } catch (_) { /* non-JSON body */ }

  if (response.status === 401) {
    store.dispatch({
      type: A.SET_CONNECTION,
      payload: {
        status: "BLOCKED",
        error:  { code: "UNAUTHORIZED", message: "Invalid or missing X-Token. Update the token in Settings." },
      },
    });
    return { ok: false, status: 401, data, error: { code: "UNAUTHORIZED", message: "Unauthorized" } };
  }

  if (response.status >= 500) {
    const srvErr = (data && data.error) || { code: "SERVER_ERROR", message: `HTTP ${response.status}` };
    return { ok: false, status: response.status, data, error: srvErr };
  }

  return {
    ok:     response.ok,
    status: response.status,
    data,
    error:  response.ok ? null : ((data && data.error) || { code: "HTTP_ERROR", message: `HTTP ${response.status}` }),
  };
}

function _dispatchOffline(message) {
  const current = store.getState().connectionStatus;
  if (current === "ONLINE" || current === "CONNECTING") {
    store.dispatch({
      type: A.SET_CONNECTION,
      payload: { status: "OFFLINE", error: { code: "NETWORK_ERROR", message: message } },
    });
  }
}
