ALTER TABLE thread_titles
  ADD COLUMN IF NOT EXISTS t3_thread_id text
    CHECK (t3_thread_id IS NULL OR t3_thread_id ~ '^[A-Za-z0-9:._%-]{1,200}$'),
  ADD COLUMN IF NOT EXISTS t3_subagent boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS t3_discovery_state (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  environment_id text NOT NULL CHECK (environment_id ~ '^[A-Za-z0-9:._%-]{1,200}$'),
  updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT,INSERT,UPDATE ON t3_discovery_state TO lighthouse_api;
