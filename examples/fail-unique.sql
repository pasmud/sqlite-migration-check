-- The demo has two notes in the same notebook, so this proposed constraint fails.
BEGIN;
CREATE UNIQUE INDEX one_note_per_notebook ON notes(notebook_id);
COMMIT;
