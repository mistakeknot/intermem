"""Tests for MetadataStore — SQLite-backed entry provenance and citation tracking."""
from __future__ import annotations

import threading

import pytest

from intermem.metadata import MetadataStore, STALE_THRESHOLD


@pytest.fixture
def store(tmp_path):
    """Create a MetadataStore with a temporary database."""
    return MetadataStore(tmp_path / ".intermem" / "metadata.db")


class TestEnsureSchema:
    def test_creates_database(self, tmp_path):
        db_path = tmp_path / ".intermem" / "metadata.db"
        store = MetadataStore(db_path)
        assert db_path.exists()
        store.close()

    def test_idempotent(self, store):
        store.ensure_schema()
        store.ensure_schema()
        row = store.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
        assert row[0] == 1

    def test_schema_version_seeded(self, store):
        row = store.conn.execute(
            "SELECT version, migration_version FROM schema_version"
        ).fetchone()
        assert row["version"] == 1
        assert row["migration_version"] == 0


class TestUpsertEntry:
    def test_insert_new_entry(self, store):
        store.upsert_entry("abc123", "Preview text", "Section A", "MEMORY.md")
        store.conn.commit()
        entry = store.get_entry("abc123")
        assert entry is not None
        assert entry["content_preview"] == "Preview text"
        assert entry["section"] == "Section A"
        assert entry["source_file"] == "MEMORY.md"
        assert entry["snapshot_count"] == 1
        assert entry["confidence"] == 0.5
        assert entry["status"] == "active"

    def test_update_increments_snapshot_count(self, store):
        store.upsert_entry("abc123", "Preview", "Section", "file.md")
        store.upsert_entry("abc123", "Preview", "Section", "file.md")
        store.upsert_entry("abc123", "Preview", "Section", "file.md")
        store.conn.commit()
        entry = store.get_entry("abc123")
        assert entry["snapshot_count"] == 3

    def test_section_updates_last_write_wins(self, store):
        store.upsert_entry("abc123", "Preview", "Old Section", "file.md")
        store.upsert_entry("abc123", "Preview", "New Section", "file.md")
        store.conn.commit()
        entry = store.get_entry("abc123")
        assert entry["section"] == "New Section"

    def test_concurrent_upsert_atomic_increment(self, tmp_path):
        """Multiple threads upserting the same entry get correct total count.

        Each thread opens its own connection (SQLite requires this).
        The atomic ON CONFLICT DO UPDATE ensures no lost increments.
        """
        db_path = tmp_path / ".intermem" / "concurrent.db"
        n_threads = 4
        n_per_thread = 50

        def worker():
            s = MetadataStore(db_path)
            for _ in range(n_per_thread):
                s.upsert_entry("hash_concurrent", "Preview", "S", "f.md")
                s.conn.commit()
            s.close()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check = MetadataStore(db_path)
        entry = check.get_entry("hash_concurrent")
        assert entry["snapshot_count"] == n_threads * n_per_thread
        check.close()


class TestCitations:
    def test_record_and_query_citation(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.record_citation("h1", "file_path", "src/main.py", "/root/proj/src/main.py")
        store.conn.commit()

        rows = store.conn.execute(
            "SELECT * FROM citations WHERE entry_hash = 'h1'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["citation_type"] == "file_path"
        assert rows[0]["citation_value"] == "src/main.py"
        assert rows[0]["status"] == "unchecked"

    def test_record_check_updates_citation_status(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.record_citation("h1", "file_path", "src/main.py", None)
        store.record_check("h1", "src/main.py", "file_exists", "valid", None)
        store.conn.commit()

        row = store.conn.execute(
            "SELECT status, last_validated FROM citations WHERE entry_hash = 'h1'"
        ).fetchone()
        assert row["status"] == "valid"
        assert row["last_validated"] is not None

    def test_record_check_appends_to_audit_log(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.record_check("h1", "path.py", "file_exists", "valid", "exists")
        store.record_check("h1", "path.py", "file_exists", "broken", "deleted")
        store.conn.commit()

        rows = store.conn.execute(
            "SELECT * FROM citation_checks WHERE entry_hash = 'h1'"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["result"] == "valid"
        assert rows[1]["result"] == "broken"


class TestConfidence:
    def test_update_confidence_active(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.update_confidence("h1", 0.8)
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["confidence"] == 0.8
        assert entry["status"] == "active"
        assert entry["confidence_updated_at"] is not None

    def test_update_confidence_stale(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.update_confidence("h1", 0.1)
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["confidence"] == 0.1
        assert entry["status"] == "stale"

    def test_stale_threshold_boundary(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.update_confidence("h1", STALE_THRESHOLD)
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["status"] == "active"

        store.update_confidence("h1", STALE_THRESHOLD - 0.01)
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["status"] == "stale"

    def test_get_stale_entries(self, store):
        store.upsert_entry("h1", "good entry", "sec", "f.md")
        store.update_confidence("h1", 0.8)
        store.upsert_entry("h2", "stale entry", "sec", "f.md")
        store.update_confidence("h2", 0.1)
        store.upsert_entry("h3", "also stale", "sec", "f.md")
        store.update_confidence("h3", 0.2)
        store.conn.commit()

        stale = store.get_stale_entries()
        hashes = {e["entry_hash"] for e in stale}
        assert hashes == {"h2", "h3"}
        assert all(
            set(e.keys()) == {"entry_hash", "section", "confidence", "content_preview"}
            for e in stale
        )


class TestUncheckedCitations:
    def test_returns_unchecked_for_active_entries(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.record_citation("h1", "file_path", "src/a.py", None)
        store.conn.commit()

        unchecked = store.get_unchecked_citations()
        assert len(unchecked) == 1
        assert unchecked[0]["citation_value"] == "src/a.py"

    def test_skips_stale_entries(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.update_confidence("h1", 0.1)  # stale
        store.record_citation("h1", "file_path", "src/a.py", None)
        store.conn.commit()

        unchecked = store.get_unchecked_citations()
        assert len(unchecked) == 0


class TestTransactions:
    def test_commit(self, store):
        store.begin_transaction()
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.commit_transaction()

        entry = store.get_entry("h1")
        assert entry is not None

    def test_rollback(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.conn.commit()

        store.begin_transaction()
        store.upsert_entry("h2", "preview2", "sec", "f.md")
        store.rollback_transaction()

        entry = store.get_entry("h2")
        assert entry is None


class TestMigrationHelpers:
    def test_table_exists(self, store):
        assert store._table_exists("memory_entries")
        assert not store._table_exists("nonexistent")

    def test_column_exists(self, store):
        assert store._column_exists("memory_entries", "entry_hash")
        assert not store._column_exists("memory_entries", "nonexistent")
