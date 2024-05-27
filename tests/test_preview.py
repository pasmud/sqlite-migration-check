import sqlite3
from contextlib import closing

import pytest

from sqlite_migration_check import (
    ForeignKeyViolation,
    PreviewError,
    PreviewLimits,
    preview,
)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "demo ?#%.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE notes (body TEXT NOT NULL); INSERT INTO notes VALUES ('demo');")
    return path


def test_preview_uses_source_rows_and_leaves_file_unchanged(source):
    original = source.read_bytes()
    result = preview(
        source,
        """
        BEGIN;
        ALTER TABLE notes ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
        CREATE TABLE migrated (body TEXT CHECK (body = 'demo'));
        INSERT INTO migrated SELECT body FROM notes;
        COMMIT;
        """,
    )
    assert result.ok
    assert source.read_bytes() == original
    assert list(source.parent.iterdir()) == [source]


def test_failure_after_ddl_does_not_change_source(source):
    original = source.read_bytes()
    with pytest.raises(PreviewError, match="no such table") as error:
        preview(source, "DROP TABLE notes; INSERT INTO missing VALUES (1);")
    assert error.value.stage == "migration"
    assert source.read_bytes() == original


def test_unfinished_transaction_is_an_error(source):
    with pytest.raises(PreviewError, match="left a transaction open"):
        preview(source, "BEGIN; DELETE FROM notes;")


def test_missing_source_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(PreviewError) as error:
        preview(path, "CREATE TABLE notes (body TEXT);")
    assert error.value.stage == "source"
    assert not path.exists()


def test_existing_foreign_key_problem_is_reported(source):
    with closing(sqlite3.connect(source)) as db:
        db.executescript("""
            CREATE TABLE accounts (id INTEGER PRIMARY KEY);
            CREATE TABLE tags (account INTEGER REFERENCES accounts(id));
            INSERT INTO tags VALUES (10);
        """)
    result = preview(source, "SELECT 1;")
    assert not result.ok
    assert result.integrity_errors == ()
    assert result.foreign_key_violations == (ForeignKeyViolation("tags", 1, "accounts", 0),)


def test_existing_check_constraint_violation_is_reported_by_integrity_check(source):
    with closing(sqlite3.connect(source)) as db:
        db.executescript("""
            CREATE TABLE counter (value INTEGER CHECK (value >= 0));
            PRAGMA ignore_check_constraints = ON;
            INSERT INTO counter VALUES (-1);
        """)
    original = source.read_bytes()
    result = preview(source, "SELECT 1;")
    assert not result.ok
    assert any("CHECK constraint failed in counter" in error for error in result.integrity_errors)
    assert result.foreign_key_violations == ()
    assert source.read_bytes() == original


@pytest.mark.parametrize("statement", ["ATTACH DATABASE 'other.sqlite' AS other", "VACUUM INTO 'other.sqlite'"])
def test_migration_cannot_create_other_database_files(source, monkeypatch, statement):
    monkeypatch.chdir(source.parent)
    with pytest.raises(PreviewError) as error:
        preview(source, statement)
    assert error.value.stage == "migration"
    assert list(source.parent.iterdir()) == [source]


def test_migration_cannot_disable_foreign_keys(source):
    with pytest.raises(PreviewError, match="not authorized"):
        preview(source, "PRAGMA foreign_keys = OFF")
    with pytest.raises(PreviewError, match="FOREIGN KEY constraint failed"):
        preview(source, """
            CREATE TABLE accounts (id INTEGER PRIMARY KEY);
            CREATE TABLE tags (account INTEGER REFERENCES accounts(id));
            INSERT INTO tags VALUES (10);
        """)


@pytest.mark.parametrize("script", ["", " \n\t", "SELECT 1;\x00", b"SELECT 1;"])
def test_rejects_invalid_script(source, script):
    with pytest.raises(PreviewError) as error:
        preview(source, script)
    assert error.value.stage == "input"


def test_sql_limit_counts_utf8_bytes(source):
    sql = "SELECT 'é';"
    with pytest.raises(PreviewError, match="SQL byte limit"):
        preview(source, sql, limits=PreviewLimits(max_sql_bytes=len(sql)))


def test_source_and_scratch_size_are_bounded(source):
    with pytest.raises(PreviewError, match="scratch database byte limit") as error:
        preview(source, "SELECT 1;", limits=PreviewLimits(max_db_bytes=1024))
    assert error.value.stage == "backup"

    original = source.read_bytes()
    with pytest.raises(PreviewError, match="database or disk is full") as error:
        preview(source, "INSERT INTO notes VALUES (zeroblob(100000));",
                limits=PreviewLimits(max_db_bytes=len(original)))
    assert error.value.stage == "migration"
    assert source.read_bytes() == original


def test_long_running_migration_is_interrupted(source):
    with pytest.raises(PreviewError, match="time limit exceeded") as error:
        preview(source, """
            WITH RECURSIVE counts(n) AS (VALUES (1) UNION ALL SELECT n+1 FROM counts)
            SELECT SUM(n) FROM counts;
        """, limits=PreviewLimits(timeout_seconds=0.1))
    assert error.value.stage == "migration"


def test_check_rows_are_bounded(source):
    with closing(sqlite3.connect(source)) as db:
        db.executescript("""
            CREATE TABLE accounts (id INTEGER PRIMARY KEY);
            CREATE TABLE tags (account INTEGER REFERENCES accounts(id));
            INSERT INTO tags VALUES (10), (20), (30);
        """)
    result = preview(source, "SELECT 1;", limits=PreviewLimits(max_check_rows=2))
    assert not result.ok
    assert result.checks_truncated
    assert len(result.foreign_key_violations) == 2


def test_backup_reads_committed_wal_without_changing_database_or_wal(source):
    with closing(sqlite3.connect(source)) as db:
        assert db.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        db.executescript("CREATE TABLE in_wal (id INTEGER); INSERT INTO in_wal VALUES (7);")
        wal = source.with_name(source.name + "-wal")
        original = source.read_bytes(), wal.read_bytes()
        assert preview(source, """
            CREATE TABLE copied (id INTEGER CHECK (id = 7));
            INSERT INTO copied SELECT id FROM in_wal;
        """).ok
        assert (source.read_bytes(), wal.read_bytes()) == original


def test_wal_with_no_sidecars_is_rejected_without_creating_them(source):
    with closing(sqlite3.connect(source)) as db:
        assert db.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    with pytest.raises(PreviewError, match="requires existing .* sidecars"):
        preview(source, "SELECT 1;")
    assert list(source.parent.iterdir()) == [source]


def test_invalid_database_reports_backup_error(source):
    source.write_bytes(b"fake database content" * 20)
    with pytest.raises(PreviewError, match="not a database") as error:
        preview(source, "SELECT 1;")
    assert error.value.stage == "backup"


def test_orders_table_and_index_changes_and_keeps_index_terms(source):
    with closing(sqlite3.connect(source)) as db:
        db.executescript("""
            CREATE TABLE obsolete (id INTEGER);
            CREATE INDEX old_index ON notes(body);
        """)
    result = preview(source, """
        CREATE TABLE "team's tags" (id INTEGER PRIMARY KEY, label TEXT UNIQUE);
        DROP TABLE obsolete;
        ALTER TABLE notes ADD COLUMN rank INTEGER DEFAULT 0;
        DROP INDEX old_index;
        CREATE INDEX ranked ON notes(lower(body), rank COLLATE BINARY DESC) WHERE rank > 0;
    """)
    assert result.ok
    assert [(change.action, (change.after or change.before).name) for change in result.changes] == [
        ("changed", "notes"), ("removed", "obsolete"), ("added", "team's tags"),
        ("removed", "old_index"), ("added", "ranked"), ("added", "sqlite_autoindex_team's tags_1"),
    ]
    table = result.changes[0]
    assert table.before.sql == "CREATE TABLE notes (body TEXT NOT NULL)"
    assert "rank INTEGER DEFAULT 0" in table.after.sql
    index = result.changes[-2].after.index
    assert index.partial and not index.unique and index.origin == "c"
    assert index.terms[0].expression and index.terms[0].column is None
    assert index.terms[1].column == "rank" and index.terms[1].descending
    implicit = result.changes[-1].after
    assert implicit.sql is None
    assert implicit.index.unique and implicit.index.origin == "u"
    assert implicit.index.terms[0].column == "label"


def test_rebuilt_index_and_without_rowid_primary_key(source):
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE INDEX note_order ON notes(body ASC)")
    result = preview(source, """
        DROP INDEX note_order;
        CREATE INDEX note_order ON notes(body DESC);
        CREATE TABLE catalog (tag TEXT PRIMARY KEY) WITHOUT ROWID;
    """)
    changed, primary = [item for item in result.changes if item.after.kind == "index"]
    assert changed.action == "changed" and changed.after.name == "note_order"
    assert changed.after.index.terms[0].descending
    assert primary.after.index.origin == "pk"
    assert primary.after.index.terms[0].column == "tag"


def test_reports_final_schema_not_intermediate_or_data_changes(source):
    result = preview(source, """
        INSERT INTO notes VALUES ('another note');
        SAVEPOINT tentative;
        CREATE TABLE discarded (id INTEGER);
        ROLLBACK TO tentative;
        RELEASE tentative;
        CREATE VIEW note_bodies AS SELECT body FROM notes;
    """)
    assert result.ok
    assert result.changes == ()
