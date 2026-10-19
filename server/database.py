import sqlite3
import os
import uuid
import logging
from contextlib import contextmanager
from typing import Generator, List
from server.config import config

logger = logging.getLogger("database")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(BASE_DIR, "migrations")


def get_connection(db_path: str = None) -> sqlite3.Connection:
    """
    Return a SQLite connection for the calling thread.

    WAL mode:     Allows concurrent readers while a single writer is active.
    FULL sync:    Flushes WAL frames to disk on each COMMIT. Safe against
                  power loss on shop PCs. Write volume is tiny so the cost
                  is negligible.
    busy_timeout: Waitress is multi-threaded; 5 s is enough for any lock
                  to clear before raising "database is locked".
    """
    if not db_path:
        try:
            from flask import current_app, has_app_context
            if has_app_context() and "APP_CONFIG" in current_app.config:
                db_path = current_app.config["APP_CONFIG"].database_path
        except Exception:
            pass
    path = db_path or config.database_path

    # sqlite3.connect() is NOT thread-safe for shared connections.
    # Each caller (= each Waitress worker thread) must open its own connection.
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=True)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous  = FULL;")   # Survive power loss
    conn.execute("PRAGMA busy_timeout = 5000;")   # ms; allows retries under load
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_transaction(db_path: str = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager that opens a connection, yields it inside an explicit
    transaction, commits on success, and rolls back + closes on any exception.

    Usage:
        with db_transaction() as conn:
            conn.execute("INSERT INTO ...")
    """
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_migrations(db_path: str = None, migrations_dir: str = None) -> List[str]:
    """
    Applies any unapplied .sql migration files in sorted order.
    Tracks applied migrations in schema_version by filename so each
    migration runs exactly once.
    """
    path = db_path or config.database_path
    mig_dir = migrations_dir or MIGRATIONS_DIR
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    os.makedirs(mig_dir, exist_ok=True)

    applied_now: List[str] = []

    # Use a bare connection here; we call executescript() which issues an
    # implicit COMMIT before it runs, so we manage the transaction manually.
    conn = get_connection(path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT UNIQUE NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

        existing = {
            row["filename"]
            for row in conn.execute("SELECT filename FROM schema_version;").fetchall()
        }

        sql_files = sorted(f for f in os.listdir(mig_dir) if f.endswith(".sql"))

        for fname in sql_files:
            if fname not in existing:
                file_path = os.path.join(mig_dir, fname)
                logger.info("Applying migration: %s", fname)
                with open(file_path, "r", encoding="utf-8") as fh:
                    sql_content = fh.read()

                # executescript() issues an implicit COMMIT first
                conn.executescript(sql_content)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_version (filename) VALUES (?);",
                    (fname,)
                )
                conn.commit()
                applied_now.append(fname)
    finally:
        conn.close()

    return applied_now


def init_db(db_path: str = None) -> None:
    """Initialises the database by running all pending migrations."""
    apply_migrations(db_path)


def stamp_boot_id(db_path: str = None) -> str:
    """
    Generate a fresh UUID boot_id and store it in system_state.
    Called once at server startup. The plugin compares boot_id across
    /state responses; a change means the server restarted so the client
    must reset its cached rev.
    """
    new_id = str(uuid.uuid4())
    with db_transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO system_state (key, value) VALUES ('boot_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP;",
            (new_id,)
        )
    logger.info("Server boot_id: %s", new_id)
    return new_id
