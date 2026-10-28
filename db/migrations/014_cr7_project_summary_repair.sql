CREATE OR REPLACE FUNCTION refresh_project_summary(p_project_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(p_project_id::text, 0));
  DELETE FROM public.project_event_summary WHERE project_id = p_project_id;
  INSERT INTO public.project_event_summary(project_id, bucket_at, source, parent_thread_id, last_observed_at)
  SELECT project_id,
    to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900),
    source,
    parent_thread_id,
    max(observed_at)
  FROM public.events
  WHERE project_id = p_project_id
  GROUP BY project_id, to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900), source, parent_thread_id;
END;
$$;

REVOKE ALL ON FUNCTION refresh_project_summary(uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION refresh_all_project_summaries()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE project_row record;
BEGIN
  FOR project_row IN SELECT id FROM public.projects ORDER BY id LOOP
    PERFORM public.refresh_project_summary(project_row.id);
  END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION refresh_all_project_summaries() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_all_project_summaries() TO lighthouse_api;

CREATE OR REPLACE FUNCTION lighthouse_merge_project_events(source_id uuid, target_id uuid)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE moved bigint;
BEGIN
  IF source_id = target_id OR NOT EXISTS (SELECT 1 FROM projects WHERE id=source_id)
     OR NOT EXISTS (SELECT 1 FROM projects WHERE id=target_id) THEN
    RAISE EXCEPTION 'invalid project merge';
  END IF;
  UPDATE events SET project_id=target_id WHERE project_id=source_id;
  GET DIAGNOSTICS moved = ROW_COUNT;
  PERFORM refresh_project_summary(source_id);
  PERFORM refresh_project_summary(target_id);
  RETURN moved;
END;
$$;

REVOKE ALL ON FUNCTION lighthouse_merge_project_events(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lighthouse_merge_project_events(uuid,uuid) TO lighthouse_api;
