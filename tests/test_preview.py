import sqlite3
from contextlib import closing

import pytest

from sqlite_migration_check import ForeignKeyViolation, PreviewError, preview


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
