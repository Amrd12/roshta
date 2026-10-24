/**
 * VIEW: SetupPanel
 * ────────────────
 * Settings only — allows configuring connection, secure token storage,
 * and plugin-side preferences (gap between jobs, pause after N).
 */

import React, { useState, useEffect } from "react";
import { store, A } from "../../core/store.js";
import { saveConfig, loadConfig } from "../../core/config.js";
import { poller } from "../../core/poller.js";
import { getHealth, getState } from "../../api/endpoints.js";
import { validateSettings } from "../../models/Settings.js";

export function SetupPanel({ state }) {
  const { settings, connectionStatus, serverVersion, serverUptime, bootId, photoshopDetected } = state;

  const [host, setHost] = useState(settings.host || "127.0.0.1");
  const [port, setPort] = useState(settings.port || 49200);
  const [token, setToken] = useState(settings.token || "");
  const [gap, setGap] = useState(settings.gap_between_jobs_seconds || 0);
  const [pauseN, setPauseN] = useState(settings.pause_after_n_batches || 0);

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [saveStatus, setSaveStatus] = useState(null);
  const [errors, setErrors] = useState({});

  useEffect(() => {
    setHost(settings.host || "127.0.0.1");
    setPort(settings.port || 49200);
    setToken(settings.token || "");
    setGap(settings.gap_between_jobs_seconds || 0);
    setPauseN(settings.pause_after_n_batches || 0);
  }, [settings]);

  async function handleTestConnection() {
    setTesting(true);
    setTestResult(null);

    try {
      const healthRes = await getHealth();
      if (!healthRes.ok) {
        const msg = (healthRes.error && healthRes.error.message) || "Connection failed";
        setTestResult({
          ok: false,
          message: `Cannot reach server: ${msg}`,
        });
        setTesting(false);
        return;
      }

      // Health succeeded, now test authentication with token
      const stateRes = await getState(0);
      if (stateRes.status === 401) {
        setTestResult({
          ok: false,
          message: "Server is UP, but token is INVALID (401 Unauthorized). Check token and save.",
        });
      } else if (!stateRes.ok && stateRes.status !== 304) {
        const msg = (stateRes.error && stateRes.error.message) || `HTTP ${stateRes.status}`;
        setTestResult({
          ok: false,
          message: `State check failed: ${msg}`,
        });
      } else {
        const ver = (healthRes.data && healthRes.data.version) || "1.0";
        const mode = (stateRes.data && stateRes.data.mode) || "ready";
        setTestResult({
          ok: true,
          message: `Connected successfully! (Server v${ver}, Mode: ${mode})`,
        });
      }
    } catch (e) {
      setTestResult({ ok: false, message: e.message });
    } finally {
      setTesting(false);
    }
  }

  async function handleSave(e) {
    if (e && e.preventDefault) e.preventDefault();
    setSaveStatus(null);

    const patch = {
      host: host.trim(),
      port: Number(port),
      token: token.trim(),
      gap_between_jobs_seconds: Number(gap),
      pause_after_n_batches: Number(pauseN),
    };

    const validationErrors = validateSettings(patch);
    if (Object.keys(validationErrors).length > 0) {
      setErrors(validationErrors);
      return;
    }
    setErrors({});

    try {
      await saveConfig(patch);
      store.dispatch({ type: A.SET_SETTINGS, payload: patch });
      poller.kick();
      setSaveStatus("Settings saved & poller refreshed.");
      setTimeout(() => setSaveStatus(null), 4000);
    } catch (err) {
      setSaveStatus(`Failed to save: ${err.message}`);
    }
  }

  const dotClass =
    connectionStatus === "ONLINE"
      ? "r-dot r-dot--online"
      : connectionStatus === "BLOCKED"
      ? "r-dot r-dot--blocked"
      : connectionStatus === "CONNECTING"
      ? "r-dot r-dot--connecting"
      : "r-dot r-dot--offline";

  return (
    <div className="r-panel">
      {/* ── Connection Status ────────────────────────────────────────────── */}
      <div className="r-section">
        <div className="r-section-header">
          <span className={dotClass} />
          <span>Server Connection: {connectionStatus}</span>
        </div>
        <div className="r-section-body">
          {connectionStatus === "ONLINE" && (
            <div style={{ marginBottom: "10px", fontSize: "11px", color: "var(--roshta-text-muted)" }}>
              <div>Version: <strong>{serverVersion || "—"}</strong></div>
              <div>Uptime: <strong>{serverUptime != null ? `${Math.floor(serverUptime / 60)}m ${serverUptime % 60}s` : "—"}</strong></div>
              <div>Boot ID: <strong>{bootId || "—"}</strong></div>
              <div>Photoshop Process: <strong>{photoshopDetected ? "Detected by service" : "Not yet detected"}</strong></div>
            </div>
          )}

          <form onSubmit={handleSave}>
            <div className="r-field">
              <label className="r-label">Server Host</label>
              <input
                className={`r-input ${errors.host ? "r-input--error" : ""}`}
                value={host}
                onChange={(e) => setHost(e.target.value)}
                placeholder="127.0.0.1"
              />
              {errors.host && <div className="r-field-error">{errors.host}</div>}
            </div>

            <div className="r-field">
              <label className="r-label">Port</label>
              <input
                type="number"
                className={`r-input ${errors.port ? "r-input--error" : ""}`}
                value={port}
                onChange={(e) => setPort(e.target.value)}
                placeholder="49200"
              />
              {errors.port && <div className="r-field-error">{errors.port}</div>}
            </div>

            <div className="r-field">
              <label className="r-label">
                Secret Token (X-Token)
                <span style={{ fontSize: "10px", marginLeft: "6px", color: "var(--roshta-text-dim)" }}>
                  (stored in UXP secure storage)
                </span>
              </label>
              <input
                type="password"
                className="r-input"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Enter server X-Token"
              />
            </div>

            <div style={{ display: "flex", gap: "8px", marginTop: "12px" }}>
              <button
                type="button"
                className="r-btn"
                onClick={handleTestConnection}
                disabled={testing}
              >
                {testing ? "Testing..." : "Test Connection"}
              </button>
              <button type="submit" className="r-btn r-btn--primary">
                Save & Apply
              </button>
            </div>
          </form>

          {testResult && (
            <div
              style={{
                marginTop: "10px",
                padding: "6px 8px",
                borderRadius: "3px",
                fontSize: "11px",
                background: testResult.ok ? "#1a3a1a" : "#3a2020",
                color: testResult.ok ? "var(--roshta-success)" : "var(--roshta-danger)",
              }}
            >
              {testResult.message}
            </div>
          )}

          {saveStatus && (
            <div style={{ marginTop: "10px", fontSize: "11px", color: "var(--roshta-success)" }}>
              {saveStatus}
            </div>
          )}
        </div>
      </div>

      {/* ── Advanced / Plugin Behavior ───────────────────────────────────── */}
      <div className="r-section">
        <div className="r-section-header">
          <span>Plugin Automation Behavior</span>
        </div>
        <div className="r-section-body">
          <div className="r-field">
            <label className="r-label">Gap Between Batches (seconds)</label>
            <input
              type="number"
              className="r-input"
              value={gap}
              onChange={(e) => setGap(e.target.value)}
              min="0"
              placeholder="0 (no wait)"
            />
          </div>

          <div className="r-field">
            <label className="r-label">Pause After Every N Batches (0 = off)</label>
            <input
              type="number"
              className="r-input"
              value={pauseN}
              onChange={(e) => setPauseN(e.target.value)}
              min="0"
              placeholder="0"
            />
          </div>

          <button type="button" className="r-btn r-btn--sm" onClick={handleSave}>
            Update Automation Settings
          </button>
        </div>
      </div>
    </div>
  );
}
