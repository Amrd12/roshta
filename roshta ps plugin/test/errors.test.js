import test from "node:test";
import assert from "node:assert/strict";

import { ErrorCode, mapPsErrorToCode, describeErrorCode } from "../src/core/errors.js";

test("Errors: mapPsErrorToCode maps common Photoshop errors", () => {
  assert.equal(mapPsErrorToCode("No such file or directory"), ErrorCode.INPUT_MISSING);
  assert.equal(mapPsErrorToCode("Could not open file"), ErrorCode.INPUT_OPEN_FAILED);
  assert.equal(mapPsErrorToCode("Wrong dimension or DPI"), ErrorCode.INPUT_SIZE_MISMATCH);
  assert.equal(mapPsErrorToCode("A font is missing: Arial"), ErrorCode.FONT_MISSING);
  assert.equal(mapPsErrorToCode("Color space profile failed"), ErrorCode.PROFILE_FAILED);
  assert.equal(mapPsErrorToCode("Could not save file to disk"), ErrorCode.SAVE_FAILED);
  assert.equal(mapPsErrorToCode("Disk is full (ENOSPC)"), ErrorCode.DISK_FULL);
  assert.equal(mapPsErrorToCode("Photoshop is busy in modal dialog"), ErrorCode.PS_BUSY);
  assert.equal(mapPsErrorToCode("Document closed unexpectedly"), ErrorCode.CANVAS_MODIFIED);
  assert.equal(mapPsErrorToCode("Something strange happened"), ErrorCode.UNKNOWN);
});

test("Errors: describeErrorCode provides readable text", () => {
  assert.equal(describeErrorCode(ErrorCode.INPUT_MISSING), "Source PSD file not found.");
  assert.equal(describeErrorCode(ErrorCode.CANVAS_MODIFIED), "Canvas was closed or modified between stages.");
});
