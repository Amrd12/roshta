# Server Implementation Plan: Prescription Printing Automation

This document defines the architectural specification and implementation roadmap for the **Roshta Python/Flask Service**.

**Revision history:**
- v1 – Initial plan (SQLite WAL, layered architecture)
- v2 – Applied pre-build review corrections: double-batch fix, FULL sync, async finalization, boot_id restart detection, crash-proof CSV, Arabic font embedding, scanner hardening, verifier tolerance, trimmed over-engineering.

---

## 1. Architectural Principles & Layering

The server follows a clean layered architecture with strict separation between pure business logic, the transactional service layer, external adapters, and HTTP controllers.

```
              [ HTTP Clients / Photoshop UXP Plugin ]
                                 │  (JSON / HTTP on 127.0.0.1)
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │     Controllers (Flask Blueprints hosted on Waitress)       │
  │   - Health, State, Jobs, Controls, Settings, CSV Export     │
  └──────────────────────────────┬──────────────────────────────┘
                                 │  (Invokes)
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │          Service Layer (Owns Thread Lock & Storage)         │
  │   - QueueService: Orchestrates jobs, leases & transactions  │
  │   - ScannerService: Non-blocking background poller (5 s)   │
  │   - JobFinalizerWorker: Async TIFF verify + file finalize   │
  └──────────────┬───────────────────────────────┬──────────────┘
                 │                               │
      (Operates Pure Domain)             (Drives Synchronously)
                 ▼                               ▼
  ┌─────────────────────────────┐ ┌─────────────────────────────┐
  │     Pure Domain Models      │ │   Single Source of Truth    │
  │   - Batch & Slot rules      │ │   - SQLite (WAL + FULL)     │
  │   - In-memory Plan Cache    │ │     • holding_files (Stage) │
  │   - Folder Exclusions       │ │     • batches (Committed)   │
  │   - Doctor Name Regex       │ └──────────────┬──────────────┘
  └─────────────────────────────┘                │ (After Commit)
                 │                               ▼
     (Uses via Interfaces)               ┌──────────────┐
                 ▼                        │ CSV Exporter │
  ┌─────────────────────────────────┐     │ (Retry-safe) │
  │            Adapters             │     └──────────────┘
  │   - FileSystem (Cross-drive)    │
  │   - TIFF Verifier (one fn)      │
  │   - Process / Power (psutil)    │
  │   - PDF Slip (ReportLab+Arabic) │
  └─────────────────────────────────┘
```

### 1.1 Layer Responsibilities

1. **Controllers (`server/controllers/`):**
   * Flask routes served via **`waitress`** (multi-threaded, robust on Windows, PyInstaller-friendly).
   * Extracts `X-Token`, parses JSON, maps domain exceptions to HTTP codes (401, 404, 409, 422, 423), renders views via `serializers.py`.

2. **Service Layer (`server/services/`):**
   * **`QueueService`:** Owns the primary re-entrant lock (`threading.RLock`). Serves `/state` from an **immutable in-memory snapshot** swapped atomically so polling never blocks. Mutating operations write synchronously to SQLite *before* replying.
   * **`ScannerService`:** Background daemon scanning every **5 seconds** using state-snapshot diffing, with two-tier file stability (size+mtime unchanged, then `open(rb)` succeeds).
   * **`JobFinalizerWorker`:** Single worker thread consuming a `queue.Queue`. `/job-result` enqueues a finalization task and returns `202 Accepted` in < 5 ms. The worker then verifies TIFFs, moves files, and marks batches `done` without holding the RLock.
   * **`LeaseReaperDaemon`:** Background thread that runs every 15 s querying `WHERE status='leased' AND lease_expires_at < CURRENT_TIMESTAMP` to reclaim timed-out batches.

3. **Pure Domain Models (`server/models/`):**
   * Decoupled from Flask, SQLite, and network logic.
   * Entities: `Batch`, `PrescriptionFile`, `PlanTree`, `FolderExclusions`.
   * Slot ordering rule: **oldest `first_seen_at` first, tie-broken by `file_path ASC`**.

4. **Single Source of Truth: SQLite (`server/database.py`):**
   * `PRAGMA journal_mode = WAL;`
   * `PRAGMA synchronous  = FULL;`  — survives power loss; write volume is tiny.
   * `PRAGMA busy_timeout = 5000;`  — 5 s retry window for Waitress thread contention.
   * `PRAGMA foreign_keys = ON;`
   * One connection per thread (`check_same_thread=True`).

5. **CSV Exporter (crash-proof, no spillover file):**
   * After each SQLite `COMMIT` to `done`, the finalizer calls `CsvService.export_pending()`.
   * `CsvService` queries `WHERE status='done' AND csv_exported_at IS NULL` and attempts to append rows. On success it sets `csv_exported_at = CURRENT_TIMESTAMP` in a separate transaction.
   * If the master CSV is Excel-locked (`PermissionError`), the rows remain unexported in SQLite. A `/state` warning flag `csv_locked: true` is surfaced. The exporter retries on the next finalization or on a 60 s background ticker.
   * **No spillover file, no duplicate risk:** the retry is idempotent because `csv_exported_at` is the guard.

---

## 2. Directory Structure

```
server/
├── app.py                       # App factory, blueprint registration, Waitress runner
├── config.py                    # Env, paths, token, filename regex, lease_seconds
├── database.py                  # SQLite connection, WAL pragmas, migrations, boot_id
├── serializers.py               # All response formatters (replaces views/ folder)
│
├── controllers/
│   ├── __init__.py
│   ├── health_controller.py     # GET /health
│   ├── state_controller.py      # GET /state?rev=N, POST /selection
│   ├── job_controller.py        # POST /lease, POST /job-heartbeat, POST /job-result
│   ├── control_controller.py    # POST /control
│   ├── settings_controller.py   # GET /settings, POST /settings
│   └── export_controller.py     # GET /csv, GET /feed
│
├── services/
│   ├── __init__.py
│   ├── queue_service.py         # Thread-safe state machine & lease logic
│   ├── scanner_service.py       # Non-blocking directory poller (5 s)
│   ├── finalizer_worker.py      # Async TIFF verify + file finalization queue
│   ├── lease_reaper.py          # Background lease expiry daemon (15 s)
│   ├── csv_service.py           # Retry-safe CSV export (no spillover file)
│   └── recovery_service.py      # Boot-time orphaned lease reclaimer
│
├── models/
│   ├── __init__.py
│   ├── batch.py                 # Batch entity, slot allocations, slot ordering rule
│   ├── prescription.py          # Doctor prescription metadata & name parsing
│   ├── plan_tree.py             # In-memory tree builder + rev + boot_id
│   └── selection_rules.py       # Skip/include filters
│
├── adapters/
│   ├── __init__.py
│   ├── filesystem_adapter.py    # Stable file detection, cross-drive safe_move
│   ├── image_verifier/          # Polymorphic output image verification
│   │   ├── __init__.py
│   │   ├── base.py              # BaseImageVerifier ABC (template method: verify)
│   │   ├── tiff_verifier.py     # TiffVerifier: tifffile, DPI tags, ±px tolerance, strided blank check
│   │   ├── jpeg_verifier.py     # JpegVerifier: Pillow JFIF/EXIF DPI, SOF frame integrity
│   │   └── factory.py           # ImageVerifierFactory.verify_file(path, ...) → (bool, str)
│   ├── process_adapter.py       # psutil Photoshop watcher & Windows power commands
│   └── slip_adapter.py          # ReportLab PDF + Arabic reshaper + embedded TTF
│
├── migrations/
│   └── init.sql                 # v1 schema (see §3)
│
├── backups/                     # Daily SQLite backups (roshta_YYYYMMDD.db)
└── logs/                        # Rotating file logs (server.log)
```

---

## 3. Database Schema

### 3.1 Full Schema (`migrations/init.sql`)

```sql
-- PRAGMAs applied at connection time by database.py:
--   PRAGMA journal_mode = WAL;
--   PRAGMA synchronous  = FULL;
--   PRAGMA busy_timeout = 5000;
--   PRAGMA foreign_keys = ON;

-- 1. STAGING QUEUE
--    Files are NEVER deleted on batch promotion.
--    They are marked status='locked' + batch_id so the scanner ignores them.
--    They are deleted only after safe_move() moves the PSD to Finished/.
CREATE TABLE IF NOT EXISTS holding_files (
    id              TEXT PRIMARY KEY,       -- SHA-1 of normalised file_path
    file_path       TEXT UNIQUE NOT NULL,
    clinic          TEXT NOT NULL,
    paper_weight    TEXT NOT NULL,
    doctor_name     TEXT NOT NULL,
    file_size       INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    first_seen_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_skipped      INTEGER DEFAULT 0,      -- 1 = operator excluded
    status          TEXT DEFAULT 'waiting', -- 'waiting' | 'locked'
    batch_id        TEXT,
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_holding_group
    ON holding_files(clinic, paper_weight, status, is_skipped);
CREATE INDEX IF NOT EXISTS idx_holding_batch ON holding_files(batch_id);

-- 2. PRODUCTION BATCHES
CREATE TABLE IF NOT EXISTS batches (
    id                  TEXT PRIMARY KEY,
    batch_number        TEXT UNIQUE,        -- 'B00142'; assigned at lease time
    lease_id            TEXT,
    clinic              TEXT NOT NULL,
    paper_weight        TEXT NOT NULL,
    status              TEXT NOT NULL,      -- 'leased'|'verifying'|'finalizing'
                                            -- |'done'|'failed'|'interrupted'|'flagged'
    attempt_count       INTEGER DEFAULT 1,
    slot_count          INTEGER DEFAULT 4,
    is_forced           INTEGER DEFAULT 0,  -- Force-batch with < 4 designs
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    leased_at           TIMESTAMP,
    lease_expires_at    TIMESTAMP,          -- Reaped by LeaseReaperDaemon
    completed_at        TIMESTAMP,
    duration_ms         INTEGER,
    print_temp_path     TEXT,
    tracker_temp_path   TEXT,
    print_final_path    TEXT,
    tracker_final_path  TEXT,
    csv_exported_at     TIMESTAMP,          -- NULL until exported; retry guard
    slip_generated_at   TIMESTAMP,
    error_message       TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_batches_lease  ON batches(lease_id);
-- Partial index: only unexported done batches need scanning
CREATE INDEX IF NOT EXISTS idx_batches_csv ON batches(csv_exported_at)
    WHERE csv_exported_at IS NULL;

-- 3. QUADRANT SLOT ALLOCATIONS
CREATE TABLE IF NOT EXISTS batch_slots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    slot          TEXT NOT NULL,            -- 'TL'|'TR'|'BL'|'BR'
    doctor_name   TEXT NOT NULL,
    source_path   TEXT NOT NULL,
    finished_path TEXT,
    x_cm          REAL NOT NULL,
    y_cm          REAL NOT NULL,
    is_empty      INTEGER DEFAULT 0,        -- 1 = white-fill placeholder
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_slots_batch ON batch_slots(batch_id);

-- 4. STICKY FOLDER EXCLUSIONS
CREATE TABLE IF NOT EXISTS folder_exclusions (
    folder_path TEXT PRIMARY KEY,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. SYSTEM STATE & COUNTERS
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO system_state (key, value) VALUES ('next_batch_number', '1');
INSERT OR IGNORE INTO system_state (key, value) VALUES ('boot_id', '');
```

### 3.2 Atomic Promotion Transaction (`POST /lease`)

**Slot ordering rule:** 4 oldest `first_seen_at ASC, file_path ASC` files selected per clinic+paper_weight group.

```sql
BEGIN IMMEDIATE;

-- A. Atomically increment batch counter and capture new number
UPDATE system_state
SET    value      = CAST(value AS INTEGER) + 1,
       updated_at = CURRENT_TIMESTAMP
WHERE  key = 'next_batch_number';

SELECT printf('B%05d', CAST(value AS INTEGER) - 1)
FROM   system_state WHERE key = 'next_batch_number';

-- B. Insert the committed batch record
INSERT INTO batches (
    id, batch_number, lease_id, clinic, paper_weight,
    status, slot_count, is_forced, leased_at, lease_expires_at
) VALUES (
    :batch_id, :batch_number, :lease_id, :clinic, :paper_weight,
    'leased', :slot_count, :is_forced,
    CURRENT_TIMESTAMP, datetime('now', '+' || :lease_seconds || ' seconds')
);

-- C. Insert quadrant slots
INSERT INTO batch_slots (batch_id, slot, doctor_name, source_path, x_cm, y_cm, is_empty)
VALUES (:batch_id, 'TL', :doctor_tl, :path_tl, 0.0,  0.0,  :empty_tl),
       (:batch_id, 'TR', :doctor_tr, :path_tr, 25.0, 0.0,  :empty_tr),
       (:batch_id, 'BL', :doctor_bl, :path_bl, 0.0,  17.0, :empty_bl),
       (:batch_id, 'BR', :doctor_br, :path_br, 25.0, 17.0, :empty_br);

-- D. Lock files in holding_files — DO NOT DELETE THEM
--    Scanner will see status='locked' and skip them.
--    They are deleted only after safe_move() completes in JobFinalizerWorker.
UPDATE holding_files
SET    status   = 'locked',
       batch_id = :batch_id
WHERE  id IN (:file_id_tl, :file_id_tr, :file_id_bl, :file_id_br);

COMMIT;
```

### 3.3 Finalization Flow (Async — `JobFinalizerWorker`)

```
POST /job-result  ──►  Mark batch 'verifying' (< 5 ms transaction)
                  ──►  202 Accepted          (returns immediately)
                  ──►  Enqueue to JobFinalizerWorker.queue

JobFinalizerWorker (background thread):
  1. verify_tiff(print_temp_path, ±2 px tolerance, strided blank check)
  2. verify_tiff(tracker_temp_path, ±2 px tolerance)
  3. Mark batch 'finalizing'
  4. Rename temp TIFFs → final names  (atomic rename on same volume)
  5. safe_move() each source PSD → Finished/<clinic>/<date>/
     ├── On success: UPDATE batch_slots SET finished_path = ...
     └── On cross-device: stream to .part file, rename, delete source
  6. DELETE FROM holding_files WHERE batch_id = :batch_id
  7. UPDATE batches SET status='done', completed_at=...
  8. CsvService.export_pending()   (retry-safe; sets csv_exported_at)
  9. PdfSlipAdapter.generate_slip() if enabled   (sets slip_generated_at)
 10. Atomically swap QueueService._state_snapshot (rev++)
```

On TIFF verification failure → mark batch `flagged`, keep temp files for inspection, do NOT move source PSDs.

---

## 4. Key Adapter Implementations

### 4.1 Non-Blocking File Stability Scanner (`filesystem_adapter.py`)

Scan interval: **5 seconds** (reduced from 2 s to avoid hammering network shares).

Two-tier stability check per file:
1. **`(size, mtime)` unchanged** across two consecutive scans.
2. **Exclusive open test:** `open(path, 'rb').close()` — raises `PermissionError` if another process (e.g. SMB copy) still holds the file open. File treated as unstable.

Scanner → `holding_files` sync rules:
- **New stable file:** `INSERT OR IGNORE INTO holding_files (...)`.
- **File deleted from disk:** `DELETE FROM holding_files WHERE file_path = ? AND status = 'waiting'`. Locked files are never auto-deleted.
- **File size or mtime changed:** `UPDATE holding_files SET file_size=?, mtime=?, first_seen_at=CURRENT_TIMESTAMP, is_skipped=0`. Resets skip (the file is effectively a new version).
- **Folder excluded:** Applied at insert time via `SELECT 1 FROM folder_exclusions WHERE ? LIKE folder_path || '%'`. Never inserted.

### 4.2 Image Verifier (`adapters/image_verifier/`)

Polymorphic verifier kept because **both TIFF and JPEG outputs are required**.
All callers go through `ImageVerifierFactory.verify_file()` — adding a new format later is a single new class + one factory entry.

**`base.py` — Abstract Base:**
```python
from abc import ABC, abstractmethod

class BaseImageVerifier(ABC):
    """
    Template-method ABC. Subclasses implement _check_format_specific();
    the base class handles the common pixel-dimension and DPI contract.
    """
    def verify(
        self,
        path: str,
        expected_width_px: int,
        expected_height_px: int,
        expected_dpi: int,
        px_tolerance: int = 2,
    ) -> tuple[bool, str]:
        ok, msg = self._open_and_check_dimensions(
            path, expected_width_px, expected_height_px, expected_dpi, px_tolerance
        )
        if not ok:
            return False, msg
        return self._check_format_specific(path)

    @abstractmethod
    def _open_and_check_dimensions(
        self, path, exp_w, exp_h, exp_dpi, tol
    ) -> tuple[bool, str]: ...

    @abstractmethod
    def _check_format_specific(self, path: str) -> tuple[bool, str]: ...
```

**`tiff_verifier.py` — TIFF:**
- Opens with `tifffile`; reads `XResolution`, `YResolution`, `ResolutionUnit` tags.
- Missing `ResolutionUnit` defaults to **inch** (per TIFF spec § 6.1 — not an error).
- Pixel dimension check: `abs(actual_w - expected_w) <= px_tolerance` (tolerance absorbs 300 DPI rounding, e.g. 50 cm → 5905.5118 px).
- Blank-page check: samples `page.asarray()[::50, ::50]` (strided) — avoids allocating the full ~70 MB array.

**`jpeg_verifier.py` — JPEG:**
- Opens with `Pillow`; reads DPI from `info['dpi']` (JFIF `APP0`) or EXIF `XResolution`/`YResolution`.
- Validates SOF frame integrity (width × height from image header).
- Same ±`px_tolerance` and DPI check.

**`factory.py` — Entry Point:**
```python
class ImageVerifierFactory:
    _registry: dict[str, BaseImageVerifier] = {
        ".tif":  TiffVerifier(),
        ".tiff": TiffVerifier(),
        ".jpg":  JpegVerifier(),
        ".jpeg": JpegVerifier(),
    }

    @classmethod
    def verify_file(
        cls,
        path: str,
        expected_width_px: int,
        expected_height_px: int,
        expected_dpi: int,
        px_tolerance: int = 2,
    ) -> tuple[bool, str]:
        ext = os.path.splitext(path)[1].lower()
        verifier = cls._registry.get(ext)
        if verifier is None:
            return False, f"Unsupported format: {ext}"
        try:
            return verifier.verify(
                path, expected_width_px, expected_height_px,
                expected_dpi, px_tolerance
            )
        except Exception as exc:
            return False, f"Verifier exception: {exc}"
```

All finalization calls use `ImageVerifierFactory.verify_file(path, ...)` — callers never import a concrete verifier class.

### 4.3 `safe_move()` (`adapters/filesystem_adapter.py`)

```python
def safe_move(src: str, dst: str) -> None:
    """
    Atomically moves src to dst across any drive boundary.

    - Same volume: os.rename() — instant, atomic.
    - Cross-volume (errno.EXDEV): Stream src → dst.part, flush, rename
      dst.part → dst, then delete src. Only catches errno.EXDEV, not a
      locked-file OSError (which propagates to caller for retry).
    """
```

### 4.4 Arabic-Ready PDF Slip (`adapters/slip_adapter.py`)

- Font: Bundle **Amiri** or **Cairo** TTF inside the PyInstaller package (`--add-data`).
- Reshaping: `arabic_reshaper.reshape()` + `bidi.algorithm.get_display()` applied **only** to dynamic Arabic data fields (clinic name, doctor names, address). English labels (e.g. "Batch:", "Date:") are passed through untouched.
- Layout: RTL Arabic lines are right-aligned; LTR English labels are left-aligned.

### 4.5 Token Authentication Strategy

- **Method:** Operator enters the token once in the Roshta plugin settings panel. Stored via UXP `localStorage`. No default fallback token.
- **No file-based token delivery:** Writing the token to the source folder is dropped — that folder may be shared.
- **Require explicit setup:** Server refuses to start without a configured `SECRET_TOKEN`. Plugin shows a setup screen until the token is saved.

---

## 5. `/state` Response: `boot_id` for Restart Detection

Every `/state` response includes:
```json
{
  "boot_id": "a3f9c1b2-...",
  "rev": 57,
  "mode": "running",
  ...
}
```

If the plugin receives a different `boot_id` than what it cached, it treats the revision as invalidated, clears its local state snapshot, and re-renders the full tree regardless of `rev`.

`boot_id` is stamped by `database.stamp_boot_id()` called once during `create_app()`.

---

## 6. Disk Space & Logging

- **Disk space check:** Before issuing any lease, `QueueService` checks available disk space on the output volume. If `< 5 GB`, no new jobs are leased and `/state` includes `disk_warning: true`.
- **Daily SQLite backup:** On startup and then at midnight, `sqlite3.Connection.backup()` copies the live database to `backups/roshta_YYYYMMDD.db`. Only the last 7 days are retained.
- **Logging:** Rotating file handler: `logs/server.log`, max 5 MB per file, 5 backups. Under `--noconsole` PyInstaller mode, `sys.stdout` is `None`; `app.py` redirects it to the log file at startup.

---

## 7. API Endpoints

| Endpoint             | Method | Description |
|---|---|---|
| `/health`            | `GET`  | Status, uptime, PS process state, resume summary, `boot_id`. |
| `/state?rev=N`       | `GET`  | Full plan tree, `boot_id`, `rev`, warnings, disk space. `304` if `rev` unchanged. |
| `/selection`         | `POST` | Toggle `is_skipped` on holding_files nodes: `{ "rev": N, "skip": [...], "include": [...] }`. |
| `/lease`             | `POST` | Lease one ready batch. Returns `200` with job contract or `204`. |
| `/job-heartbeat`     | `POST` | Extend active lease: `{ "job_id": "...", "lease_id": "..." }`. |
| `/job-result`        | `POST` | Report completion. Returns `202 Accepted` immediately; finalization is async. |
| `/control`           | `POST` | `play`, `pause`, `stop`, `force_batch`, `retry`, `mark_reviewed`, `set_after_finish`, `cancel_after_finish`, `rescan`. |
| `/settings`          | `GET`/`POST` | Read/update config (paths, templates, canvas, DPI, bleed, lease_seconds). |
| `/feed?after=ID`     | `GET`  | Paginated activity feed. |
| `/csv`               | `GET`  | Download master CSV. |

---

## 8. Build Phases

### Phase 1: Foundation & SQLite WAL Engine ✅ DONE
* `database.py` with WAL + FULL sync + boot_id.
* `config.py` with configurable regex.
* `app.py` with Waitress + X-Token middleware.
* `GET /health` endpoint.
* All tests passing in `tests/test_phase1.py`.

### Phase 2: Staging Queue & Non-Blocking Scanner
* `FileSystemAdapter`: 5 s snapshot diff, two-tier stability, `safe_move`.
* `ScannerService` daemon.
* `PlanTree` in-memory builder with `rev`.
* `GET /state?rev=N` (served from immutable snapshot).
* `POST /selection` (toggles `is_skipped` in SQLite).
* `tests/test_phase2.py`.

### Phase 3: Atomic Lease & Fake Worker Validation
* `QueueService` with `POST /lease`, lease timer, `LeaseReaperDaemon`.
* `JobFinalizerWorker`: TIFF verify, `safe_move`, CSV, slip, `done` marking.
* `POST /job-result` (202 Accepted, enqueue to finalizer).
* `POST /job-heartbeat`.
* `tools/fake_plugin.py`: simulates 50 E2E batches without Photoshop.
* `tests/test_phase3.py`.

### Phase 4: CSV, Slips & Address Book
* `CsvService` (retry-safe, `csv_exported_at` guard, 60 s background tick).
* `PdfSlipAdapter` (ReportLab + Amiri TTF + Arabic reshaper).
* `config/address_book.csv` reader (doctor row wins over clinic row).

### Phase 5: Controls, Recovery & Process Watcher
* `POST /control`: `play`, `pause`, `stop`, `force_batch`, `retry`, `mark_reviewed`, `set_after_finish`, `cancel_after_finish`.
* `ProcessAdapter`: `psutil` Photoshop watcher + Windows power commands.
* `RecoveryService`: reclaim `leased`/`finalizing` batches on boot.
* Disk space check gate in `QueueService.lease()`.
* Daily SQLite backup.

### Phase 6: Packaging & Production Hardening
* PyInstaller `--noconsole --onedir` with bundled Amiri TTF and `config.json` next to exe.
* `sys.stdout`/`sys.stderr` redirected to `logs/server.log` at startup.
* Clean-machine validation (no Python, no Node).

---

## 9. Failure Handling Matrix

| Situation | Root Cause | Recovery |
|---|---|---|
| **Plugin reports error** | Corrupt PSD, disk write error | Batch → `failed`. Auto-retry once; if fails again → `flagged`, alert operator. |
| **Lease expires** | Photoshop hung, modal dialog | `LeaseReaperDaemon` reclaims batch → `interrupted`. Temp TIFFs discarded. |
| **Verification fails** | Corrupt TIFF, wrong dimensions | Source PSDs never moved. Temp TIFFs kept. Batch → `flagged`. |
| **Power cut / crash** | Sudden shutdown | `RecoveryService` on boot presents Resume modal. `boot_id` change triggers plugin cache reset. |
| **PS closed by user** | Operator finished | Grace period (30–60 s), then server exits. State safe on disk. |
| **Pause mid-batch** | Operator clicks Pause | Current batch completes cleanly; `paused` before next lease. |
| **File still copying (SMB)** | 150 MB PSD in flight | `open(rb)` test fails → not inserted until released. |
| **Disk space < 5 GB** | Heavy TIFF output | Warning in `/state`, no new leases until cleared. |
| **CSV locked by Excel** | Operator viewing log | `csv_exported_at` remains NULL, retry on next finalization or 60 s tick. |
