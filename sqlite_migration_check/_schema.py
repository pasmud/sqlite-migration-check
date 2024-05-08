"""Metadata snapshots of main-schema objects."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class IndexTerm:
    column: str | None
    expression: bool
    descending: bool
    collation: str


@dataclass(frozen=True)
class IndexDetails:
    unique: bool
    origin: str
    partial: bool
    terms: tuple[IndexTerm, ...]


@dataclass(frozen=True)
class SchemaObject:
    kind: str
    name: str
    table: str
    sql: str | None
    index: IndexDetails | None = None


@dataclass(frozen=True)
class SchemaChange:
    action: str
    before: SchemaObject | None
    after: SchemaObject | None


Snapshot = dict[tuple[str, str], SchemaObject]
_KIND_ORDER = {kind: order for order, kind in enumerate(("table", "index", "view", "trigger"))}


def snapshot(db: sqlite3.Connection, *, include_views_triggers: bool = False) -> Snapshot:
    definitions = dict(db.execute("SELECT name, sql FROM main.sqlite_schema WHERE type = 'index'"))
    objects = {}
    for table, sql in db.execute("""
        SELECT s.name, s.sql
        FROM main.sqlite_schema AS s JOIN pragma_table_list AS t ON s.name = t.name
        WHERE s.type = 'table' AND t.schema = 'main' AND t.type IN ('table', 'virtual')
            AND substr(s.name, 1, 7) <> 'sqlite_'
    """):
        objects["table", table] = SchemaObject("table", table, table, sql)
        for name, unique, origin, partial in db.execute("""
            SELECT name, "unique", origin, partial FROM pragma_index_list(?, 'main')
        """, (table,)):
            terms = tuple(
                IndexTerm(column, cid == -2, bool(desc), coll)
                for cid, column, desc, coll in db.execute("""
                    SELECT cid, name, "desc", coll FROM pragma_index_xinfo(?, 'main')
                    WHERE key = 1 ORDER BY seqno
                """, (name,))
            )
            details = IndexDetails(bool(unique), origin, bool(partial), terms)
            objects["index", name] = SchemaObject("index", name, table, definitions.get(name), details)
    if include_views_triggers:
        for kind, name, table, sql in db.execute("""
            SELECT type, name, tbl_name, sql FROM main.sqlite_schema
            WHERE type IN ('view', 'trigger') AND substr(name, 1, 7) <> 'sqlite_'
        """):
            objects[kind, name] = SchemaObject(kind, name, table, sql)
    return objects


def diff(before: Snapshot, after: Snapshot) -> tuple[SchemaChange, ...]:
    changes = []
    for key in sorted(before.keys() | after.keys(), key=lambda key: (_KIND_ORDER[key[0]], key[1])):
        old, new = before.get(key), after.get(key)
        if old != new:
            action = "added" if old is None else "removed" if new is None else "changed"
            changes.append(SchemaChange(action, old, new))
    return tuple(changes)
