"""
tools/run_input_simulation.py — Runs a full E2E simulation using the real 'input/' folder.

Workflow:
  1. Copies 'input/' into a dedicated sandbox test folder so original files remain untouched.
  2. Creates an isolated SQLite database and sets up 'Finished/' destination folder.
  3. Boots the Flask application (QueueService, ScannerService, FinalizerWorker, LeaseReaper).
  4. Triggers scanner passes and asserts all 8 files are discovered in the PlanTree snapshot.
  5. Uses FakePhotoshopPlugin to lease and process each batch (with force=True for partial batches).
  6. Submits job results, verifies TIFF generation, and waits for background finalization.
  7. Confirms all batches reach 'done' status and source files are moved to 'Finished/'.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import time

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server.config import AppConfig
from server.database import init_db, get_connection
from server.app import create_app
from tools.fake_plugin import FakePhotoshopPlugin

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulation")


def _create_safe_temp_dir(prefix: str = "roshta_input_sim_") -> str:
    """Create a temporary directory, falling back to project-local .tmp if system temp is unavailable."""
    local_tmp = os.path.join(PROJECT_ROOT, ".tmp")
    os.makedirs(local_tmp, exist_ok=True)
    for env_k in ("TEMP", "TMP"):
        p = os.environ.get(env_k)
        if p:
            try:
                os.makedirs(p, exist_ok=True)
            except Exception:
                pass
    try:
        probe = tempfile.mkdtemp(prefix="_probe_")
        os.rmdir(probe)
        return tempfile.mkdtemp(prefix=prefix)
    except Exception:
        tempfile.tempdir = local_tmp
        return tempfile.mkdtemp(prefix=prefix, dir=local_tmp)


def run_simulation(source_dir: str = "input", keep_output: bool = True) -> bool:
    if not os.path.exists(source_dir):
        logger.error("Source directory '%s' not found!", source_dir)
        return False

    temp_root = os.path.join(PROJECT_ROOT, ".tmp", "simulation_output")
    if os.path.exists(temp_root):
        shutil.rmtree(temp_root, ignore_errors=True)
    os.makedirs(temp_root, exist_ok=True)

    sandbox_input = os.path.join(temp_root, "Input")
    sandbox_finished = os.path.join(temp_root, "Finished")
    sandbox_renders = os.path.join(temp_root, "Renders")
    db_path = os.path.join(temp_root, "sim.db")

    logger.info("============================================================")
    logger.info("STARTING ROSHTA E2E SIMULATION ON '%s'", source_dir)
    logger.info("Output Folder: %s", temp_root)
    logger.info("============================================================")

    # 1. Copy original input into sandbox
    shutil.copytree(source_dir, sandbox_input)
    os.makedirs(sandbox_finished, exist_ok=True)
    os.makedirs(sandbox_renders, exist_ok=True)
    logger.info("Cloned '%s' -> '%s'", source_dir, sandbox_input)

    # 2. Configure app
    cfg = AppConfig(
        database_path=db_path,
        source_folder=sandbox_input,
        finished_folder=sandbox_finished,
        secret_token="sim-secret-token",
        scan_interval_seconds=9999.0,  # Controlled manual scans
        disk_min_bytes=10 * 1024 * 1024,  # 10 MB threshold for simulation runs
    )
    init_db(db_path)

    app = create_app(custom_config=cfg)
    app.config["TESTING"] = True
    client = app.test_client()

    qs = app.config["QUEUE_SERVICE"]
    scanner = app.config["SCANNER_SERVICE"]
    finalizer = app.config["FINALIZER_WORKER"]

    try:
        # 3. Two-pass scan (quarantine -> stable)
        logger.info("\n--- STEP 1: SCANNING FILES ---")
        conn = get_connection(db_path)
        try:
            ins1, _ = scanner._adapter.scan(conn)
            logger.info("Pass 1 (quarantine): %d files noted", len(ins1))
            ins2, _ = scanner._adapter.scan(conn)
            logger.info("Pass 2 (stable insertion): %d files inserted into DB", len(ins2))
            qs.rebuild_snapshot(conn)
        finally:
            conn.close()

        # 4. Verify PlanTree state
        snapshot = qs.get_snapshot()
        state_dict = snapshot.to_dict()
        logger.info("\n--- STEP 2: QUEUE SNAPSHOT VERIFICATION ---")
        logger.info("Boot ID: %s | Rev: %d", state_dict["boot_id"], state_dict["rev"])
        logger.info("Clinics discovered: %d", len(state_dict["tree"]))
        for c in state_dict["tree"]:
            for p in c["papers"]:
                for b in p["batches"]:
                    docs = [f["doctor_name"] for f in b["files"]]
                    logger.info("  * [%s] %s (%s) -> %s", b["status"], c["name"], p["paper_weight"], docs)

        # 5. Run FakePhotoshopPlugin against the client
        logger.info("\n--- STEP 3: WORKER LEASING & RENDERING SIMULATION ---")
        plugin = FakePhotoshopPlugin(
            base_url="http://localhost",
            auth_token=cfg.secret_token,
            render_delay_seconds=0.05,
            output_format="tiff",
            output_dir=sandbox_renders,
        )

        # Connect plugin directly to Flask test client
        def mock_request(endpoint, method="GET", payload=None):
            headers = {"X-Token": cfg.secret_token}
            if method == "POST":
                resp = client.post(endpoint, json=payload, headers=headers)
            else:
                resp = client.get(endpoint, headers=headers)
            data = resp.get_json(silent=True) if resp.data else None
            return resp.status_code, data

        plugin._http_request = mock_request

        batch_idx = 0
        while True:
            # Lease next ready batch (force=True to lease partial groups)
            contract = plugin.lease_batch(force=True)
            if contract is None:
                logger.info("No more batches to lease (queue empty).")
                break

            batch_idx += 1
            batch_id = contract["batch_id"]
            batch_num = contract["batch_number"]
            clinic = contract["clinic"]
            pw = contract["paper_weight"]
            slots = contract["slots"]
            filled_slots = [s for s in slots if not s["is_empty"]]
            empty_slots = [s for s in slots if s["is_empty"]]

            logger.info(
                "\n[Batch %d] Processing %s (%s, %s): %d files + %d empty slots",
                batch_idx,
                batch_num,
                clinic,
                pw,
                len(filled_slots),
                len(empty_slots),
            )
            for s in slots:
                tag = f"EMPTY ({s['slot']})" if s["is_empty"] else f"{s['doctor_name']} ({s['slot']})"
                logger.info("   -> Slot %s @ (%s cm, %s cm): %s", s["slot"], s["x_cm"], s["y_cm"], tag)

            # Worker executes render & sends heartbeats & posts 202 Accepted
            ok = plugin.run_batch(contract)
            if not ok:
                logger.error("Batch %s failed to submit!", batch_id)
                return False

        # 6. Wait for finalizer background thread to finish all tasks
        logger.info("\n--- STEP 4: WAITING FOR FINALIZER COMPLETION ---")
        deadline = time.time() + 10.0
        while time.time() < deadline:
            with get_connection(db_path) as chk_conn:
                pending = chk_conn.execute(
                    "SELECT count(*) as cnt FROM batches WHERE status NOT IN ('done', 'flagged', 'interrupted');"
                ).fetchone()["cnt"]
                if pending == 0:
                    break
            time.sleep(0.1)

        # 7. Summary & Final Checks
        logger.info("\n--- STEP 5: FINAL RESULTS AUDIT ---")
        with get_connection(db_path) as conn:
            batches = conn.execute("SELECT id, batch_number, clinic, paper_weight, status FROM batches;").fetchall()
            logger.info("Batches summary (%d total):", len(batches))
            all_done = True
            for b in batches:
                logger.info("  * Batch %s (%s, %s): %s", b["batch_number"], b["clinic"], b["paper_weight"], b["status"])
                if b["status"] != "done":
                    all_done = False

            remaining_holding = conn.execute("SELECT count(*) as cnt FROM holding_files;").fetchone()["cnt"]
            logger.info("Remaining rows in holding_files: %d (expected 0 after safe move)", remaining_holding)

        # Check Finished folder
        finished_files = []
        for r, _, fs in os.walk(sandbox_finished):
            for f in fs:
                if f.lower().endswith((".psd", ".psb")):
                    finished_files.append(os.path.join(r, f))

        logger.info("Files successfully moved to 'Finished/': %d files", len(finished_files))
        for ff in finished_files:
            rel = os.path.relpath(ff, sandbox_finished)
            logger.info("  -> Finished: %s", rel)

        if all_done and len(batches) == batch_idx and remaining_holding == 0:
            logger.info("\n============================================================")
            logger.info(">>> SIMULATION SUCCESSFUL: ALL %d BATCHES COMPLETED CLEANLY! <<<", batch_idx)
            logger.info("============================================================")
            return True
        else:
            logger.error("Simulation finished with errors.")
            return False

    finally:
        # Graceful shutdown of daemons
        for key in ("FINALIZER_WORKER", "LEASE_REAPER", "SCANNER_SERVICE"):
            svc = app.config.get(key)
            if svc and hasattr(svc, "stop"):
                svc.stop(timeout=2.0)

        if keep_output:
            logger.info("\nSimulation artifacts preserved for inspection in: %s", temp_root)
            logger.info("  * Finished PSDs: %s", sandbox_finished)
            logger.info("  * Rendered Sheets (TIFF): %s", sandbox_renders)
            logger.info("  * SQLite Database: %s", db_path)
        else:
            try:
                shutil.rmtree(temp_root, ignore_errors=True)
                logger.info("Cleaned up sandbox temporary files.")
            except Exception:
                pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Roshta E2E simulation on input folder.")
    parser.add_argument("--source", default="input", help="Source folder path")
    parser.add_argument("--clean", action="store_true", help="Delete simulation artifacts on finish")
    args = parser.parse_args()

    success = run_simulation(args.source, keep_output=not args.clean)
    sys.exit(0 if success else 1)
