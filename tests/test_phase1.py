import os
import tempfile
import pytest
from server.config import AppConfig
from server.database import init_db, get_connection, db_transaction
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
    """Verify that all required tables and WAL mode are initialized in SQLite."""
    conn = get_connection(temp_config.database_path)
    try:
        # Check WAL mode
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        # Check tables
        tables = [row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()]
        
        expected_tables = [
            "schema_version",
            "holding_files",
            "batches",
            "batch_slots",
            "folder_exclusions",
            "address_book",
            "system_state"
        ]
        for t in expected_tables:
            assert t in tables, f"Expected table '{t}' was not created"

        # Verify migration tracking in schema_version table
        applied_rows = conn.execute("SELECT version, filename, applied_at FROM schema_version;").fetchall()
        assert len(applied_rows) == 1
        assert applied_rows[0]["filename"] == "init.sql"
        assert applied_rows[0]["version"] == 1
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
    # A non-existent endpoint will be blocked by token auth first
    unauthed = client.get("/state")
    assert unauthed.status_code == 401
    assert unauthed.get_json()["error"]["code"] == "UNAUTHORIZED"

    # With valid token, it passes auth (even if 404 because /state isn't mounted yet)
    authed = client.get("/state", headers={"X-Token": temp_config.secret_token})
    assert authed.status_code in (200, 404) # Not 401!

def test_doctor_filename_regex(temp_config):
    """Verify doctor filename parsing and numerical suffix stripping."""
    regex = temp_config.doctor_regex
    
    test_cases = [
        ("Dr_Smith.psd", "Dr_Smith"),
        ("Dr_Smith_01.psd", "Dr_Smith"),
        ("Dr_Smith_2.psb", "Dr_Smith"),
        ("Dr-Ahmed-03.tif", "Dr-Ahmed"),
        ("Dr. Mona 4.tiff", "Dr. Mona"),
    ]
    
    for filename, expected_doctor in test_cases:
        match = regex.match(filename)
        assert match is not None, f"Failed to match: {filename}"
        assert match.group(1) == expected_doctor, f"Expected {expected_doctor}, got {match.group(1)}"
