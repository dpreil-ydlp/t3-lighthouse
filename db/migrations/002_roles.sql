\getenv api_password LIGHTHOUSE_API_PASSWORD
SELECT 'CREATE ROLE lighthouse_api LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lighthouse_api') \gexec
ALTER ROLE lighthouse_api WITH LOGIN PASSWORD :'api_password';
GRANT CONNECT ON DATABASE lighthouse TO lighthouse_api;
GRANT USAGE ON SCHEMA public TO lighthouse_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lighthouse_api;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lighthouse_api;
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON events FROM lighthouse_api;
