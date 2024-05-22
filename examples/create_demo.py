"""Create a fresh SQLite file with synthetic notebooks and notes."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    try:
        args.database.touch(exist_ok=False)
    except OSError as exc:
        parser.error(str(exc))
    with closing(sqlite3.connect(args.database)) as db:
        db.executescript("""
            PRAGMA foreign_keys = ON;
            BEGIN;
            CREATE TABLE notebooks (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY,
                notebook_id INTEGER NOT NULL REFERENCES notebooks(id),
                body TEXT NOT NULL
            );
            INSERT INTO notebooks VALUES (1, 'Demo notebook');
            INSERT INTO notes VALUES (1, 1, 'Fake draft'), (2, 1, 'Fake checklist');
            COMMIT;
        """)


if __name__ == "__main__":
    main()
