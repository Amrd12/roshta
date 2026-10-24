/**
 * CONTROLLER: errors
 * ──────────────────
 * Error code enum and mapping utilities.
 * No imports from other layers.
 */

/** All error codes the plugin can report to the server via /job-result. */
export const ErrorCode = Object.freeze({
  INPUT_MISSING:       "INPUT_MISSING",
  INPUT_OPEN_FAILED:   "INPUT_OPEN_FAILED",
  INPUT_SIZE_MISMATCH: "INPUT_SIZE_MISMATCH",
  FONT_MISSING:        "FONT_MISSING",
  PROFILE_FAILED:      "PROFILE_FAILED",
  SAVE_FAILED:         "SAVE_FAILED",
  DISK_FULL:           "DISK_FULL",
  PS_BUSY:             "PS_BUSY",
  CANVAS_MODIFIED:     "CANVAS_MODIFIED",
  PLUGIN_RESTARTED:    "PLUGIN_RESTARTED",
  UNKNOWN:             "UNKNOWN",
});

/**
 * Map a raw Photoshop / UXP error to a known error code.
 *
 * @param {Error|string} err
 * @returns {string}  one of ErrorCode values
 */
export function mapPsErrorToCode(err) {
  const msg = (err && (err.message || String(err))).toLowerCase();

  if (msg.includes("no such file") || msg.includes("file not found") || msg.includes("enoent")) {
    return ErrorCode.INPUT_MISSING;
  }
  if (msg.includes("could not open") || msg.includes("open failed")) {
    return ErrorCode.INPUT_OPEN_FAILED;
  }
  if (msg.includes("size mismatch") || msg.includes("wrong dimension")) {
    return ErrorCode.INPUT_SIZE_MISMATCH;
  }
  if (msg.includes("font") || msg.includes("typeface")) {
    return ErrorCode.FONT_MISSING;
  }
  if (msg.includes("profile") || msg.includes("color space")) {
    return ErrorCode.PROFILE_FAILED;
  }
  if (msg.includes("save") || msg.includes("write") || msg.includes("export")) {
    return ErrorCode.SAVE_FAILED;
  }
  if (msg.includes("disk") || msg.includes("enospc") || msg.includes("no space")) {
    return ErrorCode.DISK_FULL;
  }
  if (msg.includes("busy") || msg.includes("modal")) {
    return ErrorCode.PS_BUSY;
  }
  if (msg.includes("canvas") || msg.includes("document closed") || msg.includes("no document")) {
    return ErrorCode.CANVAS_MODIFIED;
  }

  return ErrorCode.UNKNOWN;
}

/**
 * Human-readable descriptions for banner display.
 * @param {string} code
 * @returns {string}
 */
export function describeErrorCode(code) {
  const map = {
    [ErrorCode.INPUT_MISSING]:       "Source PSD file not found.",
    [ErrorCode.INPUT_OPEN_FAILED]:   "Could not open source PSD.",
    [ErrorCode.INPUT_SIZE_MISMATCH]: "Source PSD has wrong dimensions or DPI.",
    [ErrorCode.FONT_MISSING]:        "A required font is not installed.",
    [ErrorCode.PROFILE_FAILED]:      "Colour profile assignment failed.",
    [ErrorCode.SAVE_FAILED]:         "Could not save output file.",
    [ErrorCode.DISK_FULL]:           "Disk is full — free space and retry.",
    [ErrorCode.PS_BUSY]:             "Photoshop is busy in a modal scope.",
    [ErrorCode.CANVAS_MODIFIED]:     "Canvas was closed or modified between stages.",
    [ErrorCode.PLUGIN_RESTARTED]:    "Plugin reloaded mid-job.",
    [ErrorCode.UNKNOWN]:             "An unexpected error occurred.",
  };
  return map[code] || `Unknown error: ${code}`;
}
