/**
 * MODEL: Settings
 * ───────────────
 * Settings shape factory and field-level validator.
 * No logic, no imports from other layers.
 */

import { makeDefaultSettings } from "./AppState.js";
export { makeDefaultSettings };

/**
 * Validate a settings patch object.
 * Returns a map of { fieldName: errorMessage } for any invalid fields.
 * An empty object means all fields are valid.
 *
 * @param {Object} patch  - partial settings object to validate
 * @returns {{ [field: string]: string }}
 */
export function validateSettings(patch) {
  const errors = {};

  if ("port" in patch) {
    const p = Number(patch.port);
    if (!Number.isInteger(p) || p < 1024 || p > 65535) {
      errors.port = "Port must be an integer between 1024 and 65535.";
    }
  }

  if ("host" in patch) {
    if (!patch.host || typeof patch.host !== "string" || !patch.host.trim()) {
      errors.host = "Host must be a non-empty string.";
    }
  }

  if ("dpi" in patch) {
    const d = Number(patch.dpi);
    if (!Number.isInteger(d) || d < 72 || d > 1200) {
      errors.dpi = "DPI must be an integer between 72 and 1200.";
    }
  }

  if ("canvas_width_cm" in patch) {
    const w = Number(patch.canvas_width_cm);
    if (isNaN(w) || w <= 0 || w > 200) {
      errors.canvas_width_cm = "Canvas width must be between 1 and 200 cm.";
    }
  }

  if ("canvas_height_cm" in patch) {
    const h = Number(patch.canvas_height_cm);
    if (isNaN(h) || h <= 0 || h > 200) {
      errors.canvas_height_cm = "Canvas height must be between 1 and 200 cm.";
    }
  }

  if ("jpeg_quality" in patch) {
    const q = Number(patch.jpeg_quality);
    if (!Number.isInteger(q) || q < 1 || q > 100) {
      errors.jpeg_quality = "JPEG quality must be an integer between 1 and 100.";
    }
  }

  if ("watermark_opacity" in patch) {
    const o = Number(patch.watermark_opacity);
    if (isNaN(o) || o < 0 || o > 1) {
      errors.watermark_opacity = "Watermark opacity must be between 0 and 1.";
    }
  }

  if ("print_format" in patch) {
    if (!["tif", "jpg"].includes(patch.print_format)) {
      errors.print_format = "Print format must be 'tif' or 'jpg'.";
    }
  }

  if ("tracker_format" in patch) {
    if (!["tif", "jpg"].includes(patch.tracker_format)) {
      errors.tracker_format = "Tracker format must be 'tif' or 'jpg'.";
    }
  }

  if ("tiff_compression" in patch) {
    if (!["none", "lzw", "zip"].includes(patch.tiff_compression)) {
      errors.tiff_compression = "TIFF compression must be 'none', 'lzw', or 'zip'.";
    }
  }

  if ("naming_template" in patch) {
    const t = patch.naming_template || "";
    if (!t.includes("[Batch_No]") || !t.includes("[Type]")) {
      errors.naming_template = "Template must include [Batch_No] and [Type] tokens.";
    }
    if (/[\\/:*?"<>|]/.test(t.replace(/\[.*?\]/g, ""))) {
      errors.naming_template = "Template contains illegal filename characters.";
    }
  }

  if ("gap_between_jobs_seconds" in patch) {
    const g = Number(patch.gap_between_jobs_seconds);
    if (isNaN(g) || g < 0) {
      errors.gap_between_jobs_seconds = "Gap must be ≥ 0 seconds.";
    }
  }

  if ("pause_after_n_batches" in patch) {
    const n = Number(patch.pause_after_n_batches);
    if (!Number.isInteger(n) || n < 0) {
      errors.pause_after_n_batches = "Pause-after-N must be a non-negative integer (0 = off).";
    }
  }

  return errors;
}

/**
 * Merge a validated settings patch over the current settings.
 * Returns a new settings object (does not mutate).
 *
 * @param {Object} current
 * @param {Object} patch
 * @returns {Object}
 */
export function mergeSettings(current, patch) {
  return { ...current, ...patch };
}

/**
 * Settings that the server owns and that are locked while a job is running.
 * The plugin must not allow editing these in that state.
 */
export const SERVER_LOCKED_WHILE_RUNNING = [
  "canvas_width_cm", "canvas_height_cm", "dpi",
  "color_profile", "tiff_compression", "print_format",
  "tracker_format", "watermark_opacity",
];
