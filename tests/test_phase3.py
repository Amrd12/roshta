"""
Phase 3 tests: Atomic Lease, Heartbeat, Reaper, Finalizer, HTTP endpoints.

All tests use an isolated temp directory with a real SQLite database.
No network calls, no real Photoshop processes.
"""
from __future__ import annotations

import os
import tempfile
import time
import threading
import pytest

from server.config import AppConfig
from server.database import init_db, get_connection, db_transaction
from server.app import create_app
from server.models.prescription import file_id
from server.services.queue_service import QueueService
from server.services.lease_reaper import LeaseReaperDaemon
from server.services.finalizer_worker import JobFinalizerWorker, FinalizationTask


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
        secret_token="test-token",
        source_folder=source,
        finished_folder=finished,
        # Large interval so the scanner never fires during a test;
        # fake file paths would otherwise be pruned on the first scan pass.
        scan_interval_seconds=9999.0,
    )
    init_db(cfg.database_path)
    return cfg


@pytest.fixture
def client(temp_config):
    app = create_app(custom_config=temp_config)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, temp_config
    # Stop daemon threads so they release the SQLite WAL file before Windows
    # tries to delete the temp directory (WinError 32 guard).
    for key in ("FINALIZER_WORKER", "LEASE_REAPER", "SCANNER_SERVICE"):
        svc = app.config.get(key)
        if svc and hasattr(svc, "stop"):
            svc.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Helper: insert a fake holding_files row directly into SQLite
# ---------------------------------------------------------------------------

def _insert_file(
    db_path: str,
    clinic: str,
    paper_weight: str,
    doctor_name: str,
    mtime_offset: float = 0.0,
) -> str:
    """Insert a fake waiting PSD into holding_files; return its id."""
    fpath = f"/fake/{clinic}/{paper_weight}/{doctor_name}.psd"
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


def _insert_four(db_path, clinic="ClinicA", paper_weight="90gsm") -> list[str]:
    """Insert exactly 4 waiting files; returns list of ids."""
    ids = []
    for i in range(4):
        fid = _insert_file(db_path, clinic, paper_weight, f"Dr_{i}", mtime_offset=float(i))
        ids.append(fid)
    return ids


# ===========================================================================
# 1. QueueService.lease_batch()
# ===========================================================================

class TestLeaseBatch:

    def test_four_files_produce_contract(self, temp_config):
        _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()
        assert contract is not None
        assert contract["batch_number"].startswith("B")
        assert len(contract["slots"]) == 4
        assert contract["lease_id"]

    def test_returns_none_when_fewer_than_four_and_no_force(self, temp_config):
        _insert_file(temp_config.database_path, "ClinicA", "90gsm", "Dr_Solo")
        qs = QueueService(temp_config)
        qs.initialise()
        assert qs.lease_batch() is None

    def test_force_flag_leases_with_fewer_than_four(self, temp_config):
        _insert_file(temp_config.database_path, "ClinicA", "90gsm", "Dr_Solo")
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch(force=True)
        assert contract is not None
        assert contract["is_forced"] is True

    def test_holding_files_locked_after_lease(self, temp_config):
        ids = _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()
        assert contract is not None

        conn = get_connection(temp_config.database_path)
        rows = conn.execute(
            "SELECT status FROM holding_files WHERE id IN (?,?,?,?);", ids
        ).fetchall()
        conn.close()

        assert all(r["status"] == "locked" for r in rows)

    def test_batch_number_increments(self, temp_config):
        _insert_four(temp_config.database_path)
        _insert_four(temp_config.database_path, clinic="ClinicB")
        qs = QueueService(temp_config)
        qs.initialise()

        c1 = qs.lease_batch(clinic="ClinicA")
        # Mark first batch done so second can be leased
        with db_transaction(temp_config.database_path) as conn:
            conn.execute(
                "UPDATE batches SET status='done' WHERE id=?;", (c1["batch_id"],)
            )
            conn.execute(
                "DELETE FROM holding_files WHERE batch_id=?;", (c1["batch_id"],)
            )

        c2 = qs.lease_batch(clinic="ClinicB")
        assert c2 is not None
        n1 = int(c1["batch_number"][1:])
        n2 = int(c2["batch_number"][1:])
        assert n2 == n1 + 1

    def test_snapshot_shows_locked_files(self, temp_config):
        _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        qs.lease_batch()
        snap = qs.get_snapshot()
        assert snap.counters["locked"] == 4

    def test_double_lease_prevention(self, temp_config):
        """A second lease call for the same group returns None (files are locked)."""
        _insert_four(temp_config.database_path, clinic="ClinicA")
        qs = QueueService(temp_config)
        qs.initialise()
        c1 = qs.lease_batch()
        assert c1 is not None
        # Only 4 files, all locked now — no second batch possible
        c2 = qs.lease_batch()
        assert c2 is None


# ===========================================================================
# 2. QueueService.extend_lease()
# ===========================================================================

class TestExtendLease:

    def test_extend_valid_lease(self, temp_config):
        _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()

        ok = qs.extend_lease(
            batch_id=contract["batch_id"],
            lease_id=contract["lease_id"],
        )
        assert ok is True

    def test_extend_wrong_lease_id_fails(self, temp_config):
        _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()

        ok = qs.extend_lease(
            batch_id=contract["batch_id"],
            lease_id="wrong-lease-id",
        )
        assert ok is False

    def test_extend_nonexistent_batch_fails(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        ok = qs.extend_lease(batch_id="ghost_batch", lease_id="anything")
        assert ok is False


# ===========================================================================
# 3. LeaseReaperDaemon
# ===========================================================================

class TestLeaseReaperDaemon:

    def test_reaper_marks_expired_lease_interrupted(self, temp_config):
        """Force a lease to expire in the past and check the reaper reclaims it."""
        _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()
        batch_id = contract["batch_id"]

        # Backdate the lease to be already expired
        with db_transaction(temp_config.database_path) as conn:
            conn.execute(
                "UPDATE batches SET lease_expires_at = DATETIME('now', '-1 minute') WHERE id=?;",
                (batch_id,),
            )

        reaper = LeaseReaperDaemon(qs, temp_config)
        # Call _reap_expired_leases() directly (no thread needed in tests)
        reclaimed = reaper._reap_expired_leases()
        assert reclaimed == 1

        conn = get_connection(temp_config.database_path)
        row = conn.execute(
            "SELECT status FROM batches WHERE id=?;", (batch_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "interrupted"

    def test_reaper_unlocks_holding_files(self, temp_config):
        ids = _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()
        batch_id = contract["batch_id"]

        with db_transaction(temp_config.database_path) as conn:
            conn.execute(
                "UPDATE batches SET lease_expires_at = DATETIME('now', '-1 minute') WHERE id=?;",
                (batch_id,),
            )

        reaper = LeaseReaperDaemon(qs, temp_config)
        reaper._reap_expired_leases()

        conn = get_connection(temp_config.database_path)
        rows = conn.execute(
            "SELECT status, batch_id FROM holding_files WHERE id IN (?,?,?,?);", ids
        ).fetchall()
        conn.close()

        assert all(r["status"] == "waiting" for r in rows)
        assert all(r["batch_id"] is None for r in rows)

    def test_non_expired_lease_not_reaped(self, temp_config):
        _insert_four(temp_config.database_path)
        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()
        batch_id = contract["batch_id"]

        reaper = LeaseReaperDaemon(qs, temp_config)
        reclaimed = reaper._reap_expired_leases()
        assert reclaimed == 0

        conn = get_connection(temp_config.database_path)
        row = conn.execute(
            "SELECT status FROM batches WHERE id=?;", (batch_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "leased"


# ===========================================================================
# 4. JobFinalizerWorker
# ===========================================================================

class TestJobFinalizerWorker:

    def _make_psd(self, folder: str, name: str) -> str:
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(b"FAKE_PSD")
        return path

    def test_successful_job_marks_batch_done(self, temp_config):
        # Plant real source files so safe_move works
        source_files = []
        for i in range(4):
            src = self._make_psd(temp_config.source_folder, f"Dr_{i}.psd")
            source_files.append(src)
            fid = file_id(src)
            with db_transaction(temp_config.database_path) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO holding_files
                        (id, file_path, clinic, paper_weight, doctor_name,
                         file_size, mtime, is_skipped, status)
                    VALUES (?, ?, 'ClinicA', '90gsm', ?, 1024, ?, 0, 'waiting');
                    """,
                    (fid, src, f"Dr_{i}", float(i)),
                )

        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()
        assert contract is not None

        worker = JobFinalizerWorker(qs, temp_config)
        task = FinalizationTask(
            batch_id=contract["batch_id"],
            lease_id=contract["lease_id"],
            success=True,
            duration_ms=5000,
        )
        # Process synchronously (no thread needed for deterministic testing)
        worker._process(task)

        conn = get_connection(temp_config.database_path)
        row = conn.execute(
            "SELECT status FROM batches WHERE id=?;", (contract["batch_id"],)
        ).fetchone()
        conn.close()
        assert row["status"] == "done"

    def test_failed_job_marks_batch_failed(self, temp_config):
        for i in range(4):
            _insert_file(temp_config.database_path, "ClinicA", "90gsm", f"Dr_{i}", float(i))

        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()

        worker = JobFinalizerWorker(qs, temp_config)
        task = FinalizationTask(
            batch_id=contract["batch_id"],
            lease_id=contract["lease_id"],
            success=False,
            error_message="Photoshop crashed",
        )
        worker._process(task)

        conn = get_connection(temp_config.database_path)
        row = conn.execute(
            "SELECT status, error_message FROM batches WHERE id=?;",
            (contract["batch_id"],)
        ).fetchone()
        conn.close()
        assert row["status"] == "failed"
        assert "Photoshop crashed" in row["error_message"]

    def test_failed_job_unlocks_holding_files(self, temp_config):
        ids = []
        for i in range(4):
            fid = _insert_file(temp_config.database_path, "ClinicA", "90gsm", f"Dr_{i}", float(i))
            ids.append(fid)

        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()

        worker = JobFinalizerWorker(qs, temp_config)
        task = FinalizationTask(
            batch_id=contract["batch_id"],
            lease_id=contract["lease_id"],
            success=False,
            error_message="crash",
        )
        worker._process(task)

        conn = get_connection(temp_config.database_path)
        rows = conn.execute(
            "SELECT status FROM holding_files WHERE id IN (?,?,?,?);", ids
        ).fetchall()
        conn.close()
        assert all(r["status"] == "waiting" for r in rows)

    def test_stale_lease_id_discarded(self, temp_config):
        for i in range(4):
            _insert_file(temp_config.database_path, "ClinicA", "90gsm", f"Dr_{i}", float(i))

        qs = QueueService(temp_config)
        qs.initialise()
        contract = qs.lease_batch()

        worker = JobFinalizerWorker(qs, temp_config)
        task = FinalizationTask(
            batch_id=contract["batch_id"],
            lease_id="stale-lease-id",
            success=True,
        )
        worker._process(task)

        conn = get_connection(temp_config.database_path)
        row = conn.execute(
            "SELECT status FROM batches WHERE id=?;", (contract["batch_id"],)
        ).fetchone()
        conn.close()
        # Status must remain 'leased' — task was discarded
        assert row["status"] == "leased"


# ===========================================================================
# 5. HTTP Endpoints
# ===========================================================================

class TestLeaseEndpoint:

    def test_lease_204_when_nothing_ready(self, client):
        c, cfg = client
        resp = c.post("/lease", headers={"X-Token": cfg.secret_token})
        assert resp.status_code == 204

    def test_lease_200_with_four_files(self, client):
        c, cfg = client
        _insert_four(cfg.database_path)
        resp = c.post("/lease", headers={"X-Token": cfg.secret_token})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "batch_id" in data
        assert "lease_id" in data
        assert "slots" in data
        assert len(data["slots"]) == 4

    def test_lease_401_without_token(self, client):
        c, cfg = client
        resp = c.post("/lease")
        assert resp.status_code == 401

    def test_lease_force_with_fewer_files(self, client):
        c, cfg = client
        _insert_file(cfg.database_path, "ClinicA", "90gsm", "Dr_Solo")
        resp = c.post(
            "/lease",
            json={"force": True},
            headers={"X-Token": cfg.secret_token},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["is_forced"] is True


class TestHeartbeatEndpoint:

    def test_heartbeat_200_on_valid_lease(self, client):
        c, cfg = client
        _insert_four(cfg.database_path)
        lease_resp = c.post("/lease", headers={"X-Token": cfg.secret_token})
        assert lease_resp.status_code == 200
        contract = lease_resp.get_json()

        resp = c.post(
            "/job-heartbeat",
            json={
                "batch_id": contract["batch_id"],
                "lease_id": contract["lease_id"],
            },
            headers={"X-Token": cfg.secret_token},
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "extended"

    def test_heartbeat_404_wrong_lease_id(self, client):
        c, cfg = client
        _insert_four(cfg.database_path)
        lease_resp = c.post("/lease", headers={"X-Token": cfg.secret_token})
        contract = lease_resp.get_json()

        resp = c.post(
            "/job-heartbeat",
            json={
                "batch_id": contract["batch_id"],
                "lease_id": "wrong-lease",
            },
            headers={"X-Token": cfg.secret_token},
        )
        assert resp.status_code == 404

    def test_heartbeat_422_missing_fields(self, client):
        c, cfg = client
        resp = c.post(
            "/job-heartbeat",
            json={"batch_id": "only-batch"},
            headers={"X-Token": cfg.secret_token},
        )
        assert resp.status_code == 422


class TestJobResultEndpoint:

    def test_job_result_202_accepted(self, client):
        c, cfg = client
        _insert_four(cfg.database_path)
        lease_resp = c.post("/lease", headers={"X-Token": cfg.secret_token})
        contract = lease_resp.get_json()

        resp = c.post(
            "/job-result",
            json={
                "batch_id": contract["batch_id"],
                "lease_id": contract["lease_id"],
                "success": True,
                "duration_ms": 8000,
            },
            headers={"X-Token": cfg.secret_token},
        )
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["status"] == "accepted"

    def test_job_result_422_missing_fields(self, client):
        c, cfg = client
        resp = c.post(
            "/job-result",
            json={"batch_id": "b", "lease_id": "l"},  # missing success
            headers={"X-Token": cfg.secret_token},
        )
        assert resp.status_code == 422

    def test_job_result_401_without_token(self, client):
        c, _ = client
        resp = c.post("/job-result", json={"batch_id": "b", "lease_id": "l", "success": True})
        assert resp.status_code == 401


class TestFakePluginValidation:
    """Validates the fake_plugin helper worker against the server endpoints."""

    def test_fake_plugin_single_batch_lifecycle(self, client):
        c, cfg = client
        _insert_four(cfg.database_path, clinic="ClinicPlugin")

        from tools.fake_plugin import FakePhotoshopPlugin

        plugin = FakePhotoshopPlugin(
            base_url="http://localhost",
            auth_token=cfg.secret_token,
            render_delay_seconds=0.01,
            output_format="tiff",
        )

        # Mock the plugin's _http_request to route through Flask test client
        def mock_request(endpoint, method="GET", payload=None):
            headers = {"X-Token": cfg.secret_token}
            if method == "POST":
                resp = c.post(endpoint, json=payload, headers=headers)
            else:
                resp = c.get(endpoint, headers=headers)
            data = resp.get_json(silent=True) if resp.data else None
            return resp.status_code, data

        plugin._http_request = mock_request

        contract = plugin.lease_batch()
        assert contract is not None
        assert len(contract["slots"]) == 4

        success = plugin.run_batch(contract)
        assert success is True

