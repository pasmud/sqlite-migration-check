import json
import sqlite3
from contextlib import closing
from dataclasses import astuple
from pathlib import Path

import pytest

from sqlite_migration_check import PreviewError, PreviewLimits, preview


def load_vectors():
    titles = set()
    for path in sorted(Path(__file__).with_name("regression_vectors").glob("*.json")):
        vector = json.loads(path.read_text(encoding="utf-8"))
        title = vector["title"]
        assert 0 < len(title) < 72 and title.isprintable(), path.name
        assert title not in titles, path.name
        titles.add(title)
        yield pytest.param(vector, id=path.stem)


def assert_object(obj, kind, name, expected):
    assert (obj.kind, obj.name) == (kind, name)
    if kind in ("table", "view"):
        assert obj.table == name
    if "table" in expected:
        assert obj.table == expected["table"]
    if "sql" in expected:
        assert obj.sql == expected["sql"]
    if "sql_contains" in expected:
        assert expected["sql_contains"] in obj.sql
    if kind != "index":
        assert obj.index is None
    elif "index" in expected:
        assert {
            "unique": obj.index.unique,
            "origin": obj.index.origin,
            "partial": obj.index.partial,
            # Each term is [column, expression, descending, collation].
            "terms": [list(astuple(term)) for term in obj.index.terms],
        } == expected["index"]


@pytest.mark.parametrize("vector", list(load_vectors()))
def test_regression_vector(tmp_path, vector):
    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)) as db:
        # Model older writers that left orphans or used invalid parent keys.
        fk_mode = "OFF" if vector.get("legacy_source") else "ON"
        db.execute(f"PRAGMA foreign_keys = {fk_mode}")
        db.executescript("\n".join(vector["setup"]))
    original = source.read_bytes()

    sql = "\n".join(vector["migration"])
    options = {
        "include_views_triggers": vector.get("include_views_triggers", False),
        "limits": PreviewLimits(**vector.get("limits", {})),
    }
    expected = vector["expect"]
    try:
        if "error" in expected:
            stage, message = expected["error"]
            with pytest.raises(PreviewError) as caught:
                preview(source, sql, **options)
            assert caught.value.stage == stage
            assert message in str(caught.value)
            return

        result = preview(source, sql, **options)
        assert [
            (change.action, obj.kind, obj.name)
            for change in result.changes
            for obj in [change.after or change.before]
        ] == [tuple(spec[:3]) for spec in expected["changes"]]
        for change, (action, kind, name, details) in zip(
            result.changes, expected["changes"], strict=True
        ):
            if action == "added":
                assert change.before is None
            else:
                assert_object(change.before, kind, name, details.get("before", {}))
            if action == "removed":
                assert change.after is None
            else:
                assert_object(change.after, kind, name, details.get("after", {}))

        integrity = expected.get("integrity", [])
        foreign_keys = expected.get("foreign_keys", [])
        assert list(result.integrity_errors) == integrity
        violations = [list(astuple(item)) for item in result.foreign_key_violations]
        assert violations == foreign_keys
        assert result.checks_truncated is expected.get("truncated", False)
        assert result.ok is (not (integrity or foreign_keys))
    finally:
        assert source.read_bytes() == original
        assert list(tmp_path.iterdir()) == [source]
