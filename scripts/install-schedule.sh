#!/bin/bash
# Install (or reinstall) the macOS LaunchAgent that runs `delta update` every 15 minutes.
# Usage: scripts/install-schedule.sh [interval_seconds]     Uninstall: scripts/install-schedule.sh --remove
set -euo pipefail

LABEL="local.delta-india.candles"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "--remove" ]]; then
  rm -f "$PLIST"
  echo "removed $LABEL"
  exit 0
fi

INTERVAL="${1:-900}"
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
[[ -x "$ROOT/.venv/bin/delta" ]] || { echo "missing $ROOT/.venv/bin/delta — run setup first" >&2; exit 1; }

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/delta</string>
    <string>update</string>
    <string>--quiet-ok</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/logs/update.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/update.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "installed $LABEL: every ${INTERVAL}s, log $ROOT/data/logs/update.log"
