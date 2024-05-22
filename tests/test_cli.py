import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "fake notes.sqlite"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE notes (body TEXT)")
    return source, tmp_path / "fake migration.sql"


def invoke(inputs, *args):
    return subprocess.run(
        [sys.executable, "-m", "sqlite_migration_check", *map(str, inputs), *args],
        capture_output=True, text=True, timeout=10, check=False,
    )


def test_json_is_stable_and_reports_diff(inputs):
    source, migration = inputs
    migration.write_text("ALTER TABLE notes ADD COLUMN pinned INTEGER;", encoding="utf-8-sig")
    original = source.read_bytes()
    first = invoke(inputs, "--format", "json")
    second = invoke(inputs, "--format", "json")
    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert report["status"] == "ok" and report["error"] is None
    assert report["changes"][0]["after"]["sql"].endswith("pinned INTEGER)")
    assert report["checks"] == {
        "foreign_key_violations": [], "integrity_errors": [], "truncated": False,
    }
    assert source.read_bytes() == original


def test_foreign_key_findings_return_report_and_status_one(inputs):
    source, migration = inputs
    with closing(sqlite3.connect(source)) as db:
        db.executescript("""
            CREATE TABLE accounts (id INTEGER PRIMARY KEY);
            CREATE TABLE tags (name TEXT PRIMARY KEY, account INTEGER REFERENCES accounts(id))
                WITHOUT ROWID;
            INSERT INTO tags VALUES ('fake', 42);
        """)
    migration.write_text("CREATE INDEX note_index ON notes(body);", encoding="utf-8")
    result = invoke(inputs, "--format", "json")
    assert result.returncode == 1 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["status"] == "check_failed" and report["error"] is None
    assert report["changes"][0]["after"]["name"] == "note_index"
    assert report["checks"]["foreign_key_violations"] == [
        {"table": "tags", "rowid": None, "parent": "accounts", "foreign_key": 0},
    ]


def test_migration_error_does_not_report_partial_diff(inputs):
    _, migration = inputs
    migration.write_text("DROP TABLE notes; SELECT * FROM absent;", encoding="utf-8")
    result = invoke(inputs, "--format", "json")
    assert result.returncode == 2 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "error", "changes": None, "checks": None,
        "error": {"stage": "migration", "message": "no such table: absent"},
    }


def test_input_file_validation_and_text_error(inputs):
    _, migration = inputs
    migration.write_bytes(b"\xff")
    result = invoke(inputs)
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr == 'error [input]: "migration must be UTF-8 text"\n'
    migration.write_bytes(b" " * (1024 * 1024 + 1))
    result = invoke(inputs, "--format", "json")
    assert result.returncode == 2 and result.stderr == ""
    assert json.loads(result.stdout)["error"]["message"] == "migration exceeds the SQL byte limit"


def test_text_preview_and_invalid_timeout(inputs):
    _, migration = inputs
    migration.write_text("SELECT 1;", encoding="utf-8")
    result = invoke(inputs)
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == "No table or index changes.\nChecks passed.\n"
    result = invoke(inputs, "--timeout", "nan")
    assert result.returncode == 2 and "finite and positive" in result.stderr


def test_documented_examples_work_against_same_source(tmp_path):
    examples = Path(__file__).resolve().parents[1] / "examples"
    database = tmp_path / "demo.sqlite"
    command = [sys.executable, str(examples / "create_demo.py"), str(database)]
    seeded = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert seeded.returncode == 0, seeded.stderr
    original = database.read_bytes()

    success = invoke((database, examples / "add-status.sql"))
    assert success.returncode == 0 and success.stderr == ""
    assert 'CHANGED table "notes"' in success.stdout
    assert 'ADDED index "notes_open_by_notebook"' in success.stdout

    failure = invoke((database, examples / "fail-unique.sql"), "--format", "json")
    assert failure.returncode == 2 and failure.stderr == ""
    assert "UNIQUE constraint failed" in json.loads(failure.stdout)["error"]["message"]

    repeated = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert repeated.returncode == 2
    assert database.read_bytes() == original
