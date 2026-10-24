/**
 * CONTROLLER: commands/resume
 * ───────────────────────────
 * Photoshop Plugins-menu command: "Roshta — Resume Queue"
 */

import { postControl } from "../api/endpoints.js";
import { store, A } from "../core/store.js";

export async function resumeCommand() {
  const { serverMode } = store.getState();

  if (serverMode !== "paused" && serverMode !== "stopped" && serverMode !== "idle") {
    console.log("[cmd:resume] Nothing to resume (mode:", serverMode, ")");
    return;
  }

  const res = await postControl("play");
  if (!res.ok) {
    console.warn("[cmd:resume] /control play failed:", res.error);
  }
  // Poller will confirm via /state
}
