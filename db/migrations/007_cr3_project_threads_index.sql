CREATE INDEX IF NOT EXISTS parent_threads_project_idx
  ON parent_threads(project_id,id);
