"""SQLite-backed metadata store for entry provenance and citation tracking."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    migration_version INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_entries (
    entry_hash TEXT PRIMARY KEY,
    content_preview TEXT NOT NULL,
    section TEXT NOT NULL,
    source_file TEXT NOT NULL,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    snapshot_count INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0.5,
    confidence_updated_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'stale', 'orphaned'))
);

CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_hash TEXT NOT NULL
        REFERENCES memory_entries(entry_hash) ON DELETE CASCADE,
    citation_type TEXT NOT NULL,
    citation_value TEXT NOT NULL,
    resolved_path TEXT,
    status TEXT NOT NULL DEFAULT 'unchecked',
    last_validated TEXT,
    UNIQUE(entry_hash, citation_value)
);

CREATE TABLE IF NOT EXISTS citation_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_hash TEXT NOT NULL
        REFERENCES memory_entries(entry_hash) ON DELETE CASCADE,
    citation_value TEXT NOT NULL,
    check_type TEXT NOT NULL,
    result TEXT NOT NULL,
    detail TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

STALE_THRESHOLD = 0.3


class MetadataStore:
    """SQLite store for entry provenance, citations, and audit checks."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create tables if they don't exist (idempotent)."""
        self.conn.executescript(_SCHEMA)
        # Seed schema_version if empty.
        row = self.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
        if row[0] == 0:
            self.conn.execute(
                "INSERT INTO schema_version (version, migration_version) VALUES (1, 0)"
            )
            self.conn.commit()

    def upsert_entry(
        self,
        entry_hash: str,
        content_preview: str,
        section: str,
        source_file: str,
    ) -> None:
        """Insert or update an entry with atomic snapshot_count increment."""
        self.conn.execute(
            """\
            INSERT INTO memory_entries
                (entry_hash, content_preview, section, source_file, snapshot_count, last_seen)
            VALUES (?, ?, ?, ?, 1, datetime('now'))
            ON CONFLICT(entry_hash) DO UPDATE SET
                snapshot_count = snapshot_count + 1,
                last_seen = datetime('now'),
                section = excluded.section,
                source_file = excluded.source_file
            """,
            (entry_hash, content_preview, section, source_file),
        )

    def record_citation(
        self,
        entry_hash: str,
        citation_type: str,
        citation_value: str,
        resolved_path: str | None = None,
    ) -> None:
        """Record an extracted citation for an entry (idempotent per entry+value)."""
        self.conn.execute(
            """\
            INSERT INTO citations (entry_hash, citation_type, citation_value, resolved_path)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(entry_hash, citation_value) DO UPDATE SET
                citation_type = excluded.citation_type,
                resolved_path = excluded.resolved_path
            """,
            (entry_hash, citation_type, citation_value, resolved_path),
        )

    def record_check(
        self,
        entry_hash: str,
        citation_value: str,
        check_type: str,
        result: str,
        detail: str | None = None,
    ) -> None:
        """Append a citation check to the audit log."""
        self.conn.execute(
            """\
            INSERT INTO citation_checks
                (entry_hash, citation_value, check_type, result, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entry_hash, citation_value, check_type, result, detail),
        )
        # Update citation status.
        self.conn.execute(
            """\
            UPDATE citations SET status = ?, last_validated = datetime('now')
            WHERE entry_hash = ? AND citation_value = ?
            """,
            (result, entry_hash, citation_value),
        )

    def update_confidence(self, entry_hash: str, confidence: float) -> None:
        """Set confidence and derive status from threshold."""
        status = "active" if confidence >= STALE_THRESHOLD else "stale"
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """\
            UPDATE memory_entries
            SET confidence = ?, confidence_updated_at = ?, status = ?
            WHERE entry_hash = ?
            """,
            (confidence, now, status, entry_hash),
        )

    def get_stale_entries(self) -> list[dict]:
        """Return entries with confidence below the stale threshold."""
        rows = self.conn.execute(
            """\
            SELECT entry_hash, section, confidence, content_preview
            FROM memory_entries WHERE status = 'stale'
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_unchecked_citations(self, max_age_hours: int = 24) -> list[dict]:
        """Return citations needing validation, filtered to active entries only."""
        rows = self.conn.execute(
            """\
            SELECT c.entry_hash, c.citation_type, c.citation_value, c.resolved_path
            FROM citations c
            JOIN memory_entries e ON c.entry_hash = e.entry_hash
            WHERE e.status = 'active'
              AND (c.last_validated IS NULL
                   OR c.last_validated < datetime('now', ? || ' hours'))
            """,
            (f"-{max_age_hours}",),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_entry(self, entry_hash: str) -> dict | None:
        """Get full entry metadata by hash."""
        row = self.conn.execute(
            "SELECT * FROM memory_entries WHERE entry_hash = ?",
            (entry_hash,),
        ).fetchone()
        return dict(row) if row else None

    def begin_transaction(self) -> None:
        """Begin an explicit transaction."""
        self.conn.execute("BEGIN")

    def commit_transaction(self) -> None:
        """Commit the current transaction."""
        self.conn.commit()

    def rollback_transaction(self) -> None:
        """Roll back the current transaction."""
        self.conn.rollback()

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    # -- Migration helpers --

    def _table_exists(self, table_name: str) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row[0] > 0

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(row["name"] == column_name for row in rows)
