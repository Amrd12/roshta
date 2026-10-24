/**
 * CONTROLLER: commands/afterFinish
 * ─────────────────────────────────
 * Photoshop Plugins-menu command: "Roshta — Toggle After-Finishing"
 */

import { store, A } from "../core/store.js";

export function afterFinishCommand() {
  const { afterFinishArmed } = store.getState();
  store.dispatch({
    type: A.SET_AFTER_FINISH,
    payload: { armed: !afterFinishArmed },
  });
  console.log("[cmd:afterFinish] Armed:", !afterFinishArmed);
}
