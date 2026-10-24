/**
 * VIEW: App
 * ─────────
 * Root component. Subscribes to the store and renders the active tab.
 * Zero direct fetch calls, zero direct photoshop imports.
 */

import React, { useState, useEffect } from "react";
import { store, A } from "../core/store.js";
import { OfflineBanner } from "./components/OfflineBanner.jsx";
import { SetupPanel }    from "./panels/SetupPanel.jsx";
import { PlanRunPanel }  from "./panels/PlanRunPanel.jsx";

export function App() {
  const [state, setState] = useState(store.getState());

  useEffect(() => {
    // Subscribe to store updates
    const unsub = store.subscribe(setState);
    return unsub;
  }, []);

  const { activeTab, banners, connectionStatus, connectionError, warnings } = state;

  function setTab(tab) {
    store.dispatch({ type: A.SET_ACTIVE_TAB, payload: tab });
  }

  return (
    <div className="r-shell">
      {/* Priority banners: BLOCKED > OFFLINE > warnings */}
      <OfflineBanner
        status={connectionStatus}
        error={connectionError}
        warnings={warnings}
        banners={banners}
      />

      {/* Tab bar */}
      <div className="r-tab-bar">
        <button
          className={activeTab === "plan" ? "active" : ""}
          onClick={() => setTab("plan")}
          id="tab-plan"
        >
          Queue
        </button>
        <button
          className={activeTab === "setup" ? "active" : ""}
          onClick={() => setTab("setup")}
          id="tab-setup"
        >
          Settings
        </button>
      </div>

      {/* Active panel view */}
      {activeTab === "plan"  && <PlanRunPanel state={state} />}
      {activeTab === "setup" && <SetupPanel   state={state} />}
    </div>
  );
}
