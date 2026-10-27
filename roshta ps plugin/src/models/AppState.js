/**
 * MODEL: AppState
 * ───────────────
 * The single state shape the store holds. Pure data — no logic, no imports.
 * Every controller reads from and writes to slices of this object.
 */

/**
 * Returns a fresh default AppState.
 * @returns {Object}
 */
export function makeDefaultState() {
  return {
    // ── Connection ────────────────────────────────────────────────────────
    /** @type {"OFFLINE"|"CONNECTING"|"ONLINE"|"BLOCKED"} */
    connectionStatus: "OFFLINE",
    serverVersion:    null,   // string | null
    serverUptime:     null,   // seconds | null
    photoshopDetected: false,
    bootId:           null,   // string — changes on server restart
    /** @type {{code:string, message:string}|null} */
    connectionError:  null,

    // ── Server mode (from /state) ─────────────────────────────────────────
    /** @type {"idle"|"running"|"pausing"|"paused"|"stopped"} */
    serverMode: "idle",
    stateRev:   0,            // integer revision counter

    // ── Plan tree & batches (from /state) ─────────────────────────────────
    planTree: [],             // PlanNode[]
    batches:  [],             // BatchCard[]
    counters: { ready: 0, waiting: 0, done: 0, flagged: 0 },
    warnings: [],             // string[]
    csvLocked: false,

    // ── Resume summary (from /health) ─────────────────────────────────────
    /**
     * null | { interrupted_batches: BatchCard[], recommendation: string }
     */
    resumeSummary: null,

    // ── Run-loop state machine (plugin-owned) ─────────────────────────────
    /**
     * @type {"OFFLINE"|"CONNECTING"|"IDLE"|"WAITING"|"EXECUTING"|
     *        "REPORTING"|"GAP"|"BLOCKED"}
     */
    runLoopState: "OFFLINE",
    /** @type {Object|null} active job contract from /lease */
    activeJob:    null,
    /** @type {number|null} gap countdown target timestamp (ms epoch) */
    gapTargetMs:  null,
    jobsCompletedThisSession: 0,

    // ── Settings ──────────────────────────────────────────────────────────
    settings: makeDefaultSettings(),

    // ── Activity feed ─────────────────────────────────────────────────────
    /** @type {Array<{time:string, clinic:string, batchNumber:string, status:string}>} */
    activityFeed: [],

    // ── After-Finishing ───────────────────────────────────────────────────
    afterFinishAction:          "nothing",
    afterFinishArmed:           false,
    afterFinishTargetMs:        null,
    afterFinishCountdownSeconds: 60,

    // ── UI ephemeral ──────────────────────────────────────────────────────
    /** @type {"files"|"batches"} */
    planView:       "batches",
    activeTab:      "plan",    // "plan" | "setup"
    logsDrawerOpen: false,
    /** @type {null|{type:string,title:string,body:string,actions:any[]}} */
    activeDialog:   null,
    /** @type {Array<{id:string, type:string, message:string}>} */
    banners:        [],
  };
}

/**
 * Default settings — mirrors server /settings keys plus plugin-owned fields.
 * @returns {Object}
 */
export function makeDefaultSettings() {
  return {
    // Plugin-owned (stored in UXP secure storage)
    host:  "127.0.0.1",
    port:  49200,
    token: "roshta-dev-token-secret", // default dev token matching server

    // Server-owned folders
    source_folder:    "",
    finished_folder:  "",
    print_folder:     "",
    tracker_folder:   "",
    rip_hot_folder:   "",

    // Naming template
    naming_template:  "[Clinic]_[Paper_Weight]_[Batch_No]_[Type]",

    // Output formats
    print_format:     "tif",   // "tif"|"jpg"
    tracker_format:   "tif",
    tiff_compression: "lzw",   // "none"|"lzw"|"zip"
    jpeg_quality:     85,
    embed_profile:    true,
    tracker_enabled:  true,
    slip_enabled:     false,

    // Canvas (locked while running)
    canvas_width_cm:  50,
    canvas_height_cm: 34,
    dpi:              300,
    color_profile:    "ISO Coated v2 (ECI)",
    watermark_opacity: 0.18,
    watermark_text:   "",      // defaults to clinic name on server

    // Recovery
    recovery_protocol: "auto_restart",  // "auto_restart"|"flag_for_review"
    min_free_disk_gb:  5,

    // Plugin behavior
    gap_between_jobs_seconds: 0,
    pause_after_n_batches:    0,  // 0 = off
  };
}
