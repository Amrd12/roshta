/**
 * CONTROLLER: api/endpoints
 * ─────────────────────────
 * One async function per server endpoint.
 * All calls go through client.apiFetch — no direct fetch() here.
 *
 * No React, no photoshop, no uxp imports.
 */

import { apiFetch } from "./client.js";

// ── Health ────────────────────────────────────────────────────────────────

/**
 * GET /health — unauthenticated. Returns server vitals and resume summary.
 * @returns {Promise<ApiResponse>}
 */
export async function getHealth() {
  return apiFetch("/health", { method: "GET" });
}

// ── State ─────────────────────────────────────────────────────────────────

/**
 * GET /state?rev=N — returns 200 with full state or 304 if unchanged.
 * @param {number} rev  — last known revision
 * @returns {Promise<ApiResponse>}
 */
export async function getState(rev = 0) {
  return apiFetch(`/state?rev=${rev}`, { method: "GET" });
}

// ── Selection ─────────────────────────────────────────────────────────────

/**
 * POST /selection — skip or include plan tree nodes.
 * @param {{ rev: number, skip?: string[], include?: string[] }} payload
 * @returns {Promise<ApiResponse>}
 */
export async function postSelection(payload) {
  return apiFetch("/selection", {
    method: "POST",
    body:   JSON.stringify(payload),
  });
}

// ── Job ───────────────────────────────────────────────────────────────────

/**
 * POST /lease — request the next ready batch.
 * @param {{ force?: boolean, clinic?: string, paper_weight?: string }} [opts]
 * @returns {Promise<ApiResponse>}  200 = job contract, 204 = nothing ready
 */
export async function postLease(opts = {}) {
  return apiFetch("/lease", {
    method: "POST",
    body:   JSON.stringify(opts),
  });
}

/**
 * GET /next-job — spec-compliant alias for /lease.
 * @param {{ force?: boolean, clinic?: string, paper_weight?: string }} [opts]
 * @returns {Promise<ApiResponse>}
 */
export async function getNextJob(opts = {}) {
  const params = new URLSearchParams();
  if (opts.force)        params.set("force",        "1");
  if (opts.clinic)       params.set("clinic",       opts.clinic);
  if (opts.paper_weight) params.set("paper_weight", opts.paper_weight);
  const qs = params.toString() ? `?${params.toString()}` : "";
  return apiFetch(`/next-job${qs}`, { method: "GET" });
}

/**
 * POST /job-heartbeat — extend the active lease.
 * @param {string} batchId
 * @param {string} leaseId
 * @returns {Promise<ApiResponse>}
 */
export async function postHeartbeat(batchId, leaseId) {
  return apiFetch("/job-heartbeat", {
    method: "POST",
    body:   JSON.stringify({ batch_id: batchId, lease_id: leaseId }),
  });
}

/**
 * POST /job-result — report job completion or failure.
 * @param {Object} result  — built by Job model helpers
 * @returns {Promise<ApiResponse>}
 */
export async function postJobResult(result) {
  return apiFetch("/job-result", {
    method: "POST",
    body:   JSON.stringify(result),
  });
}

// ── Control ───────────────────────────────────────────────────────────────

/**
 * POST /control — send a queue control command.
 * @param {string} command  — "play"|"pause"|"stop"|"force_batch"|"retry"|
 *                            "mark_reviewed"|"rescan"
 * @param {Object} [params] — optional extra fields (e.g. { batch_id })
 * @returns {Promise<ApiResponse>}
 */
export async function postControl(command, params = {}) {
  return apiFetch("/control", {
    method: "POST",
    body:   JSON.stringify({ action: command, ...params }),
  });
}

// ── Settings ──────────────────────────────────────────────────────────────

/**
 * GET /settings
 * @returns {Promise<ApiResponse>}
 */
export async function getSettings() {
  return apiFetch("/settings", { method: "GET" });
}

/**
 * POST /settings — save a settings patch.
 * @param {Object} patch
 * @returns {Promise<ApiResponse>}
 */
export async function postSettings(patch) {
  return apiFetch("/settings", {
    method: "POST",
    body:   JSON.stringify(patch),
  });
}

// ── CSV ───────────────────────────────────────────────────────────────────

/**
 * GET /csv — download the session CSV.
 * Returns raw response; caller handles as download or file path.
 * @returns {Promise<ApiResponse>}
 */
export async function getCSV() {
  return apiFetch("/csv", { method: "GET" });
}
