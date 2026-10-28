BEGIN;

LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE;

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

DROP TRIGGER IF EXISTS events_maintain_project_event_summary ON events;
CREATE TRIGGER events_maintain_project_event_summary
AFTER INSERT ON events
FOR EACH ROW EXECUTE FUNCTION maintain_project_event_summary();

REVOKE ALL ON FUNCTION maintain_project_event_summary() FROM PUBLIC;
GRANT SELECT ON project_event_summary TO lighthouse_api;

COMMIT;
