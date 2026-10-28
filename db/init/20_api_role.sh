#!/bin/sh
set -eu
export LIGHTHOUSE_API_PASSWORD=$POSTGRES_PASSWORD
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /migrations/002_roles.sql
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /migrations/003_auth_index.sql
