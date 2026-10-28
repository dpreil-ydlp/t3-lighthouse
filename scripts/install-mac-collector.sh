#!/bin/sh
set -eu
HOST=
PORT="${LIGHTHOUSE_API_PORT:-51821}"
INTERVAL=2
TOKEN_FILE="${HOME}/.config/lighthouse/secrets/agent-token"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host) HOST=$2; shift 2;;
    --port) PORT=$2; shift 2;;
    --interval) INTERVAL=$2; shift 2;;
    --token-file) TOKEN_FILE=$2; shift 2;;
    *) echo "unknown argument" >&2; exit 2;;
  esac
done
[ -n "$HOST" ] || { echo "host is required" >&2; exit 2; }
[ -f "$TOKEN_FILE" ] || { echo "collector token file is missing" >&2; exit 1; }
[ "$(stat -f '%Lp' "$TOKEN_FILE")" = 600 ] || { echo "collector token file must be mode 0600" >&2; exit 1; }
[ "$(stat -f '%Lp' "$(dirname "$TOKEN_FILE")")" = 700 ] || { echo "collector secret directory must be mode 0700" >&2; exit 1; }
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
PYTHON=$(command -v python3) || { echo "python3 is required" >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "the collector needs Python 3.11 or later; $PYTHON is older" >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.config/lighthouse"
chmod 700 "$HOME/.config/lighthouse"
PLIST="$HOME/Library/LaunchAgents/ai.lighthouse.collector.plist"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>ai.lighthouse.collector</string>
<key>ProgramArguments</key><array><string>$PYTHON</string><string>$ROOT/collector/collector.py</string><string>--host</string><string>http://$HOST:$PORT</string><string>--interval</string><string>$INTERVAL</string><string>--token-file</string><string>$TOKEN_FILE</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$HOME/.config/lighthouse/collector.log</string>
<key>StandardErrorPath</key><string>$HOME/.config/lighthouse/collector.log</string>
</dict></plist>
PLIST_EOF
chmod 600 "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl kickstart -k "gui/$(id -u)/ai.lighthouse.collector"
echo "collector LaunchAgent installed; sources=claude_code,codex,chatgpt_desktop,codex_desktop,claude_desktop; T3 Code metadata when installed"
