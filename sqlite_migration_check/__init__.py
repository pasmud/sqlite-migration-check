"""Preview trusted SQLite migrations without applying them to the source."""

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from time import monotonic

from ._schema import SchemaChange, diff, snapshot


class PreviewError(Exception):
    """The preview could not complete the named stage."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class PreviewLimits:
    """Bounds for the input script, scratch database, checks and elapsed time."""

    max_sql_bytes: int = 1024 * 1024
    max_db_bytes: int = 64 * 1024 * 1024
    timeout_seconds: float = 10.0
    max_check_rows: int = 100

    def __post_init__(self) -> None:
        for name in ("max_sql_bytes", "max_db_bytes", "max_check_rows"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not 0 < self.timeout_seconds < math.inf:
            raise ValueError("timeout_seconds must be finite and positive")
        if self.max_check_rows >= 2**31 - 1:
            raise ValueError("max_check_rows must be less than 2147483647")


@dataclass(frozen=True)
class ForeignKeyViolation:
    table: str
    rowid: int | None
    parent: str
    foreign_key: int


@dataclass(frozen=True)
class PreviewResult:
    changes: tuple[SchemaChange, ...]
    integrity_errors: tuple[str, ...]
    foreign_key_violations: tuple[ForeignKeyViolation, ...]
    checks_truncated: bool

    @property
    def ok(self) -> bool:
        return not (self.integrity_errors or self.foreign_key_violations)


_BLOCKED_ACTIONS = {
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_VTABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
}


def _authorize(
    action: int, arg1: str | None, arg2: str | None, schema: str | None, context: str | None
) -> int:
    # ALTER TABLE ADD COLUMN uses this read-only pragma to validate new CHECK constraints.
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() != "quick_check":
        return sqlite3.SQLITE_DENY
    if action in _BLOCKED_ACTIONS or (
        action == sqlite3.SQLITE_FUNCTION and arg2 == "load_extension"
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _check_source(path: Path) -> None:
    if not path.is_file():
        raise PreviewError("source", "source must be a regular file")
    with path.open("rb") as stream:
        header = stream.read(20)
    # A read-only WAL connection may otherwise create missing sidecar files.
    if (
        header.startswith(b"SQLite format 3\x00")
        and 2 in header[18:20]
        and not all(path.with_name(path.name + suffix).is_file() for suffix in ("-wal", "-shm"))
    ):
        raise PreviewError("source", "WAL mode requires existing -wal and -shm sidecars")


def preview(
    source: str | Path,
    sql: str,
    *,
    limits: PreviewLimits = PreviewLimits(),
    include_views_triggers: bool = False,
) -> PreviewResult:
    """Back up an existing SQLite file, apply SQL to the copy, and check it."""
    stage = "input"
    deadline = monotonic() + limits.timeout_seconds

    def expired() -> bool:
        return monotonic() >= deadline

    def check_deadline() -> None:
        if expired():
            raise PreviewError(stage, "preview time limit exceeded")

    def backup_progress(status: int, remaining: int, total: int) -> None:
        check_deadline()

    try:
        if sqlite3.sqlite_version_info < (3, 37, 0):
            raise PreviewError(stage, "SQLite 3.37 or newer is required")
        if not isinstance(sql, str) or not sql.strip() or "\x00" in sql:
            raise PreviewError(stage, "migration must be non-empty text without NUL bytes")
        if len(sql) > limits.max_sql_bytes or len(sql.encode("utf-8")) > limits.max_sql_bytes:
            raise PreviewError(stage, "migration exceeds the SQL byte limit")

        stage = "source"
        path = Path(source).resolve(strict=True)
        _check_source(path)
        lock_timeout = min(0.1, limits.timeout_seconds)
        with closing(sqlite3.connect(":memory:", cached_statements=0)) as scratch:
            scratch.set_progress_handler(expired, 1000)
            with closing(sqlite3.connect(
                f"{path.as_uri()}?mode=ro", uri=True, timeout=lock_timeout
            )) as original:
                stage = "backup"
                original.set_progress_handler(expired, 1000)
                # Pin page count and backup to the same read snapshot.
                original.execute("BEGIN")
                page_size, = original.execute("PRAGMA main.page_size").fetchone()
                page_count, = original.execute("PRAGMA main.page_count").fetchone()
                max_pages = limits.max_db_bytes // page_size
                if max_pages < 1 or page_count > max_pages:
                    raise PreviewError(stage, "source exceeds the scratch database byte limit")
                check_deadline()
                original.backup(scratch, pages=64, progress=backup_progress, sleep=0.02)

            stage = "snapshot_before"
            check_deadline()
            scratch.execute(f"PRAGMA main.max_page_count = {min(max_pages, 4294967294)}")
            scratch.execute("PRAGMA temp_store = MEMORY")
            scratch.execute("PRAGMA trusted_schema = OFF")
            scratch.execute("PRAGMA foreign_keys = ON")
            scratch.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            scratch.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
            before = snapshot(scratch, include_views_triggers=include_views_triggers)
            check_deadline()

            stage = "migration"
            scratch.set_authorizer(_authorize)
            try:
                scratch.executescript(sql)
            finally:
                scratch.set_authorizer(None)
            check_deadline()
            if scratch.in_transaction:
                raise PreviewError(stage, "migration left a transaction open")

            stage = "snapshot_after"
            changes = diff(before, snapshot(scratch, include_views_triggers=include_views_triggers))
            check_deadline()

            stage = "checks"
            cap = limits.max_check_rows
            integrity = tuple(
                message for (message,) in scratch.execute(f"PRAGMA main.integrity_check({cap + 1})")
                if message != "ok"
            )
            violations = tuple(
                ForeignKeyViolation(*row)
                for row in islice(scratch.execute("PRAGMA main.foreign_key_check"), cap + 1)
            )
            check_deadline()
            return PreviewResult(
                changes,
                tuple(sorted(integrity[:cap])),
                tuple(sorted(violations[:cap], key=_foreign_key_order)),
                len(integrity) > cap or len(violations) > cap,
            )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        if expired():
            raise PreviewError(stage, "preview time limit exceeded") from exc
        raise PreviewError(stage, str(exc)) from exc


def _foreign_key_order(violation: ForeignKeyViolation) -> tuple:
    rowid = violation.rowid
    return violation.table, rowid is not None, rowid, violation.parent, violation.foreign_key
