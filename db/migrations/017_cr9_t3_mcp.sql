CREATE TABLE IF NOT EXISTS t3_mcp_connection (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  token_expires_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON t3_mcp_connection TO lighthouse_api;
