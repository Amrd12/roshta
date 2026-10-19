-- 0. Schema Version Tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT UNIQUE NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_version (filename) VALUES ('init.sql');

-- 1. Staging Queue: Incoming stable files from disk
CREATE TABLE IF NOT EXISTS holding_files (
    id TEXT PRIMARY KEY,
    file_path TEXT UNIQUE NOT NULL,
    clinic TEXT NOT NULL,
    paper_weight TEXT NOT NULL,
    doctor_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_skipped INTEGER DEFAULT 0,
    status TEXT DEFAULT 'waiting'
);
CREATE INDEX IF NOT EXISTS idx_holding_group ON holding_files(clinic, paper_weight, is_skipped);

-- 2. Production Batches
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    batch_number TEXT UNIQUE,
    lease_id TEXT,
    clinic TEXT NOT NULL,
    paper_weight TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER DEFAULT 1,
    slot_count INTEGER DEFAULT 4,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    leased_at TIMESTAMP,
    lease_expires_at TIMESTAMP,
    completed_at TIMESTAMP,
    duration_ms INTEGER,
    print_temp_path TEXT,
    tracker_temp_path TEXT,
    print_final_path TEXT,
    tracker_final_path TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_batches_lease ON batches(lease_id);

-- 3. Quadrant Allocations
CREATE TABLE IF NOT EXISTS batch_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    doctor_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    finished_path TEXT,
    x_cm REAL NOT NULL,
    y_cm REAL NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

-- 4. Sticky Folder Exclusions
CREATE TABLE IF NOT EXISTS folder_exclusions (
    folder_path TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. Address Book
CREATE TABLE IF NOT EXISTS address_book (
    clinic_name TEXT PRIMARY KEY,
    address_line TEXT NOT NULL,
    phone TEXT,
    notes TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. System Key-Value State
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
