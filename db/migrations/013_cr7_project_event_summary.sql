BEGIN;

LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE IF NOT EXISTS project_event_summary (
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  bucket_at timestamptz NOT NULL,
  source text NOT NULL,
  parent_thread_id uuid,
  last_observed_at timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS project_event_summary_bucket_key
  ON project_event_summary(project_id, bucket_at, source, parent_thread_id)
  NULLS NOT DISTINCT;
CREATE INDEX IF NOT EXISTS project_event_summary_recent_idx
  ON project_event_summary(project_id, last_observed_at DESC)
  INCLUDE (bucket_at, source, parent_thread_id);

CREATE OR REPLACE FUNCTION maintain_project_event_summary() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NEW.project_id IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.project_id::text, 0));
    INSERT INTO public.project_event_summary(project_id, bucket_at, source, parent_thread_id, last_observed_at)
    VALUES (
      NEW.project_id,
      to_timestamp(floor(extract(epoch FROM NEW.observed_at) / 900) * 900),
      NEW.source,
      NEW.parent_thread_id,
      NEW.observed_at
    )
    ON CONFLICT (project_id, bucket_at, source, parent_thread_id)
    DO UPDATE SET last_observed_at = GREATEST(project_event_summary.last_observed_at, EXCLUDED.last_observed_at);
  END IF;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION maintain_project_event_summary() FROM PUBLIC;
GRANT SELECT ON project_event_summary TO lighthouse_api;

DROP TRIGGER IF EXISTS events_maintain_project_event_summary ON events;
CREATE TRIGGER events_maintain_project_event_summary
AFTER INSERT ON events
FOR EACH ROW EXECUTE FUNCTION maintain_project_event_summary();

DELETE FROM project_event_summary;
INSERT INTO project_event_summary(project_id, bucket_at, source, parent_thread_id, last_observed_at)
SELECT project_id,
  to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900),
  source,
  parent_thread_id,
  max(observed_at)
FROM events
WHERE project_id IS NOT NULL
GROUP BY project_id, to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900), source, parent_thread_id;

COMMIT;
