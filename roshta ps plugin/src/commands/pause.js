/**
 * CONTROLLER: commands/pause
 * ──────────────────────────
 * Photoshop Plugins-menu command: "Roshta — Pause Queue"
 * Shares context with the panel — calls the same core module.
 */

import { postControl } from "../api/endpoints.js";
import { store, A } from "../core/store.js";

export async function pauseCommand() {
  const { serverMode, runLoopState } = store.getState();

  if (serverMode !== "running") {
    console.log("[cmd:pause] Server is not running — ignoring.");
    return;
  }

  // Optimistically reflect pausing intent in UI immediately
  store.dispatch({ type: A.SET_RUN_LOOP, payload: { runLoopState: "IDLE" } });

  const res = await postControl("pause");
  if (!res.ok) {
    console.warn("[cmd:pause] /control pause failed:", res.error);
  }
  // The poller will confirm the actual mode via /state
}
