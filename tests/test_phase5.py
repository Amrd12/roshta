"""
Phase 5 tests: Queue Control & Recovery Protocols.

Covers:
  POST /control:
    - play / pause / stop mode transitions and illegal transitions
    - force_batch command
    - retry command (failed / interrupted / flagged batches)
    - mark_reviewed command
    - rescan command
    - unknown command returns 400
    - missing command field returns 422
    - authentication required

  QueueService.set_mode():
    - all valid transitions
    - illegal transitions raise ValueError

  QueueService.retry_batch():
    - re-queues failed / interrupted / flagged batches
    - rejects non-retryable (leased, done) batches
    - files return to 'waiting'

  QueueService.mark_reviewed():
    - clears flagged batch to 'done'
    - rejects non-flagged batches

  QueueService.get_resume_summary():
    - 'none' when clean
    - 'auto_restart' for interrupted/leased
    - 'flag_for_review' when flagged batches exist
    - counts are accurate

  GET /health:
    - includes mode, disk_free_gb, disk_warning
    - resume_summary.recommendation correct

  Integration:
    - play -> work -> pause -> resumes after play
    - stop prevents new leases
    - retry after failure -> can be re-leased
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid

import pytest

from server.app import create_app
from server.config import AppConfig
from server.database import init_db, get_connection, db_transaction
from server.models.prescription import file_id
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
        secret_token="test-p5",
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


HEADERS = {"X-Token": "test-p5", "Content-Type": "application/json"}


# ===========================================================================
# Helpers
# ===========================================================================

def _insert_file(db_path, clinic="ClinicA", paper_weight="90gsm",
                 doctor_name="Dr_Test", mtime_offset=0.0):
    fpath = f"/fake/{clinic}/{paper_weight}/{doctor_name}.psd"
    fid   = file_id(fpath)
    mtime = 1_700_000_000.0 + mtime_offset
    with db_transaction(db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO holding_files
               (id, file_path, clinic, paper_weight, doctor_name,
                file_size, mtime, is_skipped, status)
               VALUES (?, ?, ?, ?, ?, 1024, ?, 0, 'waiting');""",
            (fid, fpath, clinic, paper_weight, doctor_name, mtime),
        )
    return fid


def _insert_four(db_path, clinic="ClinicA", paper_weight="90gsm"):
    return [_insert_file(db_path, clinic, paper_weight, f"Dr_{i}", float(i)) for i in range(4)]


def _batch_status(db_path, batch_id):
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT status FROM batches WHERE id=?;", (batch_id,)).fetchone()
        return row["status"] if row else "NOT_FOUND"
    finally:
        conn.close()


def _seed_batch(db_path, status, clinic="ClinicA"):
    """Insert a batch directly in a given status for control tests."""
    bid = f"batch_{uuid.uuid4().hex[:8]}"
    bnum = f"B{abs(hash(bid)) % 100000:05d}"
    with db_transaction(db_path) as conn:
        conn.execute(
            """INSERT INTO batches
               (id, batch_number, lease_id, clinic, paper_weight, status, attempt_count)
               VALUES (?, ?, ?, ?, 'Standard', ?, 1);""",
            (bid, bnum, str(uuid.uuid4()), clinic, status),
        )
    return bid


def _wait(db_path, batch_id, target, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _batch_status(db_path, batch_id) == target:
            return True
        time.sleep(0.05)
    return False


# ===========================================================================
# 1. QueueService.set_mode() — mode transitions
# ===========================================================================

class TestSetMode:

    def test_play_from_idle(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.mode == "idle"
        result = qs.set_mode("play")
        assert result == "running"
        assert qs.mode == "running"

    def test_pause_from_running(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        qs.set_mode("play")
        result = qs.set_mode("pause")
        assert result == "pausing"

    def test_stop_from_running(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        qs.set_mode("play")
        result = qs.set_mode("stop")
        assert result == "stopped"

    def test_stop_from_paused(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        qs.set_mode("play")
        qs.set_mode("pause")
        qs.set_mode("paused")   # simulate finalizer completing last batch
        result = qs.set_mode("stop")
        assert result == "stopped"

    def test_play_from_stopped(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        qs.set_mode("play")
        qs.set_mode("stop")
        result = qs.set_mode("play")
        assert result == "running"

    def test_illegal_pause_from_idle(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        with pytest.raises(ValueError, match="Cannot 'pause'"):
            qs.set_mode("pause")

    def test_illegal_play_from_running(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        qs.set_mode("play")
        with pytest.raises(ValueError, match="Cannot 'play'"):
            qs.set_mode("play")

    def test_unknown_command_raises(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        with pytest.raises(ValueError, match="Unknown mode command"):
            qs.set_mode("turbo")

    def test_mode_reflected_in_snapshot(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        qs.set_mode("play")
        snap = qs.get_snapshot()
        assert snap.mode == "running"


# ===========================================================================
# 2. QueueService.retry_batch()
# ===========================================================================

class TestRetryBatch:

    def test_retry_failed_batch(self, temp_config):
        bid = _seed_batch(temp_config.database_path, "failed")
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.retry_batch(bid) is True
        assert _batch_status(temp_config.database_path, bid) == "interrupted"

    def test_retry_interrupted_batch(self, temp_config):
        bid = _seed_batch(temp_config.database_path, "interrupted")
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.retry_batch(bid) is True

    def test_retry_flagged_batch(self, temp_config):
        bid = _seed_batch(temp_config.database_path, "flagged")
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.retry_batch(bid) is True

    def test_retry_done_batch_returns_false(self, temp_config):
        bid = _seed_batch(temp_config.database_path, "done")
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.retry_batch(bid) is False
        assert _batch_status(temp_config.database_path, bid) == "done"

    def test_retry_nonexistent_batch_returns_false(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.retry_batch("batch_nonexistent") is False

    def test_retry_unlocks_holding_files(self, temp_config):
        """Files associated with a failed batch must return to 'waiting'."""
        fids = _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        qs.set_mode("play")
        contract = qs.lease_batch()
        bid = contract["batch_id"]

        # Manually fail the batch so files are still locked
        with db_transaction(temp_config.database_path) as conn:
            conn.execute("UPDATE batches SET status='failed' WHERE id=?;", (bid,))

        assert qs.retry_batch(bid) is True

        conn = get_connection(temp_config.database_path)
        try:
            rows = conn.execute(
                "SELECT status FROM holding_files WHERE batch_id=?;", (bid,)
            ).fetchall()
        finally:
            conn.close()
        # Files should be back to waiting (batch_id cleared)
        for row in rows:
            assert row["status"] == "waiting"


# ===========================================================================
# 3. QueueService.mark_reviewed()
# ===========================================================================

class TestMarkReviewed:

    def test_clears_flagged_to_done(self, temp_config):
        bid = _seed_batch(temp_config.database_path, "flagged")
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.mark_reviewed(bid) is True
        assert _batch_status(temp_config.database_path, bid) == "done"

    def test_rejects_non_flagged_batch(self, temp_config):
        bid = _seed_batch(temp_config.database_path, "failed")
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.mark_reviewed(bid) is False
        assert _batch_status(temp_config.database_path, bid) == "failed"

    def test_rejects_done_batch(self, temp_config):
        bid = _seed_batch(temp_config.database_path, "done")
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.mark_reviewed(bid) is False

    def test_rejects_nonexistent_batch(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.mark_reviewed("no-such-batch") is False


# ===========================================================================
# 4. QueueService.get_resume_summary()
# ===========================================================================

class TestResumeSummary:

    def test_empty_db_returns_none_recommendation(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        summary = qs.get_resume_summary()
        assert summary["recommendation"] == "none"
        assert summary["interrupted_batches"] == 0
        assert summary["flagged_batches"] == 0

    def test_interrupted_triggers_auto_restart(self, temp_config):
        _seed_batch(temp_config.database_path, "interrupted")
        qs = QueueService(temp_config)
        qs.initialise()
        summary = qs.get_resume_summary()
        assert summary["recommendation"] == "auto_restart"
        assert summary["interrupted_batches"] == 1

    def test_leased_at_shutdown_triggers_auto_restart(self, temp_config):
        _seed_batch(temp_config.database_path, "leased")
        qs = QueueService(temp_config)
        qs.initialise()
        summary = qs.get_resume_summary()
        assert summary["recommendation"] == "auto_restart"
        assert summary["leased_batches"] == 1

    def test_flagged_triggers_flag_for_review(self, temp_config):
        _seed_batch(temp_config.database_path, "flagged")
        qs = QueueService(temp_config)
        qs.initialise()
        summary = qs.get_resume_summary()
        assert summary["recommendation"] == "flag_for_review"
        assert summary["flagged_batches"] == 1

    def test_flagged_takes_priority_over_interrupted(self, temp_config):
        _seed_batch(temp_config.database_path, "flagged")
        _seed_batch(temp_config.database_path, "interrupted")
        qs = QueueService(temp_config)
        qs.initialise()
        summary = qs.get_resume_summary()
        assert summary["recommendation"] == "flag_for_review"

    def test_counts_are_accurate(self, temp_config):
        for _ in range(3):
            _seed_batch(temp_config.database_path, "interrupted")
        for _ in range(2):
            _seed_batch(temp_config.database_path, "flagged")
        qs = QueueService(temp_config)
        qs.initialise()
        summary = qs.get_resume_summary()
        assert summary["interrupted_batches"] == 3
        assert summary["flagged_batches"] == 2


# ===========================================================================
# 5. POST /control — play / pause / stop
# ===========================================================================

class TestControlPlayPauseStop:

    def test_play_transitions_to_running(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={"command": "play"}, headers=HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["mode"] == "running"

    def test_pause_from_running(self, client):
        c, cfg, _ = client
        c.post("/control", json={"command": "play"}, headers=HEADERS)
        r = c.post("/control", json={"command": "pause"}, headers=HEADERS)
        assert r.status_code == 200
        assert r.get_json()["mode"] == "pausing"

    def test_stop_from_running(self, client):
        c, cfg, _ = client
        c.post("/control", json={"command": "play"}, headers=HEADERS)
        r = c.post("/control", json={"command": "stop"}, headers=HEADERS)
        assert r.status_code == 200
        assert r.get_json()["mode"] == "stopped"

    def test_illegal_pause_from_idle_returns_400(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={"command": "pause"}, headers=HEADERS)
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "INVALID_TRANSITION"

    def test_illegal_play_from_running_returns_400(self, client):
        c, cfg, _ = client
        c.post("/control", json={"command": "play"}, headers=HEADERS)
        r = c.post("/control", json={"command": "play"}, headers=HEADERS)
        assert r.status_code == 400

    def test_stop_prevents_new_leases(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        c.post("/control", json={"command": "play"}, headers=HEADERS)
        c.post("/control", json={"command": "stop"}, headers=HEADERS)
        r = c.get("/next-job", headers=HEADERS)
        assert r.status_code == 409   # SERVER_NOT_RUNNING

    def test_play_after_stop_allows_leases(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)
        c.post("/control", json={"command": "play"}, headers=HEADERS)
        c.post("/control", json={"command": "stop"}, headers=HEADERS)
        c.post("/control", json={"command": "play"}, headers=HEADERS)
        r = c.get("/next-job", headers=HEADERS)
        assert r.status_code == 200


# ===========================================================================
# 6. POST /control — force_batch
# ===========================================================================

class TestControlForceBatch:

    def test_force_batch_leases_partial_queue(self, client):
        c, cfg, _ = client
        _insert_file(cfg.database_path, doctor_name="Dr_Solo")
        r = c.post("/control", json={"command": "force_batch"}, headers=HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "contract" in data
        assert data["contract"]["is_forced"] is True

    def test_force_batch_empty_queue_returns_404(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={"command": "force_batch"}, headers=HEADERS)
        assert r.status_code == 404
        assert r.get_json()["error"]["code"] == "QUEUE_EMPTY"

    def test_force_batch_with_clinic_filter(self, client):
        c, cfg, _ = client
        _insert_file(cfg.database_path, clinic="ClinicX", doctor_name="Dr_X")
        _insert_file(cfg.database_path, clinic="ClinicY", doctor_name="Dr_Y",
                     mtime_offset=1.0)
        r = c.post("/control",
                   json={"command": "force_batch", "clinic": "ClinicX"},
                   headers=HEADERS)
        assert r.status_code == 200
        assert r.get_json()["contract"]["clinic"] == "ClinicX"


# ===========================================================================
# 7. POST /control — retry
# ===========================================================================

class TestControlRetry:

    def test_retry_failed_batch_via_http(self, client):
        c, cfg, _ = client
        bid = _seed_batch(cfg.database_path, "failed")
        r = c.post("/control", json={"command": "retry", "batch_id": bid}, headers=HEADERS)
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_retry_missing_batch_id_returns_422(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={"command": "retry"}, headers=HEADERS)
        assert r.status_code == 422

    def test_retry_nonexistent_batch_returns_404(self, client):
        c, cfg, _ = client
        r = c.post("/control",
                   json={"command": "retry", "batch_id": "no-such-batch"},
                   headers=HEADERS)
        assert r.status_code == 404

    def test_retry_done_batch_returns_404(self, client):
        c, cfg, _ = client
        bid = _seed_batch(cfg.database_path, "done")
        r = c.post("/control", json={"command": "retry", "batch_id": bid}, headers=HEADERS)
        assert r.status_code == 404


# ===========================================================================
# 8. POST /control — mark_reviewed
# ===========================================================================

class TestControlMarkReviewed:

    def test_mark_reviewed_flagged_batch(self, client):
        c, cfg, _ = client
        bid = _seed_batch(cfg.database_path, "flagged")
        r = c.post("/control",
                   json={"command": "mark_reviewed", "batch_id": bid},
                   headers=HEADERS)
        assert r.status_code == 200
        assert _batch_status(cfg.database_path, bid) == "done"

    def test_mark_reviewed_missing_batch_id_returns_422(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={"command": "mark_reviewed"}, headers=HEADERS)
        assert r.status_code == 422

    def test_mark_reviewed_non_flagged_returns_404(self, client):
        c, cfg, _ = client
        bid = _seed_batch(cfg.database_path, "failed")
        r = c.post("/control",
                   json={"command": "mark_reviewed", "batch_id": bid},
                   headers=HEADERS)
        assert r.status_code == 404


# ===========================================================================
# 9. POST /control — rescan + unknown
# ===========================================================================

class TestControlMiscCommands:

    def test_rescan_returns_200(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={"command": "rescan"}, headers=HEADERS)
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_unknown_command_returns_400(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={"command": "turbo_boost"}, headers=HEADERS)
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "UNKNOWN_COMMAND"

    def test_missing_command_field_returns_422(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={}, headers=HEADERS)
        assert r.status_code == 422

    def test_no_body_returns_422(self, client):
        c, cfg, _ = client
        r = c.post("/control", headers={"X-Token": "test-p5"})
        assert r.status_code == 422

    def test_requires_auth(self, client):
        c, cfg, _ = client
        r = c.post("/control", json={"command": "play"})
        assert r.status_code == 401


# ===========================================================================
# 10. GET /health — upgraded response
# ===========================================================================

class TestHealthUpgrade:

    def test_health_includes_mode(self, client):
        c, cfg, _ = client
        r = c.get("/health")
        assert r.status_code == 200
        data = r.get_json()
        assert "mode" in data
        assert data["mode"] == "idle"

    def test_health_mode_updates_after_play(self, client):
        c, cfg, _ = client
        c.post("/control", json={"command": "play"}, headers=HEADERS)
        r = c.get("/health")
        assert r.get_json()["mode"] == "running"

    def test_health_includes_resume_summary(self, client):
        c, cfg, _ = client
        r = c.get("/health")
        summary = r.get_json()["resume_summary"]
        assert "interrupted_batches" in summary
        assert "flagged_batches" in summary
        assert "recommendation" in summary

    def test_health_resume_recommendation_auto_restart(self, client):
        c, cfg, _ = client
        _seed_batch(cfg.database_path, "interrupted")
        r = c.get("/health")
        assert r.get_json()["resume_summary"]["recommendation"] == "auto_restart"

    def test_health_resume_recommendation_flag_for_review(self, client):
        c, cfg, _ = client
        _seed_batch(cfg.database_path, "flagged")
        r = c.get("/health")
        assert r.get_json()["resume_summary"]["recommendation"] == "flag_for_review"

    def test_health_includes_disk_fields(self, client):
        c, cfg, _ = client
        r = c.get("/health")
        data = r.get_json()
        assert "disk_free_gb" in data
        assert "disk_warning" in data

    def test_health_no_auth_required(self, client):
        c, cfg, _ = client
        r = c.get("/health")   # no X-Token
        assert r.status_code == 200


# ===========================================================================
# 11. Integration: play → work → pause → play cycle
# ===========================================================================

class TestPlayPauseCycle:

    def test_pause_stops_new_leases_after_batch_in_flight(self, client):
        c, cfg, _ = client
        # Insert 8 files = 2 batches
        for i in range(8):
            _insert_file(cfg.database_path, doctor_name=f"Dr_{i}", mtime_offset=float(i))

        # Play and lease first batch
        c.post("/control", json={"command": "play"}, headers=HEADERS)
        r1 = c.get("/next-job", headers=HEADERS)
        assert r1.status_code == 200

        # Pause — should set 'pausing'
        rp = c.post("/control", json={"command": "pause"}, headers=HEADERS)
        assert rp.get_json()["mode"] == "pausing"

        # While pausing, no new leases should be issued
        r2 = c.get("/next-job", headers=HEADERS)
        assert r2.status_code == 409   # SERVER_NOT_RUNNING (pausing blocks leases)

    def test_retry_after_failure_allows_re_lease(self, client):
        c, cfg, _ = client
        _insert_four(cfg.database_path)

        c.post("/control", json={"command": "play"}, headers=HEADERS)
        job = c.get("/next-job", headers=HEADERS).get_json()
        bid = job["batch_id"]

        # Report failure
        c.post("/job-result", json={
            "batch_id": bid, "lease_id": job["lease_id"],
            "success": False, "error_message": "PS error",
        }, headers=HEADERS)
        _wait(cfg.database_path, bid, "failed", timeout=5.0)

        # Retry via /control
        r = c.post("/control", json={"command": "retry", "batch_id": bid}, headers=HEADERS)
        assert r.status_code == 200

        # Now the files should be releasable (same files, new lease)
        r2 = c.get("/next-job", headers=HEADERS)
        assert r2.status_code == 200
        assert r2.get_json()["batch_id"] != bid   # new batch record
