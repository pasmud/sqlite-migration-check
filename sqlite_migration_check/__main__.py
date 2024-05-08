"""Command-line reports for SQLite migration previews."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import PreviewError, PreviewLimits, PreviewResult, preview
from ._schema import SchemaObject


def _read_sql(path: Path, limit: int) -> str:
    try:
        if not path.is_file():
            raise PreviewError("input", "migration must be a readable regular file")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise PreviewError("input", "migration exceeds the SQL byte limit")
        return data.decode("utf-8-sig")
    except OSError as exc:
        raise PreviewError("input", str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise PreviewError("input", "migration must be UTF-8 text") from exc


def _describe(obj: SchemaObject) -> str:
    if obj.index is None:
        return json.dumps(obj.sql)
    return json.dumps({"sql": obj.sql, **asdict(obj.index)}, sort_keys=True)


def _print_text(result: PreviewResult, *, include_views_triggers: bool) -> None:
    if not result.changes:
        scope = "table, index, view, or trigger" if include_views_triggers else "table or index"
        print(f"No {scope} changes.")
    for change in result.changes:
        obj = change.after or change.before
        assert obj is not None
        parent = f" on {json.dumps(obj.table)}" if obj.kind in ("index", "trigger") else ""
        print(f"{change.action.upper()} {obj.kind} {json.dumps(obj.name)}{parent}")
        if change.before is not None:
            print(f"  before: {_describe(change.before)}")
        if change.after is not None:
            print(f"  after:  {_describe(change.after)}")
    print("Checks passed." if result.ok else "Checks failed.")
    for message in result.integrity_errors:
        print(f"  integrity: {json.dumps(message)}")
    for violation in result.foreign_key_violations:
        print(f"  foreign key: {json.dumps(asdict(violation), sort_keys=True)}")
    if result.checks_truncated:
        print("  Additional check findings omitted.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqlite-migration-check",
        description="Preview a trusted SQL migration against a disposable SQLite backup.",
    )
    parser.add_argument("source", type=Path, metavar="DATABASE", help="existing SQLite database file")
    parser.add_argument("migration", type=Path, metavar="MIGRATION.sql", help="trusted UTF-8 script")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="report format")
    parser.add_argument("--timeout", type=float, default=PreviewLimits().timeout_seconds,
                         metavar="SECONDS", help="preview time limit (default: %(default)s)")
    parser.add_argument("--include-views-triggers", action="store_true",
                         help="also compare main-schema views and triggers")
    args = parser.parse_args(argv)

    try:
        try:
            limits = PreviewLimits(timeout_seconds=args.timeout)
        except ValueError as exc:
            raise PreviewError("input", str(exc)) from exc
        result = preview(
            args.source, _read_sql(args.migration, limits.max_sql_bytes), limits=limits,
            include_views_triggers=args.include_views_triggers,
        )
    except PreviewError as exc:
        if args.format == "json":
            print(json.dumps({
                "status": "error", "changes": None, "checks": None,
                "error": {"stage": exc.stage, "message": str(exc)},
            }, sort_keys=True))
        else:
            print(f"error [{exc.stage}]: {json.dumps(str(exc))}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps({
            "status": "ok" if result.ok else "check_failed",
            "changes": [asdict(change) for change in result.changes],
            "checks": {
                "integrity_errors": result.integrity_errors,
                "foreign_key_violations": [asdict(item) for item in result.foreign_key_violations],
                "truncated": result.checks_truncated,
            },
            "error": None,
        }, sort_keys=True))
    else:
        _print_text(result, include_views_triggers=args.include_views_triggers)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
