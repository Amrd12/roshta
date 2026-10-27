/**
 * CONTROLLER: config
 * ──────────────────
 * Reads and writes plugin connection settings from UXP secure storage.
 * The token is NEVER logged, never placed in plain localStorage.
 *
 * Depends on: uxp (adapter — only file that imports uxp in the core layer).
 * Note: UXP secure storage is available via the `uxp` module's `storage` API.
 */

import { makeDefaultSettings } from "../models/AppState.js";

const KEY_HOST  = "roshta.host";
const KEY_PORT  = "roshta.port";
const KEY_TOKEN = "roshta.token";           // stored in secure storage
const KEY_GAP   = "roshta.gap";
const KEY_PAUSE_N = "roshta.pauseN";

let _secureStorage = null;
let _localStorage  = null;

/**
 * Lazily initialise storage references from the uxp module.
 * Called once before any read/write.
 */
function _initStorage() {
  if (_secureStorage) return;
  try {
    const storage = require("uxp").storage;
    _secureStorage = storage.secureStorage;
    _localStorage  = storage.localFileSystem;   // for non-sensitive prefs
  } catch (e) {
    console.warn("[config] UXP storage unavailable — running outside Photoshop?", e.message);
  }
}

/**
 * Load all connection settings.  The token is read from secure storage;
 * everything else from localStorage-equivalent.
 *
 * @returns {Promise<Object>}  merged settings object (only connection fields)
 */
export async function loadConfig() {
  _initStorage();
  const defaults = makeDefaultSettings();

  let host  = defaults.host;
  let port  = defaults.port;
  let token = defaults.token || "";
  let gap   = defaults.gap_between_jobs_seconds;
  let pauseN = defaults.pause_after_n_batches;

  if (_secureStorage) {
    try {
      const rawToken = await _secureStorage.getItem(KEY_TOKEN);
      if (rawToken) token = rawToken;
    } catch(e) {
      console.warn("[config] Could not read token from secure storage:", e.message);
    }
  }

  // Non-sensitive prefs stored as JSON in UXP app data
  try {
    const { localFileSystem: lfs, types: { domains } } = require("uxp").storage;
    const dataDir = await lfs.getDataFolder();
    const prefsFile = await dataDir.getEntry("roshta_prefs.json").catch(() => null);
    if (prefsFile) {
      const text = await prefsFile.read({ format: "utf8" }).catch(() => null);
      if (text) {
        const prefs = JSON.parse(text);
        host   = prefs.host !== undefined ? prefs.host : host;
        port   = Number(prefs.port !== undefined ? prefs.port : port);
        gap    = Number(prefs.gap !== undefined ? prefs.gap : gap);
        pauseN = Number(prefs.pauseN !== undefined ? prefs.pauseN : pauseN);
      }
    }
  } catch(e) {
    // Outside Photoshop or first run — use defaults silently
  }

  return { host, port, token, gap_between_jobs_seconds: gap, pause_after_n_batches: pauseN };
}

/**
 * Save connection settings.
 * Token → secure storage.  Everything else → prefs file.
 *
 * @param {Object} patch  — may include host, port, token, gap, pauseN
 * @returns {Promise<void>}
 */
export async function saveConfig(patch) {
  _initStorage();

  // Save token to secure storage
  if ("token" in patch && _secureStorage) {
    try {
      if (patch.token) {
        await _secureStorage.setItem(KEY_TOKEN, patch.token);
      } else {
        await _secureStorage.removeItem(KEY_TOKEN);
      }
    } catch(e) {
      console.warn("[config] Could not save token to secure storage:", e.message);
    }
  }

  // Save non-sensitive prefs
  try {
    const { localFileSystem: lfs } = require("uxp").storage;
    const dataDir = await lfs.getDataFolder();

    let existing = {};
    const prefsFile = await dataDir.getEntry("roshta_prefs.json").catch(() => null);
    if (prefsFile) {
      const text = await prefsFile.read({ format: "utf8" }).catch(() => "{}");
      existing = JSON.parse(text);
    }

    const updated = {
      ...existing,
      ...(patch.host   !== undefined ? { host:   patch.host }   : {}),
      ...(patch.port   !== undefined ? { port:   patch.port }   : {}),
      ...(patch.gap_between_jobs_seconds !== undefined ? { gap: patch.gap_between_jobs_seconds } : {}),
      ...(patch.pause_after_n_batches   !== undefined ? { pauseN: patch.pause_after_n_batches } : {}),
    };

    const file = prefsFile
      ? prefsFile
      : await dataDir.createFile("roshta_prefs.json", { overwrite: true });

    await file.write(JSON.stringify(updated, null, 2), { format: "utf8" });
  } catch(e) {
    console.warn("[config] Could not save prefs:", e.message);
  }
}

/**
 * Clear saved token (logout / reset).
 * @returns {Promise<void>}
 */
export async function clearToken() {
  _initStorage();
  if (_secureStorage) {
    try { await _secureStorage.removeItem(KEY_TOKEN); } catch(e) { /* ignore */ }
  }
}
