/**
 * VIEW: PlanRunPanel
 * ──────────────────
 * Centre panel: displays live server queue, server mode, counters,
 * and quick queue control commands (Play / Pause / Rescan).
 */

import React, { useState } from "react";
import { postControl } from "../../api/endpoints.js";
import { Badge } from "../components/Badge.jsx";

export function PlanRunPanel({ state }) {
  const { serverMode, stateRev, counters, batches, warnings, connectionStatus } = state;
  const [controlLoading, setControlLoading] = useState(false);
  const [controlMsg, setControlMsg] = useState(null);

  async function handleControl(command) {
    setControlLoading(true);
    setControlMsg(null);
    try {
      const res = await postControl(command);
      if (!res.ok) {
        const msg = (res.error && res.error.message) || "Error";
        setControlMsg(`Control '${command}' failed: ${msg}`);
      } else {
        setControlMsg(`Sent '${command}' to server.`);
        setTimeout(() => setControlMsg(null), 3000);
      }
    } catch (e) {
      setControlMsg(`Error: ${e.message}`);
    } finally {
      setControlLoading(false);
    }
  }

  const isRunning = serverMode === "running";
  const isPausing = serverMode === "pausing";
  const isPaused  = serverMode === "paused";

  return (
    <div className="r-panel">
      {/* ── Mode & Live Status Header ─────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "6px 8px",
          background: "var(--roshta-bg-sunken)",
          border: "1px solid var(--roshta-border)",
          borderRadius: "var(--roshta-radius-sm)",
          marginBottom: "8px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <span style={{ fontSize: "11px", color: "var(--roshta-text-muted)" }}>Server Mode:</span>
          <Badge
            type={isRunning ? "processing" : isPaused ? "review" : "waiting"}
            label={serverMode ? serverMode.toUpperCase() : "OFFLINE"}
          />
        </div>
        <div style={{ fontSize: "10px", color: "var(--roshta-text-dim)" }}>
          rev: {stateRev}
        </div>
      </div>

      {/* ── Counters Row ─────────────────────────────────────────────────── */}
      <div className="r-counters" style={{ marginBottom: "8px" }}>
        <span>Ready: <span className="val">{counters.ready || 0}</span></span>
        <span>Waiting: <span className="val">{counters.waiting || 0}</span></span>
        <span>Done: <span className="val">{counters.done || 0}</span></span>
        {counters.flagged > 0 && (
          <span style={{ color: "var(--roshta-danger)" }}>
            Flagged: <span className="val">{counters.flagged}</span>
          </span>
        )}
      </div>

      {/* ── Quick Controls Bar ───────────────────────────────────────────── */}
      <div style={{ display: "flex", gap: "6px", marginBottom: "12px" }}>
        {isRunning ? (
          <button
            className="r-btn r-btn--sm"
            onClick={() => handleControl("pause")}
            disabled={controlLoading || connectionStatus !== "ONLINE"}
          >
            ⏸ Pause Queue
          </button>
        ) : (
          <button
            className="r-btn r-btn--primary r-btn--sm"
            onClick={() => handleControl("play")}
            disabled={controlLoading || isPausing || connectionStatus !== "ONLINE"}
          >
            ▶ {isPaused ? "Resume" : "Start Queue"}
          </button>
        )}

        <button
          className="r-btn r-btn--sm"
          onClick={() => handleControl("rescan")}
          disabled={controlLoading || connectionStatus !== "ONLINE"}
        >
          🔄 Rescan Folders
        </button>
      </div>

      {controlMsg && (
        <div style={{ fontSize: "11px", marginBottom: "8px", color: "var(--roshta-accent)" }}>
          {controlMsg}
        </div>
      )}

      {/* ── Queue Batches ────────────────────────────────────────────────── */}
      <div className="r-section">
        <div className="r-section-header">
          <span>Active Queue Batches ({batches ? batches.length : 0})</span>
        </div>
        <div className="r-section-body">
          {(!batches || batches.length === 0) ? (
            <div style={{ padding: "16px 8px", textAlign: "center", color: "var(--roshta-text-muted)", fontSize: "11px" }}>
              {connectionStatus !== "ONLINE"
                ? "Connect to server to view live batches."
                : "No active batches in queue. The scanner will plan batches as designs arrive in the source folder."}
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
              {batches.map((b, i) => {
                const filledSlots = (b.slots || []).filter(s => !s.isEmpty).length;
                return (
                  <div
                    key={b.batchKey || i}
                    style={{
                      background: "var(--roshta-bg-sunken)",
                      border: "1px solid var(--roshta-border)",
                      borderRadius: "var(--roshta-radius-sm)",
                      padding: "6px 8px",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                    }}
                  >
                    <div>
                      <div style={{ fontWeight: 600, fontSize: "11px" }}>
                        {b.clinic || "Clinic"} — {b.paperWeight || "Paper"}
                      </div>
                      <div style={{ fontSize: "10px", color: "var(--roshta-text-dim)" }}>
                        {b.batchNumber || b.batchKey || `Batch #${i + 1}`} ({filledSlots}/4 slots)
                      </div>
                    </div>
                    <Badge type={b.state} />
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
