# Server Implementation Plan: Prescription Printing Automation

This document defines the architectural specification and implementation roadmap for the **Roshta Python/Flask Service**. It incorporates all critical production feedback: **SQLite in WAL mode as the single source of truth**, an in-memory plan engine, synchronous transaction guarantees, Excel-safe CSV streaming with spillover buffers, and robust Windows filesystem adapters.

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
      │   - ScannerService: Manages background non-blocking polling │
      └──────────────┬───────────────────────────────┬──────────────┘
                     │                               │
          (Operates Pure Domain)             (Drives Synchronously)
                     ▼                               ▼
      ┌─────────────────────────────┐ ┌─────────────────────────────┐
      │     Pure Domain Models      │ │   Single Source of Truth    │
      │   - Batch & Slot rules      │ │   - SQLite (WAL Mode)       │
      │   - In-memory Plan Cache    │ │     • holding_files (Queue) │
      │   - Folder Exclusions       │ │     • batches (Committed)   │
      │   - Doctor Name Regex       │ └──────────────┬──────────────┘
      └─────────────────────────────┘                │ (After Commit)
                     │                               ▼
          (Uses via Interfaces)               ┌──────────────┐
                     ▼                        │ CSV Exporter │
      ┌─────────────────────────────────┐     │ (Spill-safe) │
      │            Adapters             │     └──────────────┘
      │   - FileSystem (Cross-drive)    │
      │   - Image Verifiers (TIFF, JPEG)│
      │   - Process / Power (Waitress)  │
      │   - PDF Slip (ReportLab/Arabic) │
      └─────────────────────────────────┘
```

### 1.1 Layer Responsibilities

1. **Controllers (`server/controllers/`):**
   * Flask routes served in production via **`waitress`** (pure Python, multi-threaded, robust on Windows, seamlessly packages with PyInstaller).
   * Extracts headers (`X-Token`), parses and validates JSON inputs, maps domain exceptions to uniform HTTP error codes (401, 404, 409, 422, 423), and renders views.
2. **Service Layer (`server/services/`):**
   * **`QueueService`:** Owns the primary re-entrant lock (`threading.RLock`). Coordinates job dispatching, lease timers, and status changes. Guarantees that any state affecting correctness is written **synchronously to SQLite inside a database transaction before replying to HTTP requests**.
   * **`ScannerService`:** Background daemon performing non-blocking directory scans using state-snapshot diffing, maintaining two-way synchronization between disk files and the `holding_files` database table.
3. **Pure Domain Models (`server/models/`):**
   * Decoupled from Flask, SQLite, and network logic.
   * Entities: `Batch`, `PrescriptionFile`, `PlanTree`, `FolderExclusions`, `AddressBook`.
   * Encapsulate slot math, quadrant assignment (TL, TR, BL, BR), doctor name normalization regex, and in-memory tree grouping.
4. **Single Source of Truth: SQLite with WAL Mode (`server/database.py`):**
   * SQLite configured with `PRAGMA journal_mode=WAL;` and `PRAGMA synchronous=NORMAL;` for high-concurrency read/write operations without table locking.
   * **Staging Queue (`holding_files`):** All stable incoming files waiting to be batched are staged here with clinic, paper weight, and operator skip toggles (`is_skipped`).
   * **Committed Batches (`batches` & `batch_slots`):** When leased, files are atomically promoted from `holding_files` to `batches`.
   * **Zero Disk Overhead for Polling:** High-frequency `/state?rev=N` polling is answered directly from an in-memory cached tree built from `holding_files`, invalidated only when disk files change or selections toggle.

5. **CSV Exporter with Excel Lock Resilience (`server/services/csv_service.py`):**
   * CSV is treated as an append-only reporting export executed **after** the SQLite transaction commits.
   * **Excel Locking Mitigation:** Windows Excel takes an exclusive lock when an operator opens `master_daily_log.csv`. If writing fails with `PermissionError`:
     * The row is written to a fallback spill buffer file (`logs/csv_spillover.csv`).
     * A persistent warning flag (`csv_locked: true`) is exposed in `/state`.
     * A background flush task automatically drains the spillover buffer into the main CSV once Excel is closed.

---

## 2. Directory Structure

```
server/
├── app.py                       # App factory, blueprints registration, Waitress runner
├── config.py                    # Environment, paths, token resolution, configurable regex
├── database.py                  # SQLite connection, WAL mode, migrations, schema_version
│
├── controllers/                 # HTTP Routing & Request/Response Serialization
│   ├── __init__.py
│   ├── health_controller.py     # GET /health
│   ├── state_controller.py      # GET /state, POST /selection
│   ├── job_controller.py        # GET /next-job, POST /job-heartbeat, POST /job-result
│   ├── control_controller.py    # POST /control (play, pause, stop, force_batch, etc.)
│   ├── settings_controller.py   # GET /settings, POST /settings
│   └── export_controller.py     # GET /csv, GET /feed, GET /debug/state-dump
│
├── services/                    # Transactional Orchestration & Thread Locking
│   ├── __init__.py
│   ├── queue_service.py         # Main thread-safe state machine & SQLite coordinator
│   ├── scanner_service.py       # Non-blocking directory poller (snapshot comparison)
│   ├── csv_service.py           # Excel-resilient CSV logger with spillover buffer
│   └── recovery_service.py      # Boot-time crash detection & orphaned lease reclaimer
│
├── models/                      # Pure Domain Models (Zero external dependencies)
│   ├── __init__.py
│   ├── batch.py                 # Batch entity, slot allocations, dimensions
│   ├── prescription.py          # Doctor prescription metadata & name parsing
│   ├── plan_tree.py             # In-memory tree builder, revision tracker (rev)
│   ├── selection_rules.py       # Skip/include filters, persistence rules
│   └── address_book.py          # Clinic addresses and delivery metadata
│
├── views/                       # Response Presenters / Serializers
│   ├── __init__.py
│   ├── state_view.py            # Formats /state response from in-memory plan
│   ├── job_view.py              # Formats /next-job contract
│   ├── health_view.py           # Formats /health and resume summary
│   └── error_view.py            # Uniform error envelopes
│
└── adapters/                    # External Interfaces & OS Operations
    ├── __init__.py
    ├── filesystem_adapter.py    # Non-blocking scan diffing, cross-drive atomic move
    ├── image_verifier/          # Polymorphic Image Verification (TIFF, JPEG, etc.)
    │   ├── __init__.py
    │   ├── base.py              # BaseImageVerifier (ABC with template method)
    │   ├── tiff_verifier.py     # TiffVerifier (tifffile, pages, tags, DPI)
    │   ├── jpeg_verifier.py     # JpegVerifier (Pillow, JFIF/EXIF DPI, SOF integrity)
    │   └── factory.py           # ImageVerifierFactory (resolves verifier by extension)
    ├── process_adapter.py       # Psutil Photoshop watcher & Windows power commands
    └── slip_adapter.py          # ReportLab PDF generator with Arabic reshaping

```

---

## 3. Database Schema (SQLite with WAL Mode)

SQLite is the **sole source of truth**, split cleanly into two stages:
1. **`holding_files`:** The incoming staging queue of scanned, verified files waiting to form batches.
2. **`batches` & `batch_slots`:** Production batches that have been leased, are rendering, or are finished.

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- Schema Version Tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

-- 1. STAGING QUEUE: Incoming files scanned from disk
CREATE TABLE IF NOT EXISTS holding_files (
    id TEXT PRIMARY KEY,                       -- Unique hash of file_path
    file_path TEXT UNIQUE NOT NULL,            -- Absolute path on disk
    clinic TEXT NOT NULL,
    paper_weight TEXT NOT NULL,
    doctor_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_skipped INTEGER DEFAULT 0,              -- 0 = included, 1 = skipped by operator
    status TEXT DEFAULT 'waiting'              -- 'waiting', 'locked'
);
CREATE INDEX IF NOT EXISTS idx_holding_group ON holding_files(clinic, paper_weight, is_skipped);

-- 2. COMMITTED PRODUCTION BATCHES (Persisted from 'leased' onward)
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,                       -- e.g. "batch_20261019_001"
    batch_number TEXT UNIQUE,                  -- e.g. "B00142" (assigned upon lease)
    lease_id TEXT,                             -- UUID issued with current lease
    clinic TEXT NOT NULL,
    paper_weight TEXT NOT NULL,
    status TEXT NOT NULL,                      -- 'ready', 'leased', 'verifying', 'finalizing', 'done', 'failed', 'interrupted', 'flagged'
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

-- Quadrant Item Allocations
CREATE TABLE IF NOT EXISTS batch_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    slot TEXT NOT NULL,                        -- 'TL', 'TR', 'BL', 'BR'
    doctor_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    finished_path TEXT,
    x_cm REAL NOT NULL,
    y_cm REAL NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

-- Sticky Folder Exclusions (File-level skips are stored directly in holding_files.is_skipped)
CREATE TABLE IF NOT EXISTS folder_exclusions (
    folder_path TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Address Book for Delivery Slips
CREATE TABLE IF NOT EXISTS address_book (
    clinic_name TEXT PRIMARY KEY,
    address_line TEXT NOT NULL,
    phone TEXT,
    notes TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- System State & Counters
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 3.1 The Atomic Promotion Transaction (Holding -> Batches)
When 4 designs in `holding_files` match a clinic and paper weight (or when the operator triggers Force Batch), they are promoted to `batches` in a single ACID transaction:

```sql
BEGIN TRANSACTION;

-- 1. Create the committed batch record
INSERT INTO batches (
    id, batch_number, lease_id, clinic, paper_weight, status, slot_count, leased_at, lease_expires_at
) VALUES (
    'batch_20261019_001', 'B00142', 'lease_9f2a', 'Al-Amal', 'Standard', 'leased', 4, CURRENT_TIMESTAMP, datetime('now', '+600 seconds')
);

-- 2. Allocate the 4 quadrant slots
INSERT INTO batch_slots (batch_id, slot, doctor_name, source_path, x_cm, y_cm) VALUES
('batch_20261019_001', 'TL', 'Dr_Sarah', 'D:/Roshta/Input/Al-Amal/Standard/Dr_Sarah.psd', 0.0, 0.0),
('batch_20261019_001', 'TR', 'Dr_Ahmed', 'D:/Roshta/Input/Al-Amal/Standard/Dr_Ahmed.psd', 25.0, 0.0),
('batch_20261019_001', 'BL', 'Dr_Mona',  'D:/Roshta/Input/Al-Amal/Standard/Dr_Mona.psd',  0.0, 17.0),
('batch_20261019_001', 'BR', 'Dr_Ziad',  'D:/Roshta/Input/Al-Amal/Standard/Dr_Ziad.psd',  25.0, 17.0);

-- 3. Delete the promoted files from the staging queue so they cannot be double-batched
DELETE FROM holding_files WHERE file_path IN (
    'D:/Roshta/Input/Al-Amal/Standard/Dr_Sarah.psd',
    'D:/Roshta/Input/Al-Amal/Standard/Dr_Ahmed.psd',
    'D:/Roshta/Input/Al-Amal/Standard/Dr_Mona.psd',
    'D:/Roshta/Input/Al-Amal/Standard/Dr_Ziad.psd'
);

COMMIT;
```


---

## 4. Robust Adapter & Service Implementations

### 4.1 Non-Blocking File Stability Scanner (`filesystem_adapter.py`)
**Problem Solved:** Never sleep inside the file scanning loop. Instead, track `(size, mtime)` in a dictionary across background polling passes.

```python
import os
import shutil
import time
from typing import Dict, Tuple

class FileSystemAdapter:
    def __init__(self):
        # Maps file_path -> (size, mtime, first_seen_timestamp)
        self._seen_files: Dict[str, Tuple[int, float, float]] = {}

    def scan_directory(self, root_dir: str, allowed_exts: Tuple[str, ...], min_age_seconds: float = 3.0):
        """
        Non-blocking scan. Returns list of fully stable files without sleeping.
        A file is stable if size and mtime are identical across passes and file age >= min_age_seconds.
        """
        current_seen: Dict[str, Tuple[int, float, float]] = {}
        stable_files = []
        now = time.time()

        for dirpath, _, filenames in os.walk(root_dir):
            for fname in filenames:
                if not fname.lower().endswith(allowed_exts):
                    continue
                full_path = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(full_path)
                    size, mtime = stat.st_size, stat.st_mtime
                except (OSError, PermissionError):
                    continue

                if full_path in self._seen_files:
                    prev_size, prev_mtime, first_seen = self._seen_files[full_path]
                    if size == prev_size and mtime == prev_mtime and (now - first_seen) >= min_age_seconds:
                        # Stability verified. Check read permissions.
                        if os.access(full_path, os.R_OK):
                            stable_files.append(full_path)
                    current_seen[full_path] = (size, mtime, first_seen)
                else:
                    current_seen[full_path] = (size, mtime, now)

        self._seen_files = current_seen
        return stable_files

    def safe_move(self, src: str, dst: str) -> str:
        """
        Cross-volume safe move. Never silently overwrites.
        Falls back to copy + verify + delete across drive boundaries.
        """
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        target = dst
        # Collision resolution: append timestamp suffix if target exists
        if os.path.exists(target):
            base, ext = os.path.splitext(dst)
            target = f"{base}_{int(time.time()*1000)}{ext}"

        try:
            # os.replace works atomically if on the same filesystem
            os.replace(src, target)
        except OSError:
            # Cross-drive move fallback
            shutil.copy2(src, target)
            if os.path.getsize(target) == os.path.getsize(src):
                os.remove(src)
            else:
                raise IOError(f"Copy verification failed for {src} to {target}")
        return target
```

### 4.2 Multi-Format Image Verification Hierarchy (`adapters/image_verifier/`)
To support multiple export formats (TIFF for lossless prepress RIPs, JPEG/PNG for lightweight proofing/trackers, etc.), verification uses an **Abstract Base Class (Template Method pattern)** with format-specific subclasses and a dynamic factory.

```python
# adapters/image_verifier/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
import os
from typing import Tuple
import numpy as np

@dataclass
class ImageMetadata:
    width_px: int
    height_px: int
    dpi: float
    page_count: int
    color_mode: str
    pixel_array: np.ndarray

class BaseImageVerifier(ABC):
    """Abstract Base Class defining the template method for image verification."""

    def verify(self, path: str, expected_width_px: int, expected_height_px: int, expected_dpi: int) -> Tuple[bool, str]:
        """Template method: executes shared pipeline and delegates format-specific parsing."""
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False, f"File missing or empty: {path}"

        try:
            # 1. Format-specific integrity check
            integrity_ok, msg = self._validate_format_integrity(path)
            if not integrity_ok:
                return False, msg

            # 2. Format-specific metadata extraction
            meta = self._extract_metadata(path)

            # 3. Common dimension validation
            if meta.width_px != expected_width_px or meta.height_px != expected_height_px:
                return False, f"Dimension mismatch: got {meta.width_px}x{meta.height_px}, expected {expected_width_px}x{expected_height_px}"

            # 4. Common DPI validation (with 2.0 tolerance for unit conversions)
            if abs(meta.dpi - expected_dpi) > 2.0:
                return False, f"DPI mismatch: got {meta.dpi:.1f}, expected {expected_dpi}"

            # 5. Multi-page restriction (montage must be a single sheet)
            if meta.page_count != 1:
                return False, f"Expected 1 page/frame, found {meta.page_count}"

            # 6. Non-blank check (ensures canvas contains varied pixel data, not solid white/black)
            if np.all(meta.pixel_array == meta.pixel_array[0, 0]):
                return False, "Image is blank/solid color"

            return True, "OK"
        except Exception as e:
            return False, f"Verification failed with error: {str(e)}"

    @abstractmethod
    def _validate_format_integrity(self, path: str) -> Tuple[bool, str]:
        """Perform format-specific stream or header validation."""
        pass

    @abstractmethod
    def _extract_metadata(self, path: str) -> ImageMetadata:
        """Extract width, height, DPI, and pixel array using format-appropriate library."""
        pass
```

```python
# adapters/image_verifier/tiff_verifier.py
import tifffile
from typing import Tuple
from .base import BaseImageVerifier, ImageMetadata

class TiffVerifier(BaseImageVerifier):
    def _validate_format_integrity(self, path: str) -> Tuple[bool, str]:
        try:
            with tifffile.TiffFile(path) as tif:
                if len(tif.pages) == 0:
                    return False, "TIFF has no pages"
            return True, "OK"
        except Exception as e:
            return False, f"Corrupted TIFF header: {str(e)}"

    def _extract_metadata(self, path: str) -> ImageMetadata:
        with tifffile.TiffFile(path) as tif:
            page = tif.pages[0]
            h, w = page.shape[0], page.shape[1]
            
            # ResolutionUnit: 2 = Inch, 3 = Centimeter
            res_unit = page.tags.get("ResolutionUnit")
            x_res_tag = page.tags.get("XResolution")
            dpi = 0.0
            if res_unit and x_res_tag:
                unit_val = res_unit.value
                x_res = x_res_tag.value[0] / x_res_tag.value[1]
                dpi = x_res if unit_val == 2 else (x_res * 2.54 if unit_val == 3 else 0.0)

            return ImageMetadata(
                width_px=w,
                height_px=h,
                dpi=dpi,
                page_count=len(tif.pages),
                color_mode=str(page.photometric),
                pixel_array=page.asarray()
            )
```

```python
# adapters/image_verifier/jpeg_verifier.py
from PIL import Image
import numpy as np
from typing import Tuple
from .base import BaseImageVerifier, ImageMetadata

class JpegVerifier(BaseImageVerifier):
    def _validate_format_integrity(self, path: str) -> Tuple[bool, str]:
        try:
            with Image.open(path) as img:
                img.verify() # Validates JPEG markers and stream integrity
            return True, "OK"
        except Exception as e:
            return False, f"Corrupted JPEG stream: {str(e)}"

    def _extract_metadata(self, path: str) -> ImageMetadata:
        # Re-open after verify() closes the file pointer
        with Image.open(path) as img:
            dpi_tuple = img.info.get("dpi", (72.0, 72.0))
            dpi = float(dpi_tuple[0])
            pixel_data = np.array(img)

            return ImageMetadata(
                width_px=img.width,
                height_px=img.height,
                dpi=dpi,
                page_count=getattr(img, "n_frames", 1),
                color_mode=img.mode,
                pixel_array=pixel_data
            )
```

```python
# adapters/image_verifier/factory.py
import os
from typing import Dict, Tuple
from .base import BaseImageVerifier
from .tiff_verifier import TiffVerifier
from .jpeg_verifier import JpegVerifier

class ImageVerifierFactory:
    _registry: Dict[str, BaseImageVerifier] = {
        ".tif": TiffVerifier(),
        ".tiff": TiffVerifier(),
        ".jpg": JpegVerifier(),
        ".jpeg": JpegVerifier(),
    }

    @classmethod
    def register(cls, ext: str, verifier: BaseImageVerifier):
        cls._registry[ext.lower()] = verifier

    @classmethod
    def get_verifier(cls, path: str) -> BaseImageVerifier:
        _, ext = os.path.splitext(path)
        ext = ext.lower()
        if ext not in cls._registry:
            raise ValueError(f"No image verifier registered for extension '{ext}'")
        return cls._registry[ext]

    @classmethod
    def verify_file(cls, path: str, expected_width_px: int, expected_height_px: int, expected_dpi: int) -> Tuple[bool, str]:
        try:
            verifier = cls.get_verifier(path)
            return verifier.verify(path, expected_width_px, expected_height_px, expected_dpi)
        except ValueError as e:
            return False, str(e)
```


### 4.3 Arabic-Ready Delivery Slip Generation (`slip_adapter.py`)
Using pure **ReportLab** (no GTK/WeasyPrint dependencies) combined with `arabic-reshaper` and `python-bidi`:
```python
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib.pagesizes import A5
from reportlab.pdfgen import canvas

class PdfSlipAdapter:
    def format_arabic(self, text: str) -> str:
        if not text:
            return ""
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)

    def generate_slip(self, output_pdf_path: str, clinic: str, batch_num: str, doctors: list, address: str):
        c = canvas.Canvas(output_pdf_path, pagesize=A5)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(40, 540, f"Batch: {batch_num}")
        
        # Render Arabic-safe text strings
        c.drawString(40, 510, self.format_arabic(f"Clinic: {clinic}"))
        c.drawString(40, 480, self.format_arabic(f"Address: {address}"))
        
        c.setFont("Helvetica", 12)
        y = 440
        c.drawString(40, y, "Included Prescriptions:")
        for doc in doctors:
            y -= 25
            c.drawString(60, y, self.format_arabic(f"• Dr. {doc}"))
        c.save()
```

### 4.4 Excel-Safe CSV Service (`csv_service.py`)
```python
import csv
import os
import threading

class CsvService:
    def __init__(self, master_path: str, spill_path: str):
        self._master_path = master_path
        self._spill_path = spill_path
        self._lock = threading.Lock()
        self.csv_locked_warning = False

    def append_batch(self, row: dict):
        with self._lock:
            # Drain any previous spillover first if master is accessible
            self._try_drain_spillover()

            try:
                self._write_row(self._master_path, row)
                self.csv_locked_warning = False
            except (PermissionError, IOError):
                # Excel has locked the master file -> divert to spillover
                self._write_row(self._spill_path, row)
                self.csv_locked_warning = True

    def _try_drain_spillover(self):
        if not os.path.exists(self._spill_path):
            return
        try:
            with open(self._spill_path, "r", encoding="utf-8-sig") as sf:
                rows = list(csv.DictReader(sf))
            if rows:
                with open(self._master_path, "a", newline="", encoding="utf-8-sig") as mf:
                    writer = csv.DictWriter(mf, fieldnames=rows[0].keys())
                    writer.writerows(rows)
            os.remove(self._spill_path)
            self.csv_locked_warning = False
        except (PermissionError, IOError):
            pass # Still locked
```

---

## 5. Token Authentication & UXP Sandboxing Strategy

**The Problem:** The Photoshop UXP plugin is sandboxed and cannot arbitrarily read files from `%LOCALAPPDATA%`.

**The Solution:**
1. **Configurable Token in `config.json`:** The server reads `SECRET_TOKEN` from `server/config.json` (or CLI `--token` flag). Default fallback can be a stable generated secret.
2. **One-Time Setup in Plugin Settings:** The operator copies this token into the Roshta plugin settings panel once, where it is persisted via UXP's `localStorage`.
3. **Automated Discovery (Alternative):** The Python server writes `roshta_session.token` into the root of the **Source Folder** (which the operator grants to the plugin). The plugin reads the token directly from the granted folder.

---

## 6. Server Build Roadmap (Vertical Slice First)

### Phase 1: Foundation & SQLite WAL Engine
* Initialize `server/database.py` with schema versioning (`schema_version`), table creation, and WAL pragmas.
* Configure `config.py` with configurable filename regex (`r"^(.*?)(?:[_\-\s]\d+)?\.(psd|psb|tif|tiff)$"`, case-insensitive).
* Implement `app.py` wrapped with **`waitress`** server.
* Add `X-Token` middleware and `GET /health` endpoint.

### Phase 2: Staging Queue & Non-Blocking Scanner
* Implement `FileSystemAdapter` with snapshot comparison and two-way sync:
  * Inserts newly stabilized files into `holding_files`.
  * Prunes deleted/renamed ghost files (`DELETE FROM holding_files WHERE file_path NOT IN (...)`).
* Implement `ScannerService` daemon running every 2.0 seconds.
* Implement `GET /state?rev=N` and `POST /selection`:
  * `POST /selection` toggles `holding_files.is_skipped` directly in SQLite.
  * Rebuilds in-memory plan tree cache and increments `rev`.

### Phase 3: Vertical Slice: Atomic Batch Promotion & Job Leasing
* Implement `QueueService`:
  * `GET /next-job`: Executes the atomic SQL transaction: inserts `batches` + `batch_slots`, deletes promoted files from `holding_files`, and returns job contract.
  * In-memory lease timer (10-minute expiry with auto-reclaim).
* Implement `POST /job-result`:
  * Synchronously updates SQLite status to `verifying`.
  * Verifies output images via `ImageVerifierFactory.verify_file(path, ...)`.
  * Multi-step safe move of source files to `Finished` (marked `finalizing` during file moves).
  * Updates SQLite status to `done`.


* Implement `tools/fake_plugin.py` and run 50 end-to-end batches completely without Photoshop.

### Phase 4: Excel-Safe CSV Logging & Address Slips
* Implement `CsvService` with Windows Excel file-lock detection, spillover buffer, and auto-draining.
* Expose `csv_locked_warning` in `/state`.
* Implement `PdfSlipAdapter` using `ReportLab`, `arabic-reshaper`, and `python-bidi`.

### Phase 5: Controls, Recovery & Process Watcher
* Implement `POST /control`: `play`, `pause`, `stop`, `force_batch`, `retry`, `mark_reviewed`.
* Implement `ProcessAdapter` watching `Photoshop.exe` via `psutil` with configurable exit grace period (30–60s).
* Build `recovery_service.py`: Reclaim orphaned `leased` or `finalizing` batches on server boot.

### Phase 6: Packaging & Production Hardening
* Package into standalone executable using PyInstaller:
  ```bash
  pyinstaller --noconsole --name roshta-server --add-data "config.json;." app.py
  ```
* Verify execution on clean Windows test environment with no Python installed.
