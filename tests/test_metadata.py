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


class TestStaleStreak:
    def test_increment_stale_streak(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.conn.commit()
        assert store.get_entry("h1")["stale_streak"] == 0

        store.increment_stale_streak("h1")
        store.conn.commit()
        assert store.get_entry("h1")["stale_streak"] == 1

        store.increment_stale_streak("h1")
        store.conn.commit()
        assert store.get_entry("h1")["stale_streak"] == 2

    def test_reset_stale_streak(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.increment_stale_streak("h1")
        store.increment_stale_streak("h1")
        store.conn.commit()
        assert store.get_entry("h1")["stale_streak"] == 2

        store.reset_stale_streak("h1")
        store.conn.commit()
        assert store.get_entry("h1")["stale_streak"] == 0


class TestDemotion:
    def test_mark_demoted(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.mark_demoted("h1")
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["status"] == "demoted"
        assert entry["demoted_at"] is not None

    def test_reactivate_entry(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.increment_stale_streak("h1")
        store.increment_stale_streak("h1")
        store.mark_demoted("h1")
        store.conn.commit()

        entry = store.get_entry("h1")
        assert entry["status"] == "demoted"
        assert entry["stale_streak"] == 2
        assert entry["demoted_at"] is not None

        store.reactivate_entry("h1")
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["status"] == "active"
        assert entry["stale_streak"] == 0
        assert entry["demoted_at"] is None

    def test_update_confidence_preserves_demoted(self, store):
        """Demoted entry stays demoted even when update_confidence gives good score."""
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.mark_demoted("h1")
        store.conn.commit()

        store.update_confidence("h1", 0.9)
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["status"] == "demoted"
        assert entry["confidence"] == 0.9

    def test_update_confidence_resets_streak_on_active(self, store):
        """When entry transitions stale->active, stale_streak resets."""
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.update_confidence("h1", 0.1)  # stale
        store.increment_stale_streak("h1")
        store.conn.commit()
        assert store.get_entry("h1")["stale_streak"] == 1

        store.update_confidence("h1", 0.8)  # active again
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["status"] == "active"
        assert entry["stale_streak"] == 0

    def test_get_demoted_entries(self, store):
        store.upsert_entry("h1", "good entry", "sec", "f.md")
        store.upsert_entry("h2", "demoted entry", "sec", "f.md")
        store.mark_demoted("h2")
        store.upsert_entry("h3", "another good", "sec", "f.md")
        store.conn.commit()

        demoted = store.get_demoted_entries()
        assert len(demoted) == 1
        assert demoted[0]["entry_hash"] == "h2"


class TestQueryMethods:
    def test_search_entries(self, store):
        store.upsert_entry("h1", "SQLite WAL mode", "Database", "f.md")
        store.upsert_entry("h2", "Git workflow", "Git", "f.md")
        store.upsert_entry("h3", "SQLite journal", "Database", "f.md")
        store.conn.commit()

        results = store.search_entries("SQLite")
        assert len(results) == 2
        hashes = {r["entry_hash"] for r in results}
        assert hashes == {"h1", "h3"}

    def test_search_entries_multiple_keywords(self, store):
        store.upsert_entry("h1", "SQLite WAL mode", "Database", "f.md")
        store.upsert_entry("h2", "SQLite journal", "Database", "f.md")
        store.conn.commit()

        results = store.search_entries("SQLite WAL")
        assert len(results) == 1
        assert results[0]["entry_hash"] == "h1"

    def test_search_entries_empty_keywords(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.conn.commit()
        assert store.search_entries("") == []
        assert store.search_entries("   ") == []

    def test_search_entries_sql_injection(self, store):
        """SQL injection attempt doesn't crash or corrupt."""
        store.upsert_entry("h1", "normal entry", "sec", "f.md")
        store.conn.commit()
        results = store.search_entries("'; DROP TABLE memory_entries; --")
        assert isinstance(results, list)
        # Table still exists
        assert store.get_entry("h1") is not None

    def test_get_topics(self, store):
        store.upsert_entry("h1", "fact 1", "Database", "f.md")
        store.upsert_entry("h2", "fact 2", "Database", "f.md")
        store.upsert_entry("h3", "fact 3", "Git", "f.md")
        store.conn.commit()

        topics = store.get_topics()
        assert len(topics) == 2
        db_topic = next(t for t in topics if t["section"] == "Database")
        assert db_topic["entry_count"] == 2

    def test_get_topics_excludes_demoted(self, store):
        store.upsert_entry("h1", "active", "Database", "f.md")
        store.upsert_entry("h2", "demoted", "Database", "f.md")
        store.mark_demoted("h2")
        store.conn.commit()

        topics = store.get_topics()
        db_topic = next(t for t in topics if t["section"] == "Database")
        assert db_topic["entry_count"] == 1


class TestMigrationV2:
    def test_migrate_to_v2_adds_columns(self, store):
        assert store._column_exists("memory_entries", "stale_streak")
        assert store._column_exists("memory_entries", "demoted_at")

    def test_migrate_to_v2_idempotent(self, store):
        store._migrate_to_v2()
        store._migrate_to_v2()
        assert store._column_exists("memory_entries", "stale_streak")

    def test_new_entry_has_zero_stale_streak(self, store):
        store.upsert_entry("h1", "preview", "sec", "f.md")
        store.conn.commit()
        entry = store.get_entry("h1")
        assert entry["stale_streak"] == 0
        assert entry["demoted_at"] is None


class TestMigrationHelpers:
    def test_table_exists(self, store):
        assert store._table_exists("memory_entries")
        assert not store._table_exists("nonexistent")

    def test_column_exists(self, store):
        assert store._column_exists("memory_entries", "entry_hash")
        assert not store._column_exists("memory_entries", "nonexistent")
