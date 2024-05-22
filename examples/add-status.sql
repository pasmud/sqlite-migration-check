BEGIN;
ALTER TABLE notes ADD COLUMN status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'done'));
UPDATE notes SET status = 'done' WHERE body = 'Fake checklist';
CREATE INDEX notes_open_by_notebook ON notes(notebook_id) WHERE status = 'open';
COMMIT;
