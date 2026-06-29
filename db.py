"""SQLite persistence: submissions, append-only audit log, appeals.

The audit log is append-only *by convention* — no code path here issues UPDATE
or DELETE against `audit_log`. M3 fills the LLM-derived columns; the burstiness /
combined-score columns stay NULL until M4. The appeals table is created now so M5
has somewhere to write.
"""

import json
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "provenance.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id             TEXT PRIMARY KEY,
                created_at     TEXT NOT NULL,
                creator_id     TEXT NOT NULL,
                text_hash      TEXT NOT NULL,
                score_b        REAL,
                score_s        REAL,
                combined_score REAL,
                confidence     REAL,
                attribution    TEXT,
                label          TEXT,
                status         TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                content_id TEXT NOT NULL,
                event_type TEXT NOT NULL,   -- 'decision' | 'appeal'
                payload    TEXT NOT NULL    -- JSON snapshot of the event
            );

            CREATE TABLE IF NOT EXISTS appeals (
                id            TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                reason        TEXT NOT NULL,
                status        TEXT NOT NULL,   -- 'under_review' | 'upheld' | 'overturned'
                resolution    TEXT
            );
            """
        )


def write_submission(row: dict):
    """Insert one scored submission. `row` keys map 1:1 to the submissions columns."""
    with _conn() as c:
        c.execute(
            """
            INSERT INTO submissions
                (id, created_at, creator_id, text_hash, score_b, score_s,
                 combined_score, confidence, attribution, label, status)
            VALUES
                (:id, :created_at, :creator_id, :text_hash, :score_b, :score_s,
                 :combined_score, :confidence, :attribution, :label, :status)
            """,
            row,
        )


def write_audit(content_id: str, event_type: str, payload: dict):
    """Append one entry to the audit log. Never updates or deletes."""
    with _conn() as c:
        c.execute(
            "INSERT INTO audit_log (created_at, content_id, event_type, payload) "
            "VALUES (?, ?, ?, ?)",
            (payload.get("timestamp"), content_id, event_type, json.dumps(payload)),
        )


def get_submission(content_id: str):
    """Return one submission row as a dict, or None if not found."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM submissions WHERE id = ?", (content_id,)
        ).fetchone()
    return dict(row) if row else None


def update_submission_status(content_id: str, status: str):
    """Update a submission's status. (submissions is mutable; audit_log is not.)"""
    with _conn() as c:
        c.execute(
            "UPDATE submissions SET status = ? WHERE id = ?", (status, content_id)
        )


def write_appeal(row: dict):
    """Insert one appeal row."""
    with _conn() as c:
        c.execute(
            """
            INSERT INTO appeals (id, submission_id, created_at, reason, status, resolution)
            VALUES (:id, :submission_id, :created_at, :reason, :status, :resolution)
            """,
            row,
        )


def get_log(limit: int = 50) -> list:
    """Return the most recent audit-log payloads, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT payload FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]
