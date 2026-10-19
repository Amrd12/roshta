import sqlite3
import os
import logging
from contextlib import contextmanager
from typing import Generator, List
from server.config import config

logger = logging.getLogger("database")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(BASE_DIR, "migrations")

def get_connection(db_path: str = None) -> sqlite3.Connection:
    """Returns a SQLite connection configured with WAL mode and row factory."""
    if not db_path:
        try:
            from flask import current_app, has_app_context
            if has_app_context() and "APP_CONFIG" in current_app.config:
                db_path = current_app.config["APP_CONFIG"].database_path
        except Exception:
            pass
    path = db_path or config.database_path
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

@contextmanager
def db_transaction(db_path: str = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for atomic SQLite transactions."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def apply_migrations(db_path: str = None, migrations_dir: str = None) -> List[str]:
    """
    Applies any unapplied .sql migration files in the migrations directory in sorted order.
    Tracks applied migrations in schema_version by filename.
    """
    path = db_path or config.database_path
    mig_dir = migrations_dir or MIGRATIONS_DIR
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    os.makedirs(mig_dir, exist_ok=True)

    applied_now = []

    with db_transaction(path) as conn:
        # 1. Ensure schema_version table exists with filename tracking
        conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT UNIQUE NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 2. Get list of already applied migrations
        existing = {
            row["filename"]
            for row in conn.execute("SELECT filename FROM schema_version").fetchall()
        }

        # 3. Find and sort all .sql migration files
        sql_files = sorted([f for f in os.listdir(mig_dir) if f.endswith(".sql")])

        # 4. Execute pending migrations
        for fname in sql_files:
            if fname not in existing:
                file_path = os.path.join(mig_dir, fname)
                logger.info(f"Applying migration: {fname}")
                with open(file_path, "r", encoding="utf-8") as f:
                    sql_content = f.read()

                conn.executescript(sql_content)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_version (filename) VALUES (?);",
                    (fname,)
                )
                applied_now.append(fname)

    return applied_now

def init_db(db_path: str = None) -> None:
    """Initializes the database by running all pending migrations."""
    apply_migrations(db_path)
