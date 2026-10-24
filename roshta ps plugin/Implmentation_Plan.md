# Plugin Implementation Plan: Roshta Photoshop UXP Plugin

This document is the plugin counterpart to the Server Implementation Plan. It defines the architecture, contract with the finished server (Phases 1 to 5, 154 tests passing), build phases, and testing strategy for the **Roshta UXP plugin**.

Date: 2026-10-22. Scope: Photoshop UXP panel plus Plugins-menu commands. The server owns every decision; the plugin owns the screen and the Photoshop work.

---

## 1. Role and Principles

- **The plugin is a thin client and a worker.** It never decides names, paths, batch numbers, slot order, or what to skip. It renders server state, sends operator commands, and executes one leased job at a time.
- **The server mode is the source of truth.** The plugin keeps no copy of queue state that could disagree with the server. After a panel reload it simply asks again.
- **Photoshop work is isolated behind an interface.** A `MontageEngine` interface (`build(job)` returns the output files) has two implementations: the real Photoshop engine and a fake one for tests. The whole run loop can then be tested without Photoshop, mirroring the server's `fake_plugin.py`.
- **Never touch the operator's own documents.** The plugin tracks the document ids it creates and closes only those.
- **Fail loudly and report precisely.** Every failure maps to an error code sent in `/job-result`, so the server can apply its recovery protocol.

## 2. Architecture

```
                 Photoshop (UXP host)
  ╔══════════════════════════════════════════════════════════╗
  ║  VIEW  (src/ui/)                                        ║
  ║  React panel + Plugins-menu UI only                     ║
  ║  SetupPanel │ PlanRunPanel │ RunBar │ LogsDrawer         ║
  ║  FilesTree │ BatchCard │ SlotGrid │ Badge │ Dialogs      ║
  ║  ► reads store.getState()    ► dispatches action objects ║
  ╚══════════════════╤══════════════════════════════════════╝
                     │ dispatch(action)  ▲ re-render on change
  ╔══════════════════▼══════════════════════════════════════╗
  ║  MODEL  (src/models/)                                   ║
  ║  Pure JS data shapes — no logic, no side effects        ║
  ║  AppState │ Job │ BatchPlan │ Settings                  ║
  ║  factory functions, typedefs, field validators only     ║
  ╚══════════════════╤══════════════════════════════════════╝
                     │ store holds AppState; reducer mutates it
  ╔══════════════════▼══════════════════════════════════════╗
  ║  CONTROLLER  (src/core/ │ src/api/ │ src/commands/)     ║
  ║  All logic — no React, no photoshop imports             ║
  ║  store.js │ poller.js │ runLoop.js │ config.js          ║
  ║  jobExecutor.js  │  errors.js                          ║
  ║  api/client.js   │  api/endpoints.js                   ║
  ║  commands/pause  │  commands/resume  │ afterFinish      ║
  ╚══════════╤════════════════════════════╤════════════════╝
             │                            │
  ╔══════════▼══════════╗    ╔════════════▼══════════════╗
  ║  API Adapter        ║    ║  PS Adapter (src/ps/)     ║
  ║  fetch 127.0.0.1    ║    ║  montageEngine.js         ║
  ║  X-Token header     ║    ║  canvas.js  watermark.js  ║
  ║  timeout / retry    ║    ║  docGuard.js  fileio.js   ║
  ╚══════════╤══════════╝    ╚═══════════════════════════╝
             │ HTTP + X-Token
      Roshta server (Flask / Waitress)
```

### 2.1 Layer rules

- **VIEW** (`src/ui/`) reads the store and dispatches plain action objects. No `fetch` calls and no `photoshop` imports inside any component.
- **MODEL** (`src/models/`) holds only data shapes, factory functions, and field validators. It imports nothing from other layers and has no side effects.
- **CONTROLLER** (`src/core/`, `src/api/`, `src/commands/`) owns all logic. Pure JavaScript — no `photoshop`, no `uxp`, no React — so the entire controller layer runs under Jest without Photoshop.
- **PS Adapter** (`src/ps/`) is the only code that imports `photoshop` or `uxp`. It implements the `MontageEngine` interface consumed by `jobExecutor.js` and is swapped for `FakeEngine` during tests.
- **One-way data flow:** VIEW dispatches action → CONTROLLER reducer updates → MODEL (AppState) changes → VIEW re-renders.

### 2.2 Technology choices

- UXP manifest v5, React for the panel, bundled with webpack (`photoshop` and `uxp` as externals).
- Plain flexbox layout. UXP CSS is narrower than a browser (no CSS grid), and Spectrum support is partial, so keep the component set small and use custom components for the tree.
- Theme through the host colour variables so the panel follows Photoshop light and dark themes.
- Token stored in UXP secure storage, never in plain `localStorage`, never logged.

### 2.3 MVC Mapping

The plugin follows a strict MVC pattern. Every layer has a single, enforced responsibility:

| MVC Layer | Folder | Responsibility |
| --- | --- | --- |
| **Model** | `src/models/` | Pure JS data shapes. No logic, no side effects. Defines state types (`AppState`, `Job`, `BatchPlan`, `Settings`), factory functions, and typed-object validators. The store holds one instance of `AppState`; everything else is derived from it. |
| **Controller** | `src/core/` + `src/api/` + `src/commands/` | All logic and orchestration. The store (reducer), poller, run-loop state machine, job executor, API client, config manager, and Plugins-menu command handlers live here. Controllers read from and write to the models; they never import React or any UI module. The Photoshop adapter (`src/ps/`) is also treated as a controller-level concern: it implements the `MontageEngine` interface consumed by the job executor. |
| **UI (View)** | `src/ui/` | React components only. They receive state by reading the store and dispatch plain action objects. No `fetch`, no `photoshop` imports. Components are split into panels (full regions) and components (reusable atoms). |

**Data flow (one direction only):**

```
User interaction
  │
  ▼
UI dispatches action  ──►  Controller (store reducer)
                                │
                                ▼
                           Model (AppState updated)
                                │
                                ▼
                        UI re-renders from new state
```

**Controller sub-modules and their model ownership:**

| Controller | Reads | Writes |
| --- | --- | --- |
| `config.js` | UXP secure storage | `settings` slice of AppState |
| `poller.js` | `bootId`, `stateRev` | `connectionStatus`, `serverMode`, `planTree`, `batches`, `counters`, `warnings` |
| `runLoop.js` | `serverMode`, `runLoopState` | `runLoopState`, `gapTargetMs` |
| `jobExecutor.js` | `activeJob`, `settings` | `activeJob`, `jobsCompletedThisSession` |
| `store.js` | — | Owns all AppState, exposes `dispatch(action)` and `subscribe(listener)` |
| `api/client.js` | `settings.host`, `settings.port`, `settings.token` | — (returns raw HTTP responses) |
| `api/endpoints.js` | — | Calls `client.js`, returns parsed JSON |
| `ps/montageEngine.js` | `JobContract` from model | Returns `BuildResult` consumed by `jobExecutor.js` |

## 3. Contract with the Finished Server

Base URL `http://127.0.0.1:<port>`. Every endpoint except `/health` needs the `X-Token` header.

| Plugin action | Endpoint | Plugin behavior |
| --- | --- | --- |
| Startup and heartbeat | `GET /health` | Show online or offline. Read `boot_id` and `resume_summary`. If resume work exists, fetch `/state` and open the resume dialog with the recommended protocol (`auto_restart` or `flag_for_review`). |
| Refresh the tree | `GET /state?rev=N` | `304` means keep the current view. `200` replaces plan, mode, counters, warnings (including `csv_locked`). If `boot_id` changed, drop the cached rev and refetch. Poll every 1 to 2 seconds. |
| Skip or include | `POST /selection` | Send the change with the last rev. On `409` redraw from the returned plan and tell the operator to repeat. `423` marks the node as locked. |
| Request work | `POST /lease` (preferred over `GET /next-job`) | Only while the server mode is `running` and no job is active. No job means wait and retry. One job at a time. |
| Keep lease alive | `POST /job-heartbeat` | Send between build stages (after each input opens, after each save), not only on a timer. |
| Report outcome | `POST /job-result` | Expect `202 Accepted`; finalization happens on the server. Watch `/state` for `done`, `flagged`, or `failed`. |
| Operator controls | `POST /control` | play, pause, stop, force\_batch, retry, mark\_reviewed, rescan. |
| Settings | `GET` and `POST /settings` | Folders, DPI, bleed, margins, naming template preview. |
| Export | `GET /csv` | Offer a download or open-folder action. |

**Error handling rules**

- `401`: show Token invalid and stop polling until fixed.
- Network error or timeout: show the offline banner, back off (1, 2, 4, up to 10 seconds), keep trying.
- `5xx`: treat like offline for polling; for `/job-result`, retry with the same payload (the server ignores repeats of a finished job).
- Never retry a lease request in a tight loop.

## 4. Run Loop State Machine

States: `OFFLINE`, `CONNECTING`, `IDLE` (server mode idle, paused, or stopped), `WAITING` (running, nothing ready), `EXECUTING`, `REPORTING`, `BLOCKED` (token or configuration problem).

```
OFFLINE --health ok--> CONNECTING --state ok--> IDLE
IDLE --server mode=running--> WAITING
WAITING --lease returns job--> EXECUTING
EXECUTING --build ok or error--> REPORTING
REPORTING --result accepted--> GAP (gap above 0 and more work ready) --countdown ends or Start now--> WAITING
REPORTING --result accepted--> WAITING (gap is 0) or IDLE (paused, stopped, or nothing ready)
any --network error--> OFFLINE
any --401--> BLOCKED
```

Rules:

- **Pause** is server-side. The plugin finishes the current job, then the server stops leasing. The panel shows Pausing until the job completes, then Paused.
- **Stop** finishes the current job and then idles; it never aborts a build mid-way.
- **One active job at a time.** A job in `EXECUTING` or `REPORTING` is never abandoned silently.
- **Plugin restart mid-job:** the document work is lost. On startup the plugin should report any remembered active job as failed with the code `PLUGIN_RESTARTED` so the server's recovery protocol runs sooner than waiting for the lease reaper. If the server's behavior differs, the reaper still reclaims the lease.
- **Per-stage modal scopes:** each build stage (create canvas, open and place one input, save PRINT, save TRACKER) runs in its own core.executeAsModal call instead of one long scope. Between stages the Photoshop UI, the Plugins-menu commands, and the panel work normally, which also gives natural points for heartbeats and for checking whether Pause was pressed. Individual native operations (for example opening a large PSD) still block while they run. The cost is that the operator could touch the plugin-owned canvas between stages, so the document guard verifies at the start of every stage that the canvas exists, is unchanged, and is the expected document.

* **GAP state (new):** between jobs the plugin can wait a configurable number of seconds (Advanced setting, default 0 = no wait) before asking for the next lease. The run bar shows Next batch in Ns with Start now and +30s buttons.
* **Why the gap helps:** no modal scope is active during it, so the Plugins-menu commands, the panel, and the operator's own work in Photoshop all function normally, and Pause takes effect immediately with no Pausing delay.
* **Gap rules:** skip the gap when nothing else is ready (so an After-Finishing action is not delayed); compute the countdown from a target timestamp, not from ticks, so it stays accurate when Photoshop is busy; hold no lease during the gap, so no heartbeat or expiry is involved; cancel the countdown if the server mode becomes paused or stopped or the server goes offline; keep the gap out of the job duration written to the CSV; let the document guard re-check at the start of the next job that the plugin touches only its own documents and restores the operator's active document.
* **Pause after every N batches (new, optional):** the plugin counts completed jobs and sends a pause control when the count is reached, so an operator can spot-check prints at set intervals. The counter resets on Play. 0 = off.
* **Pause pressed during a job:** this depends on the P0 result. If the panel buttons stay clickable during a stage, the request is sent at once and the server shows Pausing. If they do not, the plugin records the request and sends it as soon as the current stage scope ends.

## 5. The Photoshop Engine (highest-risk part)

This is built and proven first (Phase P2), standalone, with hard-coded paths.

### 5.1 Build steps per job

1. Validate before touching Photoshop: every input path exists, and the output and temp folders are writable.
2. **Stage A (own modal scope):** create the canvas document, 50 x 34 cm at the job DPI and colour profile, white background. Register it with the document guard.
3. **Stage B (one modal scope per input):** check the guard, open the source PSD, check its size is 25 x 17 cm at the expected DPI, copy it into the canvas at the slot offset (TL, TR, BL, BR), close the source without saving. Send a heartbeat after each input.
4. Empty slots (force batch) are left white. The server pads partial batches, so the engine must accept fewer than four inputs and never draw in an empty slot.
5. **Stage C (own modal scope):** check the guard, flatten, and save PRINT in the configured format to the temp path from the job. TIFF: no layers, profile embedded, compression as configured. JPEG: quality as configured, profile embedded, 8-bit RGB. Send a heartbeat.
6. **Stage D (own modal scope):** check the guard, add the tracker watermark (a diagonal text layer with the clinic name and batch number at 15 to 20 percent opacity from the job), flatten, and save TRACKER in its configured format to its temp path. Send a heartbeat.
7. Close the canvas document without saving. Restore the operator's previously active document.
8. Return the output list with type, path, format, and file size for /job-result.

### 5.2 Details to settle in the spike

- **Moving pixels between documents:** compare duplicating a flattened layer into the canvas via batchPlay against placing the file as an embedded smart object and rasterizing. Choose the one that is fastest and preserves colour without resampling. Avoid the clipboard, which is fragile.
- **No resampling:** when the input already matches 25 x 17 cm at the right DPI, place at 100 percent. A mismatch is an error, not something to rescale silently.
- **Fonts and RTL text:** a missing font or right-to-left text (for example Arabic clinic names) can render wrongly. Test the watermark with real clinic names, and enable the correct text engine if needed.
- **History:** suppress history states so a long run does not bloat memory.
- **Colour:** assign or convert to the job profile and confirm the TIFF carries it. The server's verifier only checks dimensions, DPI, page count, and a non-blank image.
- **Timing:** measure opening four real PSDs and both saves. This sets the lease length and heartbeat spacing.

* **Per-stage scopes:** measure total job time with per-stage scopes against one long scope, and confirm the extra scope overhead is acceptable.
* **Panel clickability:** confirm whether the panel buttons stay clickable while a stage scope runs (this decides how Pause behaves mid-job), and that the Plugins-menu commands work between stages.
* **JPEG output:** Photoshop must write DPI metadata that the server JPEG verifier can read. The verifier falls back to 72 DPI when the metadata is missing, which would fail a 300 DPI check. Also confirm 8-bit RGB conversion and that the profile is embedded.
* **Temp path extension:** keep the real extension last (for example name.part.tif, not name.tif.tmp) so Photoshop and the server verifier factory both pick the right format.
* **Output size:** compare file sizes and save times for TIFF (none, LZW, ZIP) and JPEG at the sheet size.

### 5.3 Error codes returned to the server

`INPUT_MISSING`, `INPUT_OPEN_FAILED`, `INPUT_SIZE_MISMATCH`, `FONT_MISSING`, `PROFILE_FAILED`, `SAVE_FAILED`, `DISK_FULL`, `PS_BUSY`, `PLUGIN_RESTARTED`, `UNKNOWN`. Confirm which codes the server accepts or maps (see section 10).

Add CANVAS\_MODIFIED: the plugin-owned canvas document was closed or changed between stages. Confirm the server maps unknown codes to a generic failure and stores the message.

## 6. User Interface

Three regions: **Setup** (settings only; it saves configuration and never starts a run), **Plan and Run** (centre; the run bar, the plan views, and all editing), and the **Logs drawer**. Keep it narrow-panel friendly, with a wide floating window as the roomy layout.

### 6.1 Setup (settings only, top or left)

The Setup panel only edits and saves settings. It has no Play, Pause, or Stop; the run is controlled from the Plan and Run panel (6.4). Settings are grouped; each group has an Apply button, shows per-field errors returned by the server, and has Revert and an unsaved-changes mark. Output settings lock while a job is leased or running and apply to future batches only.

**Connection (collapsible)**

- Status dot (online, offline, token problem) with server version, uptime, and Photoshop detected, from /health.
- Port and token fields. The token is kept in secure storage and is never shown again after saving.
- Test connection: calls /health, then an authenticated call, to tell a server that is down from a wrong token.
- Launch service: only if the P0 spike proves launchProcess works; otherwise show the login-startup hint.

**Folders (editable when idle, paused, or stopped)**

- Source: root the scanner watches (Clinic/Paper/Doctor.psd). Browse opens the folder picker; the plugin sends the path and the server validates it (exists, readable).
- Finished: where source PSDs move after a successful batch. The server checks it is writable, differs from Source, and has free space.
- Print and tracker output folders, with free disk space shown.
- Hot folders for the RIP and for slips.
- Each path shows a valid or invalid mark with the server's reason. Changing Source triggers a rescan and rebuilds the plan, so ask for confirmation first.

**Naming template**

- A text field with clickable token chips: \[Clinic\], \[Paper\_Weight\], \[Batch\_No\], \[Type\].
- A live preview with sample values, using the extension of the chosen output format. The template itself never contains an extension.
- Rules: \[Type\] and \[Batch\_No\] must be present (otherwise PRINT and TRACKER can overwrite each other or batches collide) and illegal filename characters are rejected. The server owns resolution; the plugin only shows the preview.
- Applies to future batches only and never renames existing files.

**Output type and handoff**

| Setting | Options | Notes |
| --- | --- | --- |
| Print format | TIFF (default) or JPEG | Warn on JPEG: lossy and 8-bit only, which most RIPs dislike for production |
| Tracker format | TIFF or JPEG | Set separately from print; JPEG is a sensible choice here |
| TIFF options | Compression (none, LZW, ZIP), embed profile |  |
| JPEG options | Quality, embed profile |  |
| Tracker output | On or off |  |
| Delivery address slip | Off by default | When on: if an address is missing, flag the batch or skip the slip with a warning |
| Copy to hot folder | One toggle per output, each with its folder | Replaces auto-send to printer (UXP cannot drive printers reliably); Printer Setup becomes Hot folder settings |

**Advanced (collapsed)**

- Output (locked while a job runs): canvas width and height in cm (default 50 x 34), DPI (300), colour profile, bleed and margin (the edge-to-edge decision is still open, so show the computed pixel size and slot offsets), watermark opacity (15 to 20 percent) and text.
- Behavior (editable any time): recovery protocol (Auto-Restart or Flag for Review), minimum free disk space (default 5 GB), gap between jobs in seconds (default 0), and pause after every N batches (default off). The gap and N settings are plugin-side.

**After finishing**

- Action: Do nothing (default), Close Photoshop, Lock, Sleep, Hibernate, Log off, Restart, Shut down. Countdown seconds (default 60), and a toggle for whether incomplete waiting groups block the action.
- Arm and Disarm button with an Armed indicator. The action runs once when the queue drains, after a cancelable countdown, and then disarms. It does not run if any batch failed or is flagged.
- Editable any time. Depends on server support that is not confirmed (section 10).

**Footer (read-only)**

Mode chip (Idle, Running, Paused, Stopped), a Rescan now button (control rescan), and validation errors from the last save.

**Ownership**

The server owns folders, template, outputs, formats, and canvas settings. The plugin owns the connection, the gap, and pause-after-N.

### 6.2 Plan and Run (centre)

This is where the operator previews, edits, and runs. From top to bottom: the run bar (6.4), a toggle between two views of the same server plan, a detail strip, and a warnings strip.

**Files view (the tree)**

- Clinic, Paper Weight, Doctor file, with a checkbox on every node to skip or include. Skipped nodes show the reason (for example skipped by folder).
- Each row shows a status badge (Finished green, Processing blue, Waiting grey, Needs review red), size, and age.
- Selecting a file opens the detail strip: full path, how it was parsed (clinic, paper, doctor), and any warning such as wrong dimensions.
- Collapse by default and render only expanded nodes, since there may be hundreds of files.

**Batches view (the linking)**

Shows what the server planned, one card per batch, grouped by clinic and paper weight:

```
Clinic A / 80g           Batch 1 of 3 (provisional)      READY 4/4
  [TL Dr Ahmed ] [TR Dr Mona  ]    PRINT   ClinicA-80g-B?_PRINT.tif
  [BL Dr Sarah ] [BR Dr Ziad  ]    TRACKER ClinicA-80g-B?_TRACKER.jpg
                                   Slip    on, address OK
Clinic A / 80g           Leftover        WAITING 2/4     [Force batch]
```

- Real batch numbers appear only once a batch is leased; until then a provisional label is shown.
- Cards show state (ready, leased with a lock, done), planned output names with their formats, slip status, and warnings (missing address, size mismatch).
- The plugin never computes grouping. It renders what the server returns and shows the server's ordering rule in one line (confirm the rule, for example oldest first).
- Retry and Mark Reviewed appear on failed, interrupted, or flagged batches. Force Batch on a leftover group asks for confirmation.

**Editing the groupings (needs server Phase 5b, section 13)**

- Move a file to another batch or slot with a Move to menu (do not rely on drag and drop in UXP), swap two slots, pin a batch so auto-planning leaves it alone, and Reset to auto for a group.
- Leased and finished batches are locked and show a lock.
- Server rules: only files of the same clinic and paper weight can share a batch; at most 4 per batch; no duplicates; a pinned batch with fewer than 4 files waits until forced.
- Highlight batches that changed because a new file arrived. Unpinned batches may re-plan; pinned ones do not.
- Every edit sends the last rev; handle 409 (redraw from the returned plan), 423 (locked), and 422 (invalid move).

**Not in the first release:** hover thumbnails, because the server has no thumbnail endpoint.

### 6.3 Progress and Logs (bottom drawer, collapsible)

- Session table: Time, Clinic, Batch number, Status (and Doctor if the privacy setting allows).
- One always-visible status line; the detailed activity feed is expandable.
- Buttons: Save CSV As, Open Auto-Save Folder, and the master log option.
- Warning area for `csv_locked`, low disk, unrecognized files, and missing addresses.

### 6.4 Run bar (docked in the centre panel)

This replaces the earlier floating-controls note. The bar is docked, not floating: UXP plugins get panels and modal dialogs, not free windows, and an overlay would cover tree rows and badges. It has reserved height at the bottom of the Plan and Run panel, above the logs drawer, and moves with the drawer.

- Status chip: Running, Working (batch B00042, 2/4 placed), Pausing..., Paused, Stopped, Offline, Token problem.
- One primary button that toggles Play, Pause, and Resume, so two buttons are never confusingly active together.
- Stop, labelled Stop after this batch. It also disarms an armed After-Finishing action.
- Compact counters (ready and waiting).
- Gap countdown: Next batch in Ns with Start now and +30s.
- The armed After-Finishing action with its countdown and Cancel.
- Narrow panel: collapse to icons with tooltips.

| Server or plugin state | Primary button | Stop |
| --- | --- | --- |
| Offline or token problem | disabled, shows the reason | disabled |
| Idle or stopped | Play | disabled |
| Running (waiting or working) | Pause | enabled |
| Pausing | Pausing... (disabled) | enabled |
| Paused | Resume | enabled |

Rules:

- Disable a button after a click until the server mode confirms the change; the server stays the source of truth.
- The Plugins-menu Pause and Resume commands and this bar call the same core function.
- Whether these buttons stay clickable during a build stage is a P0 test; the fallback is described in section 4.

### 6.5 Dialogs and banners

**Blocking dialogs** (UXP dialogs are modal): Resume previous session (apply the recommended protocol, or review first, with the interrupted batches listed), Force Batch confirm, After-Finishing countdown with Cancel, and Source folder change confirm.

**Inline banners** (never modal): Service offline, Token invalid, csv\_locked, low disk, unrecognized files, and missing addresses.

**Job failures are not modal.** Use a red badge plus a detail view with the error code, message, and Retry, Mark Reviewed, and Open Log actions, so an overnight run cannot stack dialogs.

**Banner priority** when several apply: Token invalid, then Offline, then job failures, then warnings.

### 6.6 Plugins-menu commands

Register command entry points: Roshta Pause, Roshta Resume, and Toggle After-Finishing. They run in the same plugin context as the panel, so they call the same core module and work even when the panel is hidden. Keyboard shortcut assignment must be tested; do not rely on it.

Pause and Resume here and in the run bar call the same core function. The menu commands may be unavailable while a build stage is running; the run bar and the windows between stages are the reliable path (P0 test).

## 7. Project Layout

The directory structure reflects the MVC split exactly. Every top-level `src/` folder belongs to exactly one layer.

```
roshta ps plugin/
├── plugin/
│   ├── manifest.json        # UXP v5: panels + commands, network 127.0.0.1, fullAccess file system
│   ├── index.html           # UXP host page (loads dist/index.js)
│   └── icons/               # dark / light panel icons
├── src/
│   │
│   ├── index.jsx            # Entry point: wires panels + commands into entrypoints.setup()
│   │
│   ├── ── MODEL LAYER ──────────────────────────────────────────────────────
│   │
│   ├── models/
│   │   ├── AppState.js      # makeDefaultState() — the single state shape the store holds
│   │   │                    #   slices: connection, serverMode, planTree, batches, counters,
│   │   │                    #           resumeSummary, runLoopState, activeJob, gapTargetMs,
│   │   │                    #           settings, activityFeed, afterFinish, uiEphemeral
│   │   ├── Job.js           # JobContract typedef + validateJobContract()
│   │   │                    #   buildSuccessResult(), buildErrorResult()
│   │   ├── BatchPlan.js     # PlanNode, BatchCard, SlotEntry, PlannedOutput typedefs
│   │   │                    #   normalizePlanTree() — maps raw server /state into typed objects
│   │   └── Settings.js      # makeDefaultSettings() — all server + plugin-owned settings fields
│   │                        #   validateSettings(patch) — returns per-field errors
│   │
│   ├── ── CONTROLLER LAYER ─────────────────────────────────────────────────
│   │
│   ├── core/
│   │   ├── store.js         # Singleton state store: dispatch(action), getState(), subscribe(fn)
│   │   │                    #   Pure reducer — no side effects; all actions are plain objects
│   │   │                    #   Action types: SET_CONNECTION, SET_STATE_REV, SET_PLAN,
│   │   │                    #     SET_RUN_LOOP, SET_ACTIVE_JOB, SET_SETTINGS, ADD_FEED_ROW,
│   │   │                    #     SET_DIALOG, SET_BANNERS, SET_RESUME_SUMMARY, etc.
│   │   ├── poller.js        # /health + /state polling with exponential backoff (1→2→4→10s)
│   │   │                    #   Detects boot_id change → drops rev cache → full re-fetch
│   │   │                    #   Dispatches SET_CONNECTION, SET_STATE_REV, SET_PLAN actions
│   │   ├── runLoop.js       # State machine: OFFLINE→CONNECTING→IDLE→WAITING→EXECUTING
│   │   │                    #               →REPORTING→GAP→WAITING (loop)
│   │   │                    #   Gap countdown uses target timestamp, not tick counting
│   │   │                    #   Exposed API: start(), stop(), pause(), resume(), forceGapEnd()
│   │   ├── jobExecutor.js   # Lease → build → heartbeat (per stage) → report → gap
│   │   │                    #   Uses MontageEngine interface; never imports photoshop directly
│   │   │                    #   pause-after-N logic; PLUGIN_RESTARTED report on startup
│   │   ├── errors.js        # Error code enum + mapPsErrorToCode(psError)
│   │   │                    #   Codes: INPUT_MISSING, INPUT_OPEN_FAILED, INPUT_SIZE_MISMATCH,
│   │   │                    #     FONT_MISSING, PROFILE_FAILED, SAVE_FAILED, DISK_FULL,
│   │   │                    #     PS_BUSY, CANVAS_MODIFIED, PLUGIN_RESTARTED, UNKNOWN
│   │   └── config.js        # Load/save connection settings from UXP secure storage
│   │                        #   token is never logged or placed in plain localStorage
│   │
│   ├── api/
│   │   ├── client.js        # fetch() wrapper: injects X-Token, timeout, retry for 5xx
│   │   │                    #   Maps HTTP status → structured { ok, status, data, error }
│   │   │                    #   401 → dispatches BLOCKED; network error → dispatches OFFLINE
│   │   └── endpoints.js     # One async function per server endpoint:
│   │                        #   getHealth(), getState(rev), postSelection(payload),
│   │                        #   postLease(opts), postHeartbeat(batchId, leaseId),
│   │                        #   postJobResult(result), postControl(command),
│   │                        #   getSettings(), postSettings(patch), getCSV()
│   │
│   ├── ps/                  # Photoshop adapter — only folder that imports `photoshop` / `uxp`
│   │   ├── montageEngine.js # MontageEngine interface contract + PhotoshopEngine class
│   │   │                    #   build(job): Promise<BuildResult>  — called by jobExecutor
│   │   │                    #   FakeEngine class for Jest tests (no Photoshop needed)
│   │   ├── canvas.js        # Stage A–D each in its own executeAsModal scope:
│   │   │                    #   A: createCanvas()  B: placeInput(slot)  C: savePrint()  D: saveTracker()
│   │   ├── watermark.js     # addWatermarkLayer(doc, text, opacity) — diagonal RTL-safe text
│   │   ├── fileio.js        # pathToEntry(absPath), entryToPath(), verifyWritable(folder)
│   │   └── docGuard.js      # register(docId), assertAlive(docId) → throws CANVAS_MODIFIED
│   │                        #   restorePreviousDoc() — called after job completes
│   │
│   ├── commands/            # Plugins-menu entry points (share the same core module context)
│   │   ├── pause.js         # Calls runLoop.pause(); works even when panel is hidden
│   │   ├── resume.js        # Calls runLoop.resume()
│   │   └── afterFinish.js   # Toggles afterFinishArmed in the store
│   │
│   ├── ── VIEW LAYER ───────────────────────────────────────────────────────
│   │
│   └── ui/
│       ├── App.jsx          # Root component; subscribes to store; renders active panel
│       ├── styles.css       # Global CSS variables from UXP host theme tokens
│       │
│       ├── panels/
│       │   ├── SetupPanel.jsx       # Settings only — no Play/Pause; groups: Connection,
│       │   │                        #   Folders, Naming, OutputType, Advanced, AfterFinishing
│       │   ├── PlanRunPanel.jsx     # Centre panel: RunBar + plan view toggle + warnings strip
│       │   ├── RunBar.jsx           # Docked status chip + Play/Pause/Stop + gap countdown
│       │   └── LogsDrawer.jsx       # Collapsible bottom drawer: feed table + CSV actions
│       │
│       └── components/
│           ├── FilesTree.jsx        # Virtualized clinic/paper/doctor tree with checkboxes
│           ├── BatchCard.jsx        # One card per batch: slots grid + output names + actions
│           ├── SlotGrid.jsx         # TL/TR/BL/BR slot display inside a BatchCard
│           ├── Badge.jsx            # Status badge: Finished, Processing, Waiting, Needs Review
│           ├── OfflineBanner.jsx    # Inline banner: Offline / Token invalid / csv_locked / disk low
│           ├── ResumeDialog.jsx     # Blocking dialog: interrupted batches + protocol choice
│           ├── CountdownModal.jsx   # After-Finishing cancelable countdown
│           └── NamingPreview.jsx    # Live template preview with token chips
│
├── test/
│   ├── models/              # AppState shape, Job validation, Settings validation
│   ├── core/                # store reducer, poller, runLoop state machine, jobExecutor + FakeEngine
│   ├── api/                 # client timeout/retry/auth, endpoint happy + error paths
│   └── tools/
│       ├── mock-server.js   # Express mock for scripted happy + error + race states
│       └── ps-mock.js       # stub of `photoshop` module for Jest
│
├── package.json             # React 16, webpack 4, babel, jest
└── webpack.config.js        # externals: uxp, photoshop, os; output: dist/index.js
```

### 7.1 What lives where — quick reference

| File | Layer | One-line purpose |
| --- | --- | --- |
| `models/AppState.js` | Model | Canonical state shape factory |
| `models/Job.js` | Model | Job contract + result builders |
| `models/BatchPlan.js` | Model | Plan tree + batch normaliser |
| `models/Settings.js` | Model | Settings shape + field-level validation |
| `core/store.js` | Controller | Single store, pure reducer, subscriber list |
| `core/poller.js` | Controller | Health + state polling with backoff |
| `core/runLoop.js` | Controller | 8-state run-loop machine |
| `core/jobExecutor.js` | Controller | Lease → build → heartbeat → report |
| `core/errors.js` | Controller | Error code enum + PS error mapper |
| `core/config.js` | Controller | Secure storage read/write |
| `api/client.js` | Controller | HTTP fetch adapter |
| `api/endpoints.js` | Controller | Server endpoint functions |
| `ps/montageEngine.js` | Controller (PS adapter) | MontageEngine interface + real + fake impl |
| `ps/canvas.js` | Controller (PS adapter) | 4 per-stage modal scopes |
| `ps/watermark.js` | Controller (PS adapter) | Diagonal tracker text layer |
| `ps/fileio.js` | Controller (PS adapter) | UXP path ↔ entry conversion |
| `ps/docGuard.js` | Controller (PS adapter) | Owned-doc tracker + canvas integrity |
| `commands/pause.js` | Controller (command) | Plugins-menu Pause |
| `commands/resume.js` | Controller (command) | Plugins-menu Resume |
| `commands/afterFinish.js` | Controller (command) | Plugins-menu Toggle After-Finishing |
| `ui/App.jsx` | View | Root, store subscriber, panel router |
| `ui/panels/SetupPanel.jsx` | View | Settings form (7 groups, Apply per group) |
| `ui/panels/PlanRunPanel.jsx` | View | Main plan + run area |
| `ui/panels/RunBar.jsx` | View | Status chip + primary control buttons |
| `ui/panels/LogsDrawer.jsx` | View | Collapsible activity feed |
| `ui/components/FilesTree.jsx` | View | Lazy-render tree |
| `ui/components/BatchCard.jsx` | View | One batch: slots + outputs + actions |
| `ui/components/SlotGrid.jsx` | View | TL/TR/BL/BR quadrant display |
| `ui/components/Badge.jsx` | View | Status colour chip |
| `ui/components/OfflineBanner.jsx` | View | Priority inline banners |
| `ui/components/ResumeDialog.jsx` | View | Modal recovery dialog |
| `ui/components/CountdownModal.jsx` | View | After-Finishing countdown |
| `ui/components/NamingPreview.jsx` | View | Template token chip + preview |

## 8. Build Phases

Phase numbers continue the master plan (server Phases 1 to 5 done; Phase 6 packaging still open).

**P0: Spike and contract check (do first, 1 to 2 days).** Prove in a throwaway panel: localhost fetch with the manifest network permission; custom X-Token header accepted; reading a path the server supplies (file access scope and entry conversion); persistent access after a Photoshop restart; timers and polling while the panel is hidden; launching the exe with launchProcess; menu commands sharing context with the panel; whether panel buttons and menu commands stay usable while an executeAsModal stage runs and between stages; the overhead of per-stage scopes; that a JPEG save writes the DPI metadata the server verifier reads; and temp path extension handling. Run the contract checks in section 10 against the real server. *Done when* each item has a pass or a chosen fallback written down.

**P1: Skeleton and connectivity (master Phase 10, part 1).** Manifest, bundler, API client, config in secure storage, poller with backoff, store, status dot and offline and token banners. *Done when* the panel shows live server state from the real server and recovers from a server restart (boot\_id change) without a reload.

**P2: Montage engine, standalone (master Phase 9).** Implement and prove the section 5 stages (A to D) from a bare panel with hard-coded paths and real PSDs, including a partial batch with empty slots and both output formats (TIFF with each compression option, JPEG at the chosen quality). Time every stage. *Done when* outputs from real inputs pass the server verifier in both formats and look correct to a human check, repeated 10 times with no leaked documents, and the document guard catches a canvas closed or edited between stages.

**P3: Run loop and job executor (master Phase 10, part 2).** State machine including GAP, lease, heartbeats between stages, result reporting, error mapping, PLUGIN\_RESTARTED handling, pause and stop behavior, pause-after-N, and the pause-during-job fallback. First against FakeEngine and the real server, then with the real engine. *Done when* 50 consecutive batches run end to end, and pause, stop, a forced failure, a gap with Start now and +30s, and a server pause during the gap all behave as specified.

**P4: Plan and Run UI (master Phase 11, part 1).** Run bar, Files view with skip checkboxes and badges, Batches view with cards, slots, planned output names and formats, counters, selection with 409 handling, Force Batch, Retry, Mark Reviewed, and Plugins-menu commands. Needs the server to return the plan (section 10). *Done when* an operator can skip a folder, read the planned batches, run a queue from the run bar, pause from the menu, and resume without losing place.

**Server Phase 5b (before P4b).** Plan editing endpoints, output format settings, and job contract changes; see section 13.

**P4b: Plan editing (needs server Phase 5b).** Move to menu, swap slots, pin and unpin, Reset to auto, changed-batch highlight, and 409, 423, and 422 handling. *Done when* a regrouped, pinned plan produces exactly the batches shown, survives a server restart, and the CSV records the slot order actually used.

**P5: Setup, logs, resume, after-finishing (master Phase 11, part 2).** Setup panel with grouped settings and Apply, output type selection (TIFF or JPEG per output) with the naming preview, Logs drawer, CSV actions, resume dialog from resume\_summary, countdown modal, and warnings area. *Done when* every dialog and banner state in the Figma prototype exists and is reachable, and changing the output format changes the planned output names in the Batches view.

**P6: Integration and hardening.** Long runs with real PSDs, forced Photoshop crash, forced server crash and restart, disk-full, Excel-locked CSV, an operator editing their own document while paused, a clinic name with unusual characters. *Done when* no scenario leaves an orphaned document, a duplicated batch, or a silent failure.

**P7: Packaging and deployment.** Build the `.ccx`, document the install, and wire it to the server exe launch or login-time startup once server Phase 6 is done. Write a short operator guide and a recovery checklist.

## 9. Testing Strategy

- **Unit tests (Jest, no Photoshop):** API client (status mapping, timeouts, backoff), poller, store reducer, run loop state machine (including all failure paths), job executor against `FakeEngine`, error mapping.
- **Contract tests:** run the plugin core against the real server with the fixtures from the server repo, including partial batches, `409`, `423`, `304`, restart (boot\_id change), and the lease reaper.
- **Engine tests (manual plus golden files):** a fixed set of four PSDs produces a reference TIFF; compare against the output with a tolerance. Repeat with a partial batch. Check no documents remain open and the operator's document is untouched.
- **UI tests:** a mock server with scripted states (every badge, offline, token error, resume, countdown) so each screen can be reviewed without real work.
- **Soak:** 50 batches with the real engine and 200 with the fake engine, watching memory and open documents.
- **Debugging:** UXP Developer Tool for loading, logs, and the inspector; keep a plugin-side rotating debug log that never contains the token.

* **Gap and pause tests:** countdown computed from timestamps, gap skipped when nothing is ready, Start now, +30s, server paused or offline during the gap, pause-after-N counting and reset on Play.
* **Mid-job interaction tests:** press Pause during each build stage; edit or close the plugin-owned canvas between stages and confirm the document guard reports CANVAS\_MODIFIED.
* **Output format tests:** TIFF and JPEG both pass the server verifier in a fixture run, including TIFF for print with JPEG for tracker; JPEG DPI metadata present.
* **Plan view tests:** the Batches view renders the server plan, provisional labels, locked batches, and a 409 redraw.
* **Plan editing tests (after Phase 5b):** invalid moves (cross-clinic, a fifth file, duplicates), pins surviving a restart, and a pinned file that is deleted.

## 10. Contract Questions to Confirm Against the Server

The status report is the basis for section 3, but these points are not stated there and need a quick check before P1 and P3:

1. **Job shape for partial batches:** does `inputs` contain four entries with empty ones marked, or only the filled slots? What does the plugin do for the empty slot positions?
2. **Re-asking for a lease** while one is active: does the server return the same job, return nothing, or refuse? This decides restart handling.
3. **`/state` not-changed reply:** confirmed as `304`. Confirm that `boot_id` is included in `/state` as well as `/health`.
4. **After-Finishing:** the status report lists no endpoint for setting, arming, or cancelling the action, and the control list omits `set_after_finish` and `cancel_after_finish`. The process adapter exists, so this may be pending. The plugin UI for it depends on the answer.
5. **Activity feed:** `/feed` is not in the endpoint matrix. Decide whether the plugin builds its feed from `/state` recent rows or the server adds `/feed`.
6. **Error codes** accepted in `/job-result`, and whether free-text messages are stored and shown.
7. **Output paths:** does the server guarantee the temp output folders exist, and are paths absolute with forward or back slashes?
8. **Settings scope:** which settings the plugin may change through `/settings` (naming template, folders, DPI, recovery protocol) and how they are validated.
9. **Token and port setup:** the plan assumes the token is entered once in plugin settings. Confirm the port is fixed or discoverable.
10. **Thumbnails:** no endpoint exists; confirm it is out of scope for the first release.

**Added with the Setup and Plan and Run design:**

- Does /state return the planned batches (slots, provisional labels, planned output names with format, warnings), or only the tree and counts?
- Which ordering rule fills the slots (for example oldest first)?
- Are print\_format, tracker\_format, tiff\_compression, jpeg\_quality, and embed\_profile accepted in /settings, and does the job carry a format per output?
- Do temp paths keep the real extension last (for example name.part.tif)? The server verifier factory depends on it.
- Does Photoshop write DPI metadata into JPEGs? The server JPEG verifier falls back to 72 DPI when it is missing.
- Rename the CSV header Print\_TIFF to a neutral Print\_File.

## 11. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| UXP cannot launch the exe | Plugin cannot own the service | Login-time startup; hybrid native module as a last resort |
| Montage too slow or colour shifts | Throughput or print quality | Spike timing first; fixed profile handling; golden-file comparison |
| Modal lock blocks the operator | Frustration, apparent freeze | Clear Working state; pause finishes the current job |
| Photoshop crash mid-job | Lost batch | Lease reaper, `interrupted` status, recovery protocol |
| Large tree is slow in UXP | Sluggish panel | Collapsed by default, render only expanded nodes |
| RTL or missing fonts in watermark | Wrong tracker text | Test early with real names; report `FONT_MISSING` |
| Token exposure | Local misuse | Secure storage, no logging, localhost only |

**Added risks**

- Panel not clickable during a build stage, so Pause is delayed. Mitigation: per-stage scopes, the P0 test, and the record-and-send-later fallback.
- Operator edits the canvas between stages or during the gap. Mitigation: document guard checks at every stage and at job start.
- JPEG chosen for production print degrades quality. Mitigation: TIFF default and a warning on JPEG for print.
- Plan edits conflict with newly arriving files. Mitigation: rev checks, 409 redraw, pinned batches.
- Server lacks plan editing endpoints. Mitigation: server Phase 5b; until then the Batches view is read-only.

## 12. Definition of Done for the Plugin

- An operator can install the plugin, enter the token once, see the live queue, skip files, press Play, and walk away.
- 50 consecutive real batches complete with correct TIFFs, correct CSV rows, sources moved to Finished, and no leftover documents.
- Pause, stop, resume, force batch, retry, and the resume-after-crash dialog all work from inside Photoshop.
- A Photoshop crash and a server crash each recover cleanly with no duplicate or lost batch.

* Print and tracker formats can each be set to TIFF or JPEG, and both pass verification.
* The Plan and Run panel shows the server's plan, skipping and regrouping change exactly the batches that are built, and the CSV records the real slot order.
* The gap counter and pause-after-N work, and Pause from the run bar or the menu behaves as designed in section 4.

## 13. Server Phase 5b: Plan Editing and Output Format Settings

The finished server supports skip and include only. The Plan and Run panel also needs custom grouping and output format settings. This is a small addition on top of server Phases 1 to 5.

### 13.1 Plan payload

/state returns, for every clinic and paper group: the ordering rule; the batches, each with a stable batch\_key, a provisional label, state, pinned flag, locked flag, and four slots (file id, doctor, path, parse info); the planned outputs for each batch (type, planned name, format); slip status; warnings; and a leftover list for files that do not fill a batch.

### 13.2 Proposed endpoints

| Endpoint | Body | Effect |
| --- | --- | --- |
| POST /plan/move | rev, file\_id, to\_batch\_key, to\_slot | Move a file into a slot of another batch (or a new batch) in the same clinic and paper group |
| POST /plan/swap | rev, two batch\_key and slot pairs | Swap two slots, within or across batches of the same group |
| POST /plan/pin | rev, batch\_key, pinned | Pin or unpin a batch |
| POST /plan/reset | rev, clinic, paper | Remove overrides for the group and re-plan automatically |

Responses: the new plan and rev; 409 for a stale rev; 423 for a locked batch; 422 for an invalid move.

### 13.3 Rules

- Only files of the same clinic and paper weight can share a batch.
- At most 4 files per batch, no duplicates, and skipped files cannot be placed.
- Leased, verifying, finalizing, and done batches are locked.
- A pinned batch with fewer than 4 files waits until forced.
- Unpinned batches re-plan automatically when files arrive or are skipped; pinned batches do not.

### 13.4 Persistence

- A plan\_overrides table in SQLite: group key (clinic, paper), batch position, ordered slot file ids, pinned flag, and updated time. Written in the same transaction as the plan change, so pins survive a restart.
- If a pinned file disappears, the pin is dropped and a warning is raised.
- The real batch number is still assigned at lease. Promotion uses the pinned arrangement exactly, and the CSV slot columns record the placement actually used.

### 13.5 Output format settings and job contract

- /settings keys: print\_format and tracker\_format (tif or jpg), tiff\_compression, jpeg\_quality, embed\_profile.
- Each job output carries its format. Temp paths keep the real extension last (for example name.part.tif), so the verifier factory and Photoshop both choose correctly.
- The naming template stays free of extensions; the server appends the extension from the format.
- The JPEG verifier DPI fallback of 72 should be treated as a failure only when the file truly lacks metadata; confirm Photoshop writes it.
- Rename the CSV header Print\_TIFF to Print\_File.

### 13.6 Tests

- Invalid moves (cross-clinic, a fifth file, duplicates, skipped file) return 422.
- Stale rev returns 409; locked batch returns 423.
- Pins survive a restart; a deleted pinned file drops the pin with a warning.
- A pinned plan is promoted exactly as shown, and the CSV records it.
- Mixed formats (TIFF print, JPEG tracker) pass verification in a fake-plugin run.
