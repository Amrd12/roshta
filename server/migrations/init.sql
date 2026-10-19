-- ============================================================
-- Roshta Server – Initial Schema (Migration v1)
-- ============================================================
-- NOTE: PRAGMA statements cannot live inside a CREATE TABLE
--       transaction block; they are applied by database.py at
--       connection time. They are documented here for reference.
--
--   PRAGMA journal_mode = WAL;
--   PRAGMA synchronous  = FULL;   -- Survive power loss (tiny write volume)
--   PRAGMA busy_timeout = 5000;   -- 5 s before "database is locked" error
--   PRAGMA foreign_keys = ON;

-- 0. MIGRATION TRACKING
-- (Created first by database.py before migrations run; harmless if exists)
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT UNIQUE NOT NULL,
    applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_version (filename) VALUES ('init.sql');

-- ============================================================
-- 1. STAGING QUEUE
--    Every stable PSD discovered on disk lives here until its
--    source file is physically moved to Finished/.
--    Files are NEVER deleted on promotion; they are marked
--    status='locked' + batch_id so the scanner ignores them.
-- ============================================================
CREATE TABLE IF NOT EXISTS holding_files (
    id              TEXT PRIMARY KEY,       -- SHA-1 of normalised file_path
    file_path       TEXT UNIQUE NOT NULL,   -- Absolute path on disk
    clinic          TEXT NOT NULL,
    paper_weight    TEXT NOT NULL,
    doctor_name     TEXT NOT NULL,
    file_size       INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    first_seen_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_skipped      INTEGER DEFAULT 0,      -- 1 = operator excluded
    status          TEXT DEFAULT 'waiting', -- 'waiting' | 'locked'
    batch_id        TEXT,                   -- Set during promotion; cleared after move
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_holding_group
    ON holding_files(clinic, paper_weight, status, is_skipped);
CREATE INDEX IF NOT EXISTS idx_holding_batch
    ON holding_files(batch_id);

-- ============================================================
-- 2. PRODUCTION BATCHES
--    Created atomically alongside batch_slots when a lease fires.
-- ============================================================
CREATE TABLE IF NOT EXISTS batches (
    id                  TEXT PRIMARY KEY,   -- e.g. "batch_20261019_001"
    batch_number        TEXT UNIQUE,        -- e.g. "B00142" (set at lease time)
    lease_id            TEXT,               -- UUID for the current active lease
    clinic              TEXT NOT NULL,
    paper_weight        TEXT NOT NULL,
    status              TEXT NOT NULL,      -- 'leased' | 'verifying' | 'finalizing'
                                            -- | 'done' | 'failed' | 'interrupted'
                                            -- | 'flagged'
    attempt_count       INTEGER DEFAULT 1,
    slot_count          INTEGER DEFAULT 4,
    is_forced           INTEGER DEFAULT 0,  -- Force-batch with <4 designs
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    leased_at           TIMESTAMP,
    lease_expires_at    TIMESTAMP,          -- Stored in DB; reaped by background thread
    completed_at        TIMESTAMP,
    duration_ms         INTEGER,
    print_temp_path     TEXT,
    tracker_temp_path   TEXT,
    print_final_path    TEXT,
    tracker_final_path  TEXT,
    csv_exported_at     TIMESTAMP,          -- NULL until row appended to master CSV
    slip_generated_at   TIMESTAMP,          -- NULL until PDF slip written
    error_message       TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_batches_lease  ON batches(lease_id);
CREATE INDEX IF NOT EXISTS idx_batches_csv    ON batches(csv_exported_at)
    WHERE csv_exported_at IS NULL;          -- Partial index speeds CSV retry scan

-- ============================================================
-- 3. QUADRANT SLOT ALLOCATIONS
-- ============================================================
CREATE TABLE IF NOT EXISTS batch_slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL,
    slot            TEXT NOT NULL,          -- 'TL' | 'TR' | 'BL' | 'BR'
    doctor_name     TEXT NOT NULL,
    source_path     TEXT NOT NULL,          -- Original path in Input folder
    finished_path   TEXT,                   -- Set after safe_move completes
    x_cm            REAL NOT NULL,
    y_cm            REAL NOT NULL,
    is_empty        INTEGER DEFAULT 0,      -- 1 = white-fill placeholder (force batch)
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_slots_batch ON batch_slots(batch_id);

-- ============================================================
-- 4. STICKY FOLDER EXCLUSIONS
--    Folder-level skips survive scanner restarts.
--    File-level skips live in holding_files.is_skipped.
-- ============================================================
CREATE TABLE IF NOT EXISTS folder_exclusions (
    folder_path TEXT PRIMARY KEY,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 5. SYSTEM STATE & ATOMIC COUNTERS
-- ============================================================
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Seed the batch number sequence counter (1-based; produces B00001 on first use)
INSERT OR IGNORE INTO system_state (key, value) VALUES ('next_batch_number', '1');

-- boot_id: Replaced by server on each startup so the plugin detects restarts
INSERT OR IGNORE INTO system_state (key, value) VALUES ('boot_id', '');
