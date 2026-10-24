/**
 * MODEL: Job
 * ──────────
 * Type definitions and pure builder functions for job contracts and results.
 * No logic, no side effects, no imports from other layers.
 *
 * Actual server contract (confirmed from queue_service.py):
 * {
 *   batch_id, batch_number, lease_id,
 *   clinic, paper_weight, is_forced, lease_seconds,
 *   slots: [{ slot, doctor_name, source_path, x_cm, y_cm, is_empty }]
 * }
 */

/**
 * Validate that a raw server lease payload has minimum required fields.
 * @param {unknown} raw
 * @returns {{ valid: boolean, missing: string[] }}
 */
export function validateJobContract(raw) {
  if (!raw || typeof raw !== "object") {
    return { valid: false, missing: ["(not an object)"] };
  }
  const required = [
    "batch_id", "batch_number", "lease_id",
    "clinic", "paper_weight", "lease_seconds", "slots",
  ];
  const missing = required.filter(k => !(k in raw));
  return { valid: missing.length === 0, missing };
}

/**
 * Build a success result payload for POST /job-result.
 *
 * @param {Object} job        - the active job contract
 * @param {Object} info
 * @param {string}  info.printPath
 * @param {string}  [info.trackerPath]
 * @param {number}  info.durationMs
 * @param {number}  info.widthPx
 * @param {number}  info.heightPx
 * @param {number}  info.dpi
 * @returns {Object}
 */
export function buildSuccessResult(job, { printPath, trackerPath, durationMs, widthPx, heightPx, dpi }) {
  return {
    batch_id:            job.batch_id,
    lease_id:            job.lease_id,
    success:             true,
    print_temp_path:     printPath,
    tracker_temp_path:   trackerPath || undefined,
    duration_ms:         durationMs,
    expected_width_px:   widthPx,
    expected_height_px:  heightPx,
    expected_dpi:        dpi || 300,
  };
}

/**
 * Build a failure result payload for POST /job-result.
 *
 * @param {Object} job
 * @param {string} errorCode
 * @param {string} errorMessage
 * @param {number} [durationMs]
 * @returns {Object}
 */
export function buildErrorResult(job, errorCode, errorMessage, durationMs = 0) {
  return {
    batch_id:      job.batch_id,
    lease_id:      job.lease_id,
    success:       false,
    error_code:    errorCode,
    error_message: errorMessage,
    duration_ms:   durationMs,
  };
}

/**
 * Build a PLUGIN_RESTARTED failure for a job remembered across a reload.
 * @param {string} batchId
 * @param {string} leaseId
 * @returns {Object}
 */
export function buildRestartedResult(batchId, leaseId) {
  return {
    batch_id:      batchId,
    lease_id:      leaseId,
    success:       false,
    error_code:    "PLUGIN_RESTARTED",
    error_message: "Plugin reloaded mid-job; lease will be reclaimed by server reaper.",
    duration_ms:   0,
  };
}
