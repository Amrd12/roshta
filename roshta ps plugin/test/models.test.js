import test from "node:test";
import assert from "node:assert/strict";

import { makeDefaultState, makeDefaultSettings } from "../src/models/AppState.js";
import { validateSettings } from "../src/models/Settings.js";
import { validateJobContract, buildSuccessResult, buildErrorResult } from "../src/models/Job.js";
import { normalizeStateResponse } from "../src/models/BatchPlan.js";

test("AppState: creates default state shape with expected slices", () => {
  const state = makeDefaultState();
  assert.equal(state.connectionStatus, "OFFLINE");
  assert.equal(state.serverMode, "idle");
  assert.equal(state.stateRev, 0);
  assert.deepEqual(state.counters, { ready: 0, waiting: 0, done: 0, flagged: 0 });
  assert.equal(state.activeTab, "plan");
  assert.ok(state.settings);
  assert.equal(state.settings.host, "127.0.0.1");
  assert.equal(state.settings.port, 49200);
});

test("Settings: validation flags invalid ports and values", () => {
  const validPatch = { port: 49200, host: "localhost", dpi: 300, print_format: "tif" };
  assert.deepEqual(validateSettings(validPatch), {});

  const invalidPatch = { port: 99999, host: "", dpi: 50, print_format: "png" };
  const errors = validateSettings(invalidPatch);
  assert.ok(errors.port, "Should flag port > 65535");
  assert.ok(errors.host, "Should flag empty host");
  assert.ok(errors.dpi, "Should flag DPI < 72");
  assert.ok(errors.print_format, "Should flag non-tif/jpg print format");
});

test("Job: validateJobContract checks required fields", () => {
  const invalid = { batch_id: "B1" };
  const res1 = validateJobContract(invalid);
  assert.equal(res1.valid, false);
  assert.ok(res1.missing.includes("lease_id"));

  const valid = {
    batch_id: "B1",
    batch_number: "B0001",
    lease_id: "L1",
    clinic: "Al-Amal",
    paper_weight: "80g",
    lease_seconds: 600,
    slots: [],
  };
  const res2 = validateJobContract(valid);
  assert.equal(res2.valid, true);
  assert.equal(res2.missing.length, 0);
});

test("Job: buildSuccessResult and buildErrorResult format payloads", () => {
  const job = { batch_id: "B1", lease_id: "L1" };
  const success = buildSuccessResult(job, {
    printPath: "/tmp/print.tif",
    trackerPath: "/tmp/tracker.jpg",
    durationMs: 1200,
    widthPx: 5906,
    heightPx: 4016,
    dpi: 300,
  });
  assert.equal(success.success, true);
  assert.equal(success.print_temp_path, "/tmp/print.tif");
  assert.equal(success.expected_dpi, 300);

  const error = buildErrorResult(job, "INPUT_MISSING", "File not found", 500);
  assert.equal(error.success, false);
  assert.equal(error.error_code, "INPUT_MISSING");
});

test("BatchPlan: normalizeStateResponse normalizes raw server json", () => {
  const raw = {
    mode: "running",
    rev: 5,
    counters: { ready: 4, waiting: 2, done: 10, flagged: 0 },
    batches: [
      {
        batch_key: "BK1",
        clinic: "Clinic A",
        paper_weight: "80g",
        state: "ready",
        slots: [
          { slot: "TL", doctor_name: "Dr. Smith", is_empty: false },
          { slot: "TR", is_empty: true },
        ],
      },
    ],
    warnings: ["warning 1"],
    csv_locked: true,
  };

  const norm = normalizeStateResponse(raw);
  assert.equal(norm.counters.ready, 4);
  assert.equal(norm.batches.length, 1);
  assert.equal(norm.batches[0].clinic, "Clinic A");
  assert.equal(norm.batches[0].slots.length, 2);
  assert.equal(norm.batches[0].slots[0].doctor, "Dr. Smith");
  assert.equal(norm.warnings[0], "warning 1");
  assert.equal(norm.csvLocked, true);
});
