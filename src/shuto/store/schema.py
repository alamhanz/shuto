"""The database schema: version, the first-version DDL and the step-by-step upgrade."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA_V1 = """
CREATE TABLE batches(
  id TEXT PRIMARY KEY, created_at TEXT, notes TEXT DEFAULT '', source TEXT);
CREATE TABLE photos(
  id INTEGER PRIMARY KEY,
  batch_id TEXT REFERENCES batches(id),
  sha256 TEXT UNIQUE NOT NULL,
  dhash INTEGER, src_path TEXT NOT NULL, orig_name TEXT NOT NULL, width INT, height INT,
  captured_at TEXT, camera TEXT, has_gps INT DEFAULT 0, status TEXT NOT NULL,
  near_dup_group INTEGER,
  cull_json TEXT, metadata_json TEXT, metadata_source TEXT, detected_json TEXT,
  guardrail_json TEXT, release_ids TEXT DEFAULT '[]',
  submit_name TEXT UNIQUE, prepared_path TEXT,
  uploaded_at TEXT, exported_at TEXT, export_file TEXT, submitted_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_photos_batch_status ON photos(batch_id, status);
CREATE INDEX idx_photos_status ON photos(status);
CREATE TABLE rejections(
  id INTEGER PRIMARY KEY, photo_id INT REFERENCES photos(id),
  reason_code TEXT NOT NULL, note TEXT, at TEXT NOT NULL);
CREATE TABLE llm_calls(
  id INTEGER PRIMARY KEY, photo_id INT, provider TEXT, model TEXT, input_tokens INT,
  output_tokens INT, seconds REAL, repaired INT, retries INT, ok INT, error TEXT, at TEXT);
CREATE TABLE events(
  id INTEGER PRIMARY KEY, photo_id INT, batch_id TEXT, kind TEXT, detail_json TEXT, at TEXT);
CREATE INDEX idx_events_photo ON events(photo_id);
CREATE TABLE jobs(
  id INTEGER PRIMARY KEY, kind TEXT, batch_id TEXT, state TEXT, done INT, total INT,
  error TEXT, started_at TEXT, finished_at TEXT);
CREATE TABLE settings(
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT);
"""


def migrate(conn: sqlite3.Connection, path: Path) -> None:
    """Create or upgrade the database behind `conn` to SCHEMA_VERSION (one transaction)."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database {path} is newer (v{version}) than this shuto (v{SCHEMA_VERSION})."
            )
        if version < 1:
            for stmt in SCHEMA_V1.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute("PRAGMA user_version=1")
        if version < 2:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
            if "filters_json" not in cols:
                conn.execute("ALTER TABLE photos ADD COLUMN filters_json TEXT")
            conn.execute("PRAGMA user_version=2")
        if version < 3:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
            if "draft_json" not in cols:
                conn.execute("ALTER TABLE photos ADD COLUMN draft_json TEXT")
            conn.execute("PRAGMA user_version=3")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
