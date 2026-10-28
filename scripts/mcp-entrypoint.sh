#!/bin/sh
set -eu
for i in $(seq 1 600); do
  [ -s /run/secrets/agent-token ] && break
  sleep 1
done
[ -s /run/secrets/agent-token ] || { echo 'MCP agent credential was not provisioned' >&2; exit 1; }
exec python -m lighthouse.mcp_server
