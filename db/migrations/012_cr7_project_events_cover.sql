DROP INDEX IF EXISTS events_project_time_idx;
CREATE INDEX events_project_time_idx
  ON events(project_id, observed_at DESC)
  INCLUDE (source, parent_thread_id);
