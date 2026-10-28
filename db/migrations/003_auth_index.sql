ALTER TABLE principals ADD COLUMN IF NOT EXISTS token_lookup_hash text;
CREATE UNIQUE INDEX IF NOT EXISTS principals_token_lookup_hash_uidx
  ON principals(token_lookup_hash) WHERE token_lookup_hash IS NOT NULL;
