#!/bin/sh
set -eu
DBPASS=$(cat "${LIGHTHOUSE_DB_PASSWORD_FILE:-/run/secrets/db-password}")
export DATABASE_URL="postgresql://lighthouse_api:${DBPASS}@db:5432/lighthouse"
unset DBPASS
exec python -m lighthouse.admin "$@"
