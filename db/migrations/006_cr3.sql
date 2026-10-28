ALTER TABLE projects ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'manual';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS hidden boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS project_roots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  path text NOT NULL UNIQUE,
  display_path text NOT NULL,
  display_name text NOT NULL,
  origin text NOT NULL CHECK (origin IN ('t3','folder')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS project_roots_project_idx ON project_roots(project_id);
CREATE INDEX IF NOT EXISTS unmapped_cr3_open_time_idx
  ON unmapped_observations(observed_at DESC) WHERE resolved_project_id IS NULL;
CREATE INDEX IF NOT EXISTS events_thread_time_idx ON events(parent_thread_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS events_source_time_idx ON events(project_id, source, observed_at DESC);
CREATE INDEX IF NOT EXISTS incidents_project_open_idx
  ON incidents(project_id) WHERE state NOT IN ('resolved','closed');

CREATE TABLE IF NOT EXISTS thread_titles (
  source text NOT NULL,
  external_thread_id text NOT NULL,
  title text NOT NULL CHECK (length(title) <= 120),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, external_thread_id)
);

GRANT SELECT,INSERT,UPDATE,DELETE ON project_roots,thread_titles TO lighthouse_api;

CREATE OR REPLACE FUNCTION lighthouse_merge_project_events(source_id uuid, target_id uuid)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE moved bigint;
BEGIN
  IF source_id = target_id OR NOT EXISTS (SELECT 1 FROM projects WHERE id=source_id)
     OR NOT EXISTS (SELECT 1 FROM projects WHERE id=target_id) THEN
    RAISE EXCEPTION 'invalid project merge';
  END IF;
  UPDATE events SET project_id=target_id WHERE project_id=source_id;
  GET DIAGNOSTICS moved = ROW_COUNT;
  RETURN moved;
END;
$$;
REVOKE ALL ON FUNCTION lighthouse_merge_project_events(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lighthouse_merge_project_events(uuid,uuid) TO lighthouse_api;
