"""Preview trusted SQLite migrations without applying them to the source."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


class PreviewError(Exception):
    """The preview could not complete the named stage."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class ForeignKeyViolation:
    table: str
    rowid: int | None
    parent: str
    foreign_key: int


@dataclass(frozen=True)
class PreviewResult:
    integrity_errors: tuple[str, ...]
    foreign_key_violations: tuple[ForeignKeyViolation, ...]

    @property
    def ok(self) -> bool:
        return not (self.integrity_errors or self.foreign_key_violations)


def preview(source: str | Path, sql: str) -> PreviewResult:
    """Back up an existing SQLite file, apply SQL to the copy, and check it."""
    stage = "source"
    try:
        path = Path(source).resolve(strict=True)
        if not path.is_file():
            raise PreviewError(stage, "source must be a regular file")

        with closing(sqlite3.connect(":memory:")) as scratch:
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as original:
                stage = "backup"
                original.backup(scratch)

            stage = "migration"
            scratch.execute("PRAGMA foreign_keys = ON")
            scratch.executescript(sql)
            if scratch.in_transaction:
                raise PreviewError(stage, "migration left a transaction open")

            stage = "checks"
            integrity = tuple(
                message for (message,) in scratch.execute("PRAGMA main.integrity_check")
                if message != "ok"
            )
            violations = tuple(
                ForeignKeyViolation(*row)
                for row in scratch.execute("PRAGMA main.foreign_key_check")
            )
            return PreviewResult(integrity, violations)
    except (OSError, sqlite3.Error) as exc:
        raise PreviewError(stage, str(exc)) from exc
