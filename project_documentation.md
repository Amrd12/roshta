# Prescription Printing Automation: Photoshop UXP Plugin + Flask Service (Spec v2)

Revision of the original blueprint. Changes: the system is now two parts (UXP panel plus a Python/Flask service packaged as an exe), HTTP replaces any WebSocket idea, pause control lives inside Photoshop, and an After-Finishing action (shutdown, restart, etc.) has been added.

## 1. Project Overview

A prepress automation system for a print shop handling medical prescriptions. It scans incoming design files (PSD and similar), groups them by clinic and paper weight, waits for an optimal batch of four (4-up), and automatically generates print-ready montages and tracker canvases, logging every action to CSV.

Photoshop is required because the inputs are layered PSDs that only Photoshop can render faithfully (fonts, smart objects, color management).

## 2. Architecture

The system has two components with a strict division of labor.

**Flask service (Python, packaged as a standalone exe, no Python or Node install needed on shop machines)**

- Folder scanning, the "file is stable" check, grouping, and the wait-for-4 queue
- Batch numbering and naming template resolution
- `session_state.json` as the single source of truth for queue state
- CSV logging (session log and master daily log)
- File moves to Finished, disk space checks, RIP hot-folder handoff
- After-Finishing actions (shutdown, restart, etc.)
- Watching for Photoshop and exiting after it closes

**UXP plugin (inside Photoshop)**

- The three-panel UI (React)
- Requests jobs from the service and executes one montage job at a time
- Reports success or failure for every job
- Provides Pause/Resume from Photoshop menu commands as well as the panel

Photoshop is a stateless worker: the service decides what to build, Photoshop only knows how to build it.

### 2.1 Communication: plain HTTP, plugin always initiates

No WebSocket is needed because the UI lives in the plugin. The plugin polls the service on localhost.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Service alive check; plugin shows an "offline" banner if it fails |
| `GET /state` | Queue tree, counters, log rows, current mode (polled every 1 to 2 s) |
| `GET /next-job` | Returns the next job JSON, or none (only called while running) |
| `POST /job-result` | Plugin reports success or failure for a job ID |
| `POST /control` | play, pause, stop, force\_batch, set\_after\_finish, cancel\_after\_finish |
| `GET /settings` and `POST /settings` | Read and save configuration |
| `GET /csv` | Export the session or master log |
| `GET /thumb` | Small thumbnail for hover previews (optional, see Privacy) |

Rules:

- Bind to 127.0.0.1 only and require a shared token header on every request.
- Fixed port with a configurable fallback.
- Single worker only (the queue must have exactly one owner). Run the Flask server with a threaded server and guard shared state with a lock; keep the queue worker logic in the service, not in request handlers.
- Every job message is idempotent: a resend never causes a double build.

### 2.2 Service lifecycle

1. Preferred: the plugin launches the exe through the UXP `launchProcess` permission. **This must be verified first** on the target Photoshop version, because UXP may not be able to launch an exe or pass arguments.
2. Fallback A: the exe starts at Windows login (Startup shortcut or Task Scheduler) and idles until Photoshop appears.
3. Fallback B: a small UXP hybrid (native C++) module that can launch processes freely.

The exe does not need Photoshop's process ID. It finds `Photoshop.exe` by name using `psutil`, waits for it to appear, and exits after it has been gone for a grace period (30 to 60 seconds) so a quick Photoshop restart does not drop the connection.

Other requirements:

- Single-instance guard (named mutex or port lock). A second copy exits quietly.
- Windowed build (no console), logging to a file.
- Because state lives on disk, the service exiting with Photoshop loses nothing.

## 3. Core Workflow

1. **Scan and Log:** the service reads the "Before acceptance" directories and maps the file tree.
2. **Queueing ("Wait for 4"):** designs from the same clinic and paper weight are pooled until 4 are available.
3. **Montaging:** the plugin receives a job and places four 25x17 cm designs onto the 50x34 cm canvas in a deterministic quadrant order.
4. **Exporting:** two TIFFs are saved: `_PRINT.tif` (clean) and `_TRACKER.tif` (watermarked). Both are written to temporary names first.
5. **Verification:** the service confirms both files exist, have plausible size, and open correctly, then renames the temp files to final names.
6. **Cleanup:** only after verification, the source designs move to the Finished folder.
7. **Record:** batch details, including which design sat in which quadrant, are appended to the CSV log.
8. **After-Finishing action:** if one is armed and the queue has drained, it runs (see section 5).

## 4. UI/UX Architecture (3 panels, UXP React)

### Panel 1: Setup and Execution (left sidebar)

- **Folders:** Source (Before Acceptance) and Destination (Printing/Finished).
- **Naming convention builder:** token template, for example `[Clinic]-[Paper_Weight]-B[Batch_No]_[Type]`, with a live preview and a dry-run test.
- **Output handoff:** send Print and/or Tracker files to a RIP hot folder or the production printer. Direct printer driving from UXP is unlikely to be feasible; a hot folder is the primary route. A Printer Setup button can open the system printer settings.
- **Advanced settings:** canvas dimensions, bleed and margin, DPI, color profile, crash recovery protocol.
- **Run controls:** Play, Pause, Stop (see 4.4 for exact behavior).
- **After finishing:** dropdown plus Arm/Cancel (see section 5).

### Panel 2: Holding Queue (center tree view)

- Expandable tree: Clinic, Paper Weight, Doctor.
- Status badges: Finished (green), Processing (blue/spinner), Waiting (gray), Needs review (red).
- Counters such as `[ 2/4 - WAITING ]` and `[ 4/4 - READY ]`, plus an age indicator for queues that have been waiting a long time.
- **Force Batch** button next to waiting queues to process an incomplete batch (for example 3 files) when urgent.
- Hover thumbnails (optional, see Privacy).

### Panel 3: Live Progress and Logs (bottom drawer, collapsible)

- Session table: Time, Clinic, Doctor, Batch number, Status.
- Activity feed collapsed by default into a details view; a single clear status line is always visible.
- Data export: Save CSV As, Open Auto-Save Folder, and an option for silent appending to a master daily CSV.

### 4.4 Pause and Resume control (new)

Pause must be usable from inside Photoshop so the operator can work manually and then resume without losing place.

- **Panel buttons:** Play, Pause, Stop.
- **Photoshop menu commands:** the plugin registers command entries under the Plugins menu: *Pause Queue*, *Resume Queue*, and *Toggle After-Finishing Action*. This lets the operator pause even when the panel is hidden. Keyboard shortcut assignment for these should be tested on the target Photoshop version.
- **Behavior:**
  - **Pause** means finish the current batch, then stop. The UI shows "Pausing..." until the current job completes, then "Paused". A montage is never interrupted mid-build.
  - **Resume** continues from the saved queue position.
  - **Stop** finishes the current batch and then stops the queue, and disarms any After-Finishing action.
- The paused state is saved in `session_state.json`, so it survives a crash or restart.
- While paused, the plugin only touches its own documents and never closes or modifies documents the operator opened manually.
- Optional later addition: a **Pause now** that aborts the current batch and marks it interrupted, handled by the same recovery protocol.

## 5. After-Finishing Action (new)

An optional action that runs automatically when the work is done, useful for overnight runs.

### 5.1 Available actions

- Do nothing (default)
- Close Photoshop
- Lock the computer
- Sleep
- Hibernate
- Log off
- Restart
- Shut down

### 5.2 Trigger definition

"Finished" means the queue has drained: no batch is in progress and no batch of 4 (or a forced batch) is ready. Incomplete waiting groups (for example 3 of 4) do not block the trigger, but the action's confirmation dialog lists them so the operator knows they are still pending. A setting lets the operator choose whether waiting groups should prevent the action.

### 5.3 Safety rules

- **Cancelable countdown:** a visible countdown (default 60 seconds, configurable) in the panel before any power action, with a Cancel button. The service starts the system command with a delay so it can also be aborted from the system side.
- **Do not fire on problems:** if any batch failed or is flagged for review, the action does not run, and the panel shows an alert instead.
- **Disarm after use:** the action runs once and then resets to Do nothing, so a later session is never shut down unexpectedly. The operator can choose "remember" if they really want it permanent.
- **Pause and Stop** disarm or suspend the action.
- **Unsaved work:** before closing Photoshop or powering off, the plugin closes only the documents it created. If the operator left their own unsaved documents open, the action is skipped and the operator is warned, rather than risking lost work.
- **Order of operations:** flush and verify CSV and `session_state.json`, then close Photoshop (if selected), then the service exits, then the system command runs.
- The armed action is stored in `session_state.json`, so it survives a service restart, and an After-Finishing event is written to the CSV log.

### 5.4 Implementation notes

- The service executes the action (Windows `shutdown` command with a delay and abort, plus lock/sleep/log-off equivalents), not Photoshop.
- Windows only in the first version.
- Sleep and hibernate can be blocked by system policies; report failure instead of assuming success.

## 6. Technical Engine Features

### 6.1 Asynchronous queue

Processing happens in controlled steps: take a job, run it, report result, take the next. Pause, Stop, and After-Finishing hook into the gaps between jobs.

### 6.2 Job leases

When the plugin takes a job, the service marks it in-flight with a timestamp. If no result arrives within a timeout, the job is treated as interrupted. This also catches a hung Photoshop.

### 6.3 Crash recovery and state saving

- The service saves state to `session_state.json` continuously, using atomic writes (write a temp file, then rename).
- On restart it detects the file and asks to resume.
- TIFFs are written to temp names and renamed only after verification, so partial files never look valid.
- Recovery protocols for an interrupted batch:
  - **Auto-Restart:** discard the temp outputs and rebuild the batch of 4.
  - **Flag for Review:** highlight the batch in red and require human verification before retrying.
- Source files are moved to Finished only after both outputs are verified.
- Batch counters are persisted so numbers are never duplicated after a crash.

### 6.4 File handling

- Polling with a "file is stable" check (size unchanged across two polls) before a file is queued.
- Duplicate detection for re-submitted designs.
- Unrecognized files go to an Unsorted/Errors bucket with a visible warning.
- Disk space check before starting and before each batch.

## 7. Input and Output Specifications

- **Input:** individual prescription designs (PSD and similar), 25x17 cm.
- **Base canvas:** 50x34 cm, 2x2 grid.
- **Open decision:** edge-to-edge placement leaves no bleed, crop marks, or gutter. Decide whether designs already include bleed and whether the canvas grows (for example to 51x35) to hold marks and tracker text.
- **Resolution and color:** define DPI, color mode (CMYK or RGB), ICC profile, TIFF compression, and flattening. Validate every input and flag mismatches before processing.
- **Output 1 (`_PRINT.tif`):** clean, print-ready file for the RIP.
- **Output 2 (`_TRACKER.tif`):** identical file with a 15 to 20 percent opacity diagonal watermark or gutter text showing the Clinic Name and Batch Number for the guillotine operator.
- **Quadrant order:** deterministic (by filename or timestamp) and recorded in the CSV.

## 8. Privacy and Compliance

- Prescriptions contain patient and doctor data. Review what appears in hover thumbnails, the activity feed, and CSV logs.
- Make thumbnails optional or blurred.
- Decide where CSVs are stored, who can read them, and how long they are retained, according to local regulations.
- Ensure no patient data lands on shop-floor visible output (tracker text should be clinic and batch only).
- The service binds to localhost only and requires a token.

## 9. Technology Stack

| Layer | Choice |
| --- | --- |
| Photoshop side | UXP plugin, React, command entry points for pause/resume |
| Service | Python with Flask, single worker |
| Helpers | `psutil` (Photoshop process watch), `tifffile` or Pillow (output verification), standard library for CSV, JSON, and atomic writes |
| Packaging | PyInstaller (prefer `--onedir`, windowed build); Nuitka as an alternative |
| Distribution | Code-sign the exe or whitelist it, to avoid antivirus and SmartScreen warnings |
| Transport | HTTP on 127.0.0.1 with a token |

## 10. Validation Before Building

These are the main unknowns; test them first.

1. Can UXP launch the exe (and from which Photoshop versions)? If not, use Fallback A or B.
2. Can the Photoshop step run in isolation: four paths in, two correct TIFFs out (DPI, profile, watermark), and how long does opening four large PSDs take?
3. File access: do granted folders and paths work reliably across Photoshop restarts?
4. Can plugin menu commands be assigned keyboard shortcuts on the target version?
5. Does the production printing path work through a RIP hot folder?

## 11. Suggested Build Order

1. Photoshop montage step in isolation.
2. Flask service with queue, state file, and recovery, tested with a fake worker.
3. Connect them over HTTP with a minimal panel.
4. Full UI, pause/resume commands, CSV logging, After-Finishing actions.
5. Packaging, signing, and an installer or setup guide.
6. Fixture-based tests for montage placement, naming, and recovery.

## 12. Nice-to-Have Features

- Preflight validation report per file (dimensions, DPI, color mode, bleed).
- Reprocess or undo a finished batch.
- Dry-run mode for naming and montage on sample files.
- Age alerts for groups waiting too long.
- Prominent error states (corrupt file, missing font, disk full, service offline).