CREATE TABLE project_briefs (
  project_id uuid PRIMARY KEY REFERENCES projects(id),
  about text NOT NULL,
  now text NOT NULL,
  done jsonb NOT NULL CHECK (jsonb_typeof(done) = 'array'),
  "left" jsonb NOT NULL CHECK (jsonb_typeof("left") = 'array'),
  remaining_hours numeric CHECK (remaining_hours IS NULL OR remaining_hours BETWEEN 0 AND 2000),
  eta_confidence text NOT NULL CHECK (eta_confidence IN ('low','medium','high')),
  eta_basis text NOT NULL,
  activity_through timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL CHECK (version > 0)
);

CREATE TABLE project_brief_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id),
  version integer NOT NULL,
  body jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, version)
);

GRANT SELECT, INSERT, UPDATE ON project_briefs TO lighthouse_api;
GRANT SELECT, INSERT ON project_brief_history TO lighthouse_api;
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON project_brief_history FROM lighthouse_api;
