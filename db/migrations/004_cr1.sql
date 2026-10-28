ALTER TABLE unmapped_observations
  ADD COLUMN IF NOT EXISTS confidence text NOT NULL DEFAULT 'unknown'
    CHECK (confidence IN ('high','medium','low','unknown')),
  ADD COLUMN IF NOT EXISTS resolved_project_id uuid REFERENCES projects(id),
  ADD COLUMN IF NOT EXISTS bound_at timestamptz;

CREATE INDEX IF NOT EXISTS unmapped_open_idx
  ON unmapped_observations(source, working_directory, observed_at DESC)
  WHERE resolved_project_id IS NULL;
