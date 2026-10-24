/**
 * MODEL: BatchPlan
 * ────────────────
 * Type definitions and a normalizer that maps raw /state server JSON
 * into clean, typed objects the UI can rely on.
 * No logic, no side effects.
 */

/**
 * Normalize raw /state response into typed plan tree and batch arrays.
 *
 * @param {Object} raw  - raw JSON from server GET /state
 * @returns {{ planTree: PlanNode[], batches: BatchCard[], counters: Object, warnings: string[], csvLocked: boolean }}
 */
export function normalizeStateResponse(raw) {
  if (!raw || typeof raw !== "object") {
    return { planTree: [], batches: [], counters: makeDefaultCounters(), warnings: [], csvLocked: false };
  }

  const planTree  = normalizePlanTree(raw.plan_tree  || raw.tree || []);
  const batches   = normalizeBatches(raw.batches     || []);
  const counters  = normalizeCounters(raw.counters   || raw.stats || {});
  const warnings  = Array.isArray(raw.warnings) ? raw.warnings : [];
  const csvLocked = Boolean(raw.csv_locked);

  return { planTree, batches, counters, warnings, csvLocked };
}

/**
 * Recursively normalize a plan tree from the server.
 * @param {Array} nodes
 * @returns {PlanNode[]}
 */
function normalizePlanTree(nodes) {
  if (!Array.isArray(nodes)) return [];
  return nodes.map(n => ({
    id:         String(n.id || ""),
    type:       n.type || "file",          // "clinic"|"paper"|"file"
    name:       String(n.name || ""),
    status:     n.status || "waiting",     // "finished"|"processing"|"waiting"|"needs_review"
    skipped:    Boolean(n.is_skipped || n.skipped),
    skipReason: n.skip_reason || null,
    sizeBytes:  n.file_size   || null,
    mtime:      n.mtime       || null,
    children:   normalizePlanTree(n.children || []),
  }));
}

/**
 * Normalize batch cards from the server.
 * @param {Array} batches
 * @returns {BatchCard[]}
 */
function normalizeBatches(batches) {
  if (!Array.isArray(batches)) return [];
  return batches.map(b => ({
    batchKey:    String(b.batch_key  || b.batch_id  || ""),
    batchNumber: b.batch_number || null,   // null until leased
    clinic:      String(b.clinic      || ""),
    paperWeight: String(b.paper_weight || ""),
    state:       b.state   || b.status || "waiting",
    locked:      Boolean(b.locked),
    pinned:      Boolean(b.pinned),
    isForced:    Boolean(b.is_forced),
    slots:       normalizeSlots(b.slots || []),
    outputs:     normalizeOutputs(b.outputs || []),
    slipEnabled: Boolean(b.slip_enabled),
    slipWarning: b.slip_warning || null,
    warnings:    Array.isArray(b.warnings) ? b.warnings : [],
    errorCode:   b.error_code   || null,
    errorMessage:b.error_message|| null,
  }));
}

/**
 * @param {Array} slots
 * @returns {SlotEntry[]}
 */
function normalizeSlots(slots) {
  if (!Array.isArray(slots)) return [];
  return slots.map(s => ({
    slot:       s.slot       || "",         // "TL"|"TR"|"BL"|"BR"
    fileId:     s.file_id    || s.id || null,
    doctor:     s.doctor_name|| s.doctor || null,
    sourcePath: s.source_path|| s.path   || null,
    xCm:        s.x_cm != null ? s.x_cm : null,
    yCm:        s.y_cm != null ? s.y_cm : null,
    isEmpty:    Boolean(s.is_empty),
  }));
}

/**
 * @param {Array|Object} outputs
 * @returns {PlannedOutput[]}
 */
function normalizeOutputs(outputs) {
  if (Array.isArray(outputs)) {
    return outputs.map(o => ({
      type:        o.type        || "print",
      plannedName: o.planned_name|| o.name || "",
      format:      o.format      || "tif",
    }));
  }
  // Legacy: outputs = { print: {...}, tracker: {...} }
  const result = [];
  for (const [type, o] of Object.entries(outputs)) {
    if (o) result.push({ type, plannedName: o.planned_name || o.name || "", format: o.format || "tif" });
  }
  return result;
}

function normalizeCounters(raw) {
  return {
    ready:   Number(raw.ready   || 0),
    waiting: Number(raw.waiting || 0),
    done:    Number(raw.done    || 0),
    flagged: Number(raw.flagged || 0),
  };
}

export function makeDefaultCounters() {
  return { ready: 0, waiting: 0, done: 0, flagged: 0 };
}
