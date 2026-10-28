ALTER TABLE thread_titles
  ADD COLUMN IF NOT EXISTS title_origin text NOT NULL DEFAULT 'native'
  CHECK (title_origin IN ('t3','native'));

DELETE FROM thread_titles
WHERE lower(btrim(title)) IN ('new thread','untitled','untitled session');
