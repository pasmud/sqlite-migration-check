# SQLite Migration Check

Preview a trusted SQLite migration against a local development database. The tool opens the
source with `mode=ro`, copies a consistent read snapshot into memory using SQLite's online
backup API, applies the script to the copy, and checks the result. It never applies the
migration to the source or saves the scratch database.

## Try it with synthetic data

Requires Python 3.11+ linked against SQLite 3.37+ and no runtime packages outside the standard
library. From the checkout, run:

```sh
python examples/create_demo.py demo.sqlite
python -m sqlite_migration_check demo.sqlite examples/add-status.sql
python -m sqlite_migration_check demo.sqlite examples/fail-unique.sql --format json
```

The first command creates a new fake database and refuses to overwrite an existing path.
The first preview reports a changed `notes` table and an added `notes_open_by_notebook`
index, then exits `0`. The second preview exits `2` with a migration error because two
fake notes share the same notebook. Both previews start from the same unmodified database.

## Inputs and reports

```text
sqlite-migration-check DATABASE MIGRATION.sql [--format {text,json}] [--timeout SECONDS]
```

- `DATABASE` must be an existing local regular file. `MIGRATION.sql` must be an explicitly
  supplied local regular file containing non-empty UTF-8 SQL (an optional BOM is accepted).
  Use scripts you wrote or reviewed. Scripts may use transactions or savepoints but must
  finish with no transaction open. Foreign-key enforcement is on during the script.
- The diff compares the **final** main-schema tables and indexes by name, with tables
  before indexes and names sorted within each group. It shows SQLite's stored DDL and, for
  indexes, key terms, collation, ordering, uniqueness, origin, and whether the index is
  partial. For an implicit index, `sql` is `null`; `origin` is SQLite's `c` (explicit index),
  `u` (unique constraint), or `pk` (primary key). A rename appears as a removal and addition;
  a changed DDL string is reported even if it has the same meaning.
- Row changes, views, triggers, SQLite internal tables, and virtual-table shadow tables are
  not diffed. The tool still executes data changes and runs `integrity_check` and
  `foreign_key_check` on the final copy. No schema changes does not imply no data changes.

| Exit | JSON `status` | Meaning |
| --- | --- | --- |
| `0` | `ok` | The script completed and both checks passed. |
| `1` | `check_failed` | The script completed, but at least one integrity or foreign-key finding was reported. The diff is included. |
| `2` | `error` | The preview stopped at an input, source, backup, snapshot, migration, or check error. No partial diff is reported. |

Text mode puts completed reports on stdout and errors on stderr. JSON mode writes one object
to stdout with `status`, `changes`, `checks`, and `error`. Each change has `action` and
nullable `before`/`after` objects. Completed reports use `error: null`; failed previews use
`changes: null`, `checks: null`, and an error object with `stage` and `message`. A foreign-key
finding identifies the child table, rowid (`null` for `WITHOUT ROWID`), parent table, and
zero-based foreign-key ID. Argument syntax errors are emitted by the CLI parser to stderr
with exit `2`, even when JSON was requested. SQLite diagnostics may differ across SQLite versions.

The same preview is available from Python:

```python
from sqlite_migration_check import PreviewError, PreviewLimits, preview

try:
    result = preview(
        "demo.sqlite",
        "CREATE INDEX demo_notes_by_notebook ON notes(notebook_id);",
        limits=PreviewLimits(timeout_seconds=3),
    )
except PreviewError as error:
    print(error.stage, error)
else:
    print(result.ok, result.changes)
```

## Limits and compatibility

- Defaults: 1 MiB of SQL, 64 MiB of database pages for both the source snapshot and scratch
  growth, 16 MiB SQLite value/row limit, 100 findings per check, and a 10-second preview
  deadline. The report marks omitted findings with `checks.truncated`. Python callers can
  adjust the script, page and finding limits through `PreviewLimits`; CLI callers can adjust
  the deadline with `--timeout`. The deadline is cooperative, and the page limit does not
  cap total process memory (for example, sorting and query workspace also consume memory).
- Attachment, `VACUUM`/`VACUUM INTO`, temporary schema objects, virtual-table creation/deletion,
  and extension loading are blocked during the script. Only the read-only `quick_check`
  pragma is permitted during migration because SQLite uses it to validate some `ALTER TABLE`s.
  The scratch connection keeps temporary storage in memory and uses `trusted_schema=OFF`.
  Application-specific extensions, functions, and collations are not loaded. This is a
  local preview for trusted files, not a sandbox or an exact model of every app connection.
- WAL-mode sources require existing `-wal` and `-shm` sidecars so SQLite does not normally
  create them when opening the source. SQLite may update shared-memory coordination data.
  Keep the app connection open and avoid changing journal mode or replacing the source while
  previewing. For a closed WAL-mode database without sidecars, use a standalone local snapshot
  exported by the app. If a rollback-mode database is locked, retry when the writer is idle.
- Reports can contain schema definitions and diagnostic names. Use fake development data
  when sharing reports. A passing preview checks only that particular snapshot and script.
