ALTER TABLE project_briefs
  ALTER COLUMN eta_confidence DROP NOT NULL,
  DROP CONSTRAINT project_briefs_eta_confidence_check,
  ALTER COLUMN eta_basis DROP NOT NULL,
  ADD COLUMN schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version IN (1, 2));
