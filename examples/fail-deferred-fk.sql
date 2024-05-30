BEGIN;

CREATE TABLE note_reminders (
    note_id INTEGER NOT NULL REFERENCES notes(id) DEFERRABLE INITIALLY DEFERRED,
    label TEXT NOT NULL
);

-- The synthetic database has notes 1 and 2, so note 999 is missing.
INSERT INTO note_reminders VALUES (999, 'Fake reminder');

-- SQLite allows the insert in this transaction but rejects the commit.
COMMIT;
