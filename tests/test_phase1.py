import os
import tempfile
import pytest
from server.config import AppConfig
from server.database import init_db, get_connection, db_transaction, stamp_boot_id
from server.app import create_app


@pytest.fixture
def temp_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_roshta.db")
        cfg = AppConfig(
            database_path=db_path,
            log_dir=os.path.join(tmpdir, "logs"),
            secret_token="test-secret-token"
        )
        init_db(cfg.database_path)
        yield cfg


@pytest.fixture
def client(temp_config):
    app = create_app(custom_config=temp_config)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_database_initialization(temp_config):
    """Verify all required tables, WAL mode, FULL sync, and initial seeds are in place."""
    conn = get_connection(temp_config.database_path)
    try:
        # WAL mode
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        # FULL synchronous (value 2)
        sync_mode = conn.execute("PRAGMA synchronous;").fetchone()[0]
        assert sync_mode == 2, f"Expected synchronous=FULL (2), got {sync_mode}"

        # Required tables
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        ]
        expected_tables = [
            "schema_version",
            "holding_files",
            "batches",
            "batch_slots",
            "folder_exclusions",
            "system_state",
        ]
        for t in expected_tables:
            assert t in tables, f"Expected table '{t}' was not created"

        # Migration tracking
        applied_rows = conn.execute(
            "SELECT version, filename, applied_at FROM schema_version;"
        ).fetchall()
        assert len(applied_rows) == 1
        assert applied_rows[0]["filename"] == "init.sql"
        assert applied_rows[0]["version"] == 1

        # Seeded system_state rows
        state_rows = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM system_state;").fetchall()
        }
        assert "next_batch_number" in state_rows
        assert state_rows["next_batch_number"] == "1"
        assert "boot_id" in state_rows

        # holding_files has expected columns (including new status and batch_id)
        cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(holding_files);").fetchall()
        ]
        for expected_col in ["id", "file_path", "status", "batch_id", "is_skipped", "first_seen_at"]:
            assert expected_col in cols, f"Column '{expected_col}' missing from holding_files"

        # batches has csv_exported_at and is_forced
        batch_cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(batches);").fetchall()
        ]
        for expected_col in ["csv_exported_at", "slip_generated_at", "is_forced", "lease_expires_at"]:
            assert expected_col in batch_cols, f"Column '{expected_col}' missing from batches"

    finally:
        conn.close()


def test_health_endpoint(client):
    """Verify GET /health responds with valid JSON and status ok."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"
    assert "uptime_seconds" in data
    assert "resume_summary" in data
    assert data["resume_summary"]["holding_files"] == 0


def test_token_authentication(client, temp_config):
    """Verify endpoints enforce X-Token authentication."""
    unauthed = client.get("/state")
    assert unauthed.status_code == 401
    assert unauthed.get_json()["error"]["code"] == "UNAUTHORIZED"

    authed = client.get("/state", headers={"X-Token": temp_config.secret_token})
    assert authed.status_code in (200, 404)  # Passes auth; /state may not be mounted yet


def test_doctor_filename_regex(temp_config):
    """Verify doctor filename parsing and numerical suffix stripping."""
    regex = temp_config.doctor_regex
    test_cases = [
        ("Dr_Smith.psd",       "Dr_Smith"),
        ("Dr_Smith_01.psd",    "Dr_Smith"),
        ("Dr_Smith_2.psb",     "Dr_Smith"),
        ("Dr-Ahmed-03.tif",    "Dr-Ahmed"),
        ("Dr. Mona 4.tiff",    "Dr. Mona"),
    ]
    for filename, expected_doctor in test_cases:
        match = regex.match(filename)
        assert match is not None, f"Failed to match: {filename}"
        assert match.group(1) == expected_doctor, (
            f"Expected '{expected_doctor}', got '{match.group(1)}'"
        )


def test_stamp_boot_id(temp_config):
    """Verify stamp_boot_id() writes a valid UUID to system_state."""
    boot_id = stamp_boot_id(temp_config.database_path)
    assert len(boot_id) == 36  # Standard UUID4 format
    assert boot_id.count("-") == 4

    conn = get_connection(temp_config.database_path)
    try:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = 'boot_id';"
        ).fetchone()
        assert row is not None
        assert row["value"] == boot_id
    finally:
        conn.close()

    # Calling stamp_boot_id again should produce a different UUID
    second_id = stamp_boot_id(temp_config.database_path)
    assert second_id != boot_id
