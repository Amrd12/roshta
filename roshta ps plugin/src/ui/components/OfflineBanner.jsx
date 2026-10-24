/**
 * VIEW: OfflineBanner
 * ───────────────────
 * Priority banners shown at the top of the panel:
 *   1. BLOCKED (invalid or missing token)
 *   2. OFFLINE (server unreachable, showing reconnecting state)
 *   3. CONNECTING (transient initial connection)
 *   4. Server warnings (e.g. csv_locked)
 */

import React from "react";
import { store, A } from "../../core/store.js";

export function OfflineBanner({ status, error, warnings = [], banners = [] }) {
  if (status === "BLOCKED") {
    return (
      <div className="r-banner r-banner--blocked">
        <span className="r-dot r-dot--blocked" />
        <div style={{ flex: 1 }}>
          <strong>Token Problem:</strong> {(error && error.message) || "Invalid or missing X-Token."}
        </div>
        <button
          className="r-btn r-btn--sm"
          onClick={() => store.dispatch({ type: A.SET_ACTIVE_TAB, payload: "setup" })}
        >
          Settings
        </button>
      </div>
    );
  }

  if (status === "OFFLINE") {
    const { settings } = store.getState();
    const host = settings.host || "127.0.0.1";
    const port = settings.port || 49200;
    return (
      <div className="r-banner r-banner--offline">
        <span className="r-dot r-dot--offline" />
        <div style={{ flex: 1 }}>
          <strong>Server Offline:</strong> Cannot reach Roshta service at {host}:{port}. Reconnecting...
        </div>
      </div>
    );
  }

  if (status === "CONNECTING") {
    return (
      <div className="r-banner r-banner--info">
        <span className="r-dot r-dot--connecting" />
        <div>Connecting to Roshta service...</div>
      </div>
    );
  }

  // Active server warnings (e.g. csv_locked)
  if (warnings && warnings.length > 0) {
    return (
      <div>
        {warnings.map((w, idx) => (
          <div key={idx} className="r-banner r-banner--warning">
            <span>⚠️</span>
            <div>{w}</div>
          </div>
        ))}
      </div>
    );
  }

  return null;
}
