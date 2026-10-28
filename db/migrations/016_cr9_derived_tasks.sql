ALTER TABLE tasks ADD COLUMN derived_source text;
ALTER TABLE tasks ADD COLUMN derived_external_id text;
CREATE UNIQUE INDEX tasks_derived_source_external_id_uq
  ON tasks(derived_source, derived_external_id)
  WHERE derived_source IS NOT NULL AND derived_external_id IS NOT NULL;
