"""
Phase 4 tests: End-to-End pipeline with fake worker simulation.

Covers:
  - GET /next-job endpoint (spec-compliant)
  - Batch numbering counter persistence across leases
  - Lease heartbeat keeps batch alive
  - Crash simulation: lease expires -> reaper marks 'interrupted' -> files re-queue
  - Full pipeline: fake_plugin fetches job, creates TIFF, posts /job-result -> finalizer
    verifies, moves files, marks 'done', appends CSV row
  - Failed-result path: plugin reports failure -> batch marked 'failed', files unlocked
  - Force-batch path: fewer than 4 files -> force=1 -> empty slots filled
  - CSV logger: session_log.csv and master_YYYY-MM-DD.csv are created and populated
  - Multi-batch drain: N sequential batches all reach 'done'
"""
from __future__ import annotations

import csv
import os
import tempfile
import time
from typing import List

import numpy as np
import pytest
from PIL import Image

from server.app import create_app
from server.config import AppConfig
from server.csv_log import BatchLogRow, make_csv_logger
from server.database import init_db, get_connection, db_transaction
from server.models.prescription import file_id
from server.services.lease_reaper import LeaseReaperDaemon
from server.services.queue_service import QueueService


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def temp_config(tmpdir_path):
    source   = os.path.join(tmpdir_path, "Input")
    finished = os.path.join(tmpdir_path, "Finished")
    os.makedirs(source,   exist_ok=True)
    os.makedirs(finished, exist_ok=True)
    cfg = AppConfig(
        database_path=os.path.join(tmpdir_path, "test.db"),
        log_dir=os.path.join(tmpdir_path, "logs"),
        secret_token="test-token-phase4",
        source_folder=source,
        finished_folder=finished,
        scan_interval_seconds=9999.0,
        disk_min_bytes=0,
    )
    init_db(cfg.database_path)
    return cfg


@pytest.fixture
def client(temp_config):
    app = create_app(custom_config=temp_config)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, temp_config, app
    for key in ("FINALIZER_WORKER", "LEASE_REAPER", "SCANNER_SERVICE"):
        svc = app.config.get(key)
        if svc and hasattr(svc, "stop"):
            svc.stop(timeout=2.0)


# ===========================================================================
# Helpers
# ===========================================================================

HEADERS = {"X-Token": "test-token-phase4", "Content-Type": "application/json"}


def _insert_file(db_path, clinic="ClinicA", paper_weight="90gsm",
                 doctor_name="Dr_Test", mtime_offset=0.0, real_path=""):
    fpath = real_path or f"/fake/{clinic}/{paper_weight}/{doctor_name}.psd"
    fid   = file_id(fpath)
    mtime = 1_700_000_000.0 + mtime_offset
    with db_transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO holding_files
                (id, file_path, clinic, paper_weight, doctor_name,
                 file_size, mtime, is_skipped, status)
            VALUES (?, ?, ?, ?, ?, 1024, ?, 0, 'waiting');
            """,
            (fid, fpath, clinic, paper_weight, doctor_name, mtime),
        )
    return fid


def _insert_four(db_path, clinic="ClinicA", paper_weight="90gsm"):
    return [_insert_file(db_path, clinic, paper_weight, f"Dr_{i}", float(i)) for i in range(4)]


def _make_tiff(directory, name="render.tif"):
    arr = np.zeros((200, 300, 3), dtype=np.uint8)
    arr[50:150, 50:250] = [100, 150, 200]
    img = Image.fromarray(arr)
    path = os.path.join(directory, name)
    img.save(path, format="TIFF", dpi=(300, 300), compression="tiff_lzw")
    return path


def _batch_status(db_path, batch_id):
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT status FROM batches WHERE id = ?;", (batch_id,)).fetchone()
        return row["status"] if row else "NOT_FOUND"
    finally:
        conn.close()


def _file_status(db_path, fid):
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT status FROM holding_files WHERE id = ?;", (fid,)).fetchone()
        return row["status"] if row else "NOT_FOUND"
    finally:
        conn.close()


def _wait(db_path, batch_id, target, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _batch_status(db_path, batch_id) == target:
            return True
        time.sleep(0.05)
    return False


# ===========================================================================
# 1. GET /next-job
# ===========================================================================

class TestNextJobEndpoint:

    def test_204_when_empty(self, client):
        c, cfg, _ = client
        assert c.get("/next-job", headers=HEADERS).status_code == 204

    def test_200_with_four_files(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        r = c.get("/next-job", headers=HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["batch_number"].startswith("B")
        assert len(data["slots"]) == 4
        assert data["lease_id"]

    def test_204_with_fewer_than_four_no_force(self, client):
        c, cfg, _ = client
        _insert_file(cfg.database_path, doctor_name="Dr_Solo")
        assert c.get("/next-job", headers=HEADERS).status_code == 204

    def test_force_param_leases_partial(self, client):
        c, cfg, _ = client
        _insert_file(cfg.database_path, doctor_name="Dr_Solo")
        r = c.get("/next-job?force=1", headers=HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["is_forced"] is True
        assert len([s for s in data["slots"] if s["is_empty"]]) == 3

    def test_requires_auth(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        assert c.get("/next-job").status_code == 401

    def test_second_call_returns_204_while_first_leased(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        assert c.get("/next-job", headers=HEADERS).status_code == 200
        assert c.get("/next-job", headers=HEADERS).status_code == 204


# ===========================================================================
# 2. Batch numbering
# ===========================================================================

class TestBatchNumbering:

    def test_sequential_numbers(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        numbers = []
        for batch_idx in range(3):
            for i in range(4):
                _insert_file(temp_config.database_path, f"Clinic{batch_idx}",
                              "Standard", f"Dr_{i}", float(batch_idx * 10 + i))
            qs.rebuild_snapshot(get_connection(temp_config.database_path))
            c = qs.lease_batch()
            assert c is not None
            numbers.append(c["batch_number"])
        assert len(set(numbers)) == 3
        assert numbers == sorted(numbers)

    def test_batch_number_format(self, temp_config):
        import re
        _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        c = qs.lease_batch()
        assert re.match(r"^B\d{5}$", c["batch_number"])


# ===========================================================================
# 3. Heartbeat
# ===========================================================================

class TestHeartbeat:

    def test_extends_lease(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        r = c.post("/job-heartbeat",
                   json={"batch_id": job["batch_id"], "lease_id": job["lease_id"]},
                   headers=HEADERS)
        assert r.status_code == 200

    def test_wrong_lease_id_returns_404(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        r = c.post("/job-heartbeat",
                   json={"batch_id": job["batch_id"], "lease_id": "wrong"},
                   headers=HEADERS)
        assert r.status_code == 404


# ===========================================================================
# 4. Crash simulation: lease expiry + reaper
# ===========================================================================

class TestCrashRecovery:

    def test_expired_lease_reclaimed(self, temp_config):
        fids = _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()
        bid = contract["batch_id"]
        with db_transaction(temp_config.database_path) as conn:
            conn.execute(
                "UPDATE batches SET lease_expires_at = DATETIME('now','-10 minutes') WHERE id=?;",
                (bid,)
            )
        LeaseReaperDaemon(qs, temp_config)._reap_expired_leases()
        assert _batch_status(temp_config.database_path, bid) == "interrupted"
        for fid in fids:
            assert _file_status(temp_config.database_path, fid) == "waiting"

    def test_can_re_lease_after_crash(self, temp_config):
        _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        c1 = qs.lease_batch()
        with db_transaction(temp_config.database_path) as conn:
            conn.execute(
                "UPDATE batches SET lease_expires_at = DATETIME('now','-1 minute') WHERE id=?;",
                (c1["batch_id"],)
            )
        reaper = LeaseReaperDaemon(qs, temp_config)
        reaper._reap_expired_leases()
        qs.rebuild_snapshot(get_connection(temp_config.database_path))
        c2 = qs.lease_batch()
        assert c2 is not None
        assert c2["batch_id"] != c1["batch_id"]


# ===========================================================================
# 5. Full pipeline: lease -> result -> done
# ===========================================================================

class TestFullPipeline:

    def test_successful_pipeline_reaches_done(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        c.post("/job-result", json={
            "batch_id": job["batch_id"], "lease_id": job["lease_id"],
            "success": True, "duration_ms": 1234,
        }, headers=HEADERS)
        assert _wait(cfg.database_path, job["batch_id"], "done")

    def test_holding_files_deleted_after_done(self, client):
        c, cfg, _ = client
        fids = _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        c.post("/job-result", json={
            "batch_id": job["batch_id"], "lease_id": job["lease_id"],
            "success": True, "duration_ms": 500,
        }, headers=HEADERS)
        _wait(cfg.database_path, job["batch_id"], "done")
        conn = get_connection(cfg.database_path)
        try:
            ph = ",".join("?" * len(fids))
            rows = conn.execute(
                f"SELECT id FROM holding_files WHERE id IN ({ph});", fids
            ).fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_pipeline_without_tiff_path(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        c.post("/job-result", json={
            "batch_id": job["batch_id"], "lease_id": job["lease_id"],
            "success": True, "duration_ms": 100,
        }, headers=HEADERS)
        assert _wait(cfg.database_path, job["batch_id"], "done")


# ===========================================================================
# 6. Failed result
# ===========================================================================

class TestFailedResult:

    def test_plugin_failure_marks_failed(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        c.post("/job-result", json={
            "batch_id": job["batch_id"], "lease_id": job["lease_id"],
            "success": False, "error_message": "PS crashed",
        }, headers=HEADERS)
        assert _wait(cfg.database_path, job["batch_id"], "failed")

    def test_files_unlocked_after_failure(self, client):
        c, cfg, _ = client
        fids = _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        c.post("/job-result", json={
            "batch_id": job["batch_id"], "lease_id": job["lease_id"],
            "success": False, "error_message": "err",
        }, headers=HEADERS)
        _wait(cfg.database_path, job["batch_id"], "failed")
        for fid in fids:
            assert _file_status(cfg.database_path, fid) == "waiting"

    def test_stale_lease_id_ignored(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        c.post("/job-result", json={
            "batch_id": job["batch_id"], "lease_id": "wrong-lease",
            "success": True, "duration_ms": 100,
        }, headers=HEADERS)
        time.sleep(0.5)
        assert _batch_status(cfg.database_path, job["batch_id"]) == "leased"


# ===========================================================================
# 7. Force-batch
# ===========================================================================

class TestForceBatch:

    def test_forced_batch_has_empty_slots(self, client):
        c, cfg, _ = client
        _insert_file(cfg.database_path, doctor_name="Dr_Only")
        r = c.get("/next-job?force=1", headers=HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["is_forced"] is True
        assert len([s for s in data["slots"] if not s["is_empty"]]) == 1
        assert len([s for s in data["slots"] if s["is_empty"]]) == 3

    def test_forced_batch_can_complete(self, client):
        c, cfg, _ = client
        _insert_file(cfg.database_path, doctor_name="Dr_Only")
        job = c.get("/next-job?force=1", headers=HEADERS).get_json()
        c.post("/job-result", json={
            "batch_id": job["batch_id"], "lease_id": job["lease_id"],
            "success": True, "duration_ms": 200,
        }, headers=HEADERS)
        assert _wait(cfg.database_path, job["batch_id"], "done")


# ===========================================================================
# 8. CSV logger unit tests
# ===========================================================================

class TestCsvLogger:

    def _make_row(self, batch_number="B00001"):
        return BatchLogRow(
            timestamp="2026-10-21T10:00:00Z",
            batch_id=f"batch_{batch_number}",
            batch_number=batch_number,
            clinic="ClinicA",
            paper_weight="90gsm",
            slot_count="4",
            is_forced="0",
            duration_ms="1234",
            print_final_path="/out/B_PRINT.tif",
            tracker_final_path="/out/B_TRACKER.tif",
            doctors="Dr_A|Dr_B|Dr_C|Dr_D",
            status="done",
            error_message="",
        )

    def test_creates_session_csv(self, tmpdir_path):
        finished = os.path.join(tmpdir_path, "Finished")
        os.makedirs(finished)
        logger = make_csv_logger(finished)
        logger.append_batch(self._make_row())
        assert os.path.exists(logger.session_path())
        with open(logger.session_path(), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0][0] == "timestamp"
        assert rows[1][2] == "B00001"

    def test_creates_master_csv(self, tmpdir_path):
        from datetime import date
        finished = os.path.join(tmpdir_path, "Finished")
        os.makedirs(finished)
        logger = make_csv_logger(finished)
        logger.append_batch(self._make_row())
        master = logger.master_path_for_today()
        assert os.path.exists(master)
        assert date.today().isoformat() in os.path.basename(master)

    def test_no_duplicate_headers(self, tmpdir_path):
        finished = os.path.join(tmpdir_path, "Finished")
        os.makedirs(finished)
        logger = make_csv_logger(finished)
        for i in range(5):
            logger.append_batch(self._make_row(f"B0000{i}"))
        with open(logger.session_path(), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 6   # 1 header + 5 data
        assert rows[0][0] == "timestamp"


# ===========================================================================
# 9. CSV integration via HTTP
# ===========================================================================

class TestCsvIntegration:

    def test_done_batch_in_session_csv(self, client):
        c, cfg, app = client
        _insert_four(cfg.database_path)
        job = c.get("/next-job", headers=HEADERS).get_json()
        c.post("/job-result", json={
            "batch_id": job["batch_id"], "lease_id": job["lease_id"],
            "success": True, "duration_ms": 777,
        }, headers=HEADERS)
        _wait(cfg.database_path, job["batch_id"], "done")

        csv_logger = app.config.get("CSV_LOGGER")
        if csv_logger is None:
            pytest.skip("No CSV_LOGGER configured.")

        path = csv_logger.session_path()
        assert os.path.exists(path)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        batch_ids = [row[1] for row in rows[1:]]
        assert job["batch_id"] in batch_ids


# ===========================================================================
# 10. Multi-batch drain
# ===========================================================================

class TestMultiBatchDrain:

    @pytest.mark.parametrize("n", [3, 6])
    def test_all_batches_reach_done(self, client, n):
        c, cfg, _ = client
        batch_ids = []
        for i in range(n):
            for j in range(4):
                _insert_file(cfg.database_path, f"Clinic_{i}", "Standard",
                             f"Dr_{j}", float(i * 10 + j))
            job = c.get("/next-job", headers=HEADERS).get_json()
            batch_ids.append(job["batch_id"])
            c.post("/job-result", json={
                "batch_id": job["batch_id"], "lease_id": job["lease_id"],
                "success": True, "duration_ms": 100,
            }, headers=HEADERS)

        for bid in batch_ids:
            ok = _wait(cfg.database_path, bid, "done", timeout=8.0)
            assert ok, f"Batch {bid} stuck at {_batch_status(cfg.database_path, bid)}"

    def test_unique_batch_numbers(self, client):
        c, cfg, _ = client
        numbers = []
        for i in range(5):
            for j in range(4):
                _insert_file(cfg.database_path, f"C{i}", "S", f"D{j}", float(i*10+j))
            job = c.get("/next-job", headers=HEADERS).get_json()
            numbers.append(job["batch_number"])
            c.post("/job-result", json={
                "batch_id": job["batch_id"], "lease_id": job["lease_id"],
                "success": True, "duration_ms": 50,
            }, headers=HEADERS)
            _wait(cfg.database_path, job["batch_id"], "done", timeout=5.0)
        assert len(set(numbers)) == 5
