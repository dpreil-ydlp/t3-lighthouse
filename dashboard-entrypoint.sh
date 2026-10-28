#!/bin/sh
set -eu
key_file="${DASHBOARD_SESSION_KEY_FILE:-/host-secrets/dashboard-session-key}"
[ -s "$key_file" ] || { echo 'Dashboard session key is missing' >&2; exit 1; }
export DASHBOARD_SESSION_KEY="$(cat "$key_file")"
[ "${#DASHBOARD_SESSION_KEY}" -ge 32 ] || { echo 'Dashboard session key must contain at least 32 bytes' >&2; exit 1; }
exec node server.js
