#!/bin/sh
set -eu
# Run inside the API container. The pytest session creates and drops lighthouse_test.
export LIGHTHOUSE_TEST_DB_PASSWORD_FILE="${LIGHTHOUSE_DB_PASSWORD_FILE:-/run/secrets/db-password}"
export LIGHTHOUSE_LIVE_DATABASE_NAME=lighthouse
exec pytest -q "$@"
