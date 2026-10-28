CREATE TABLE IF NOT EXISTS ignored_observation_groups (
  source text NOT NULL,
  match_kind text NOT NULL CHECK (match_kind IN ('working_directory','external_thread_id')),
  match_value text NOT NULL,
  ignored_by uuid NOT NULL REFERENCES principals(id),
  ignored_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source,match_kind,match_value)
);
GRANT SELECT,INSERT,UPDATE,DELETE ON ignored_observation_groups TO lighthouse_api;

CREATE OR REPLACE FUNCTION enforce_event_thread_project() RETURNS trigger AS $$
BEGIN
  IF NEW.parent_thread_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM parent_threads WHERE id=NEW.parent_thread_id AND project_id=NEW.project_id
  ) THEN
    RAISE EXCEPTION 'event project_id must match parent thread project_id' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_thread_project_consistency ON events;
CREATE TRIGGER events_thread_project_consistency
  BEFORE INSERT OR UPDATE OF project_id,parent_thread_id ON events
  FOR EACH ROW EXECUTE FUNCTION enforce_event_thread_project();
