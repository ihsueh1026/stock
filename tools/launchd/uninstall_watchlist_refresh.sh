#!/usr/bin/env zsh
# Uninstall the morning watchlist pre-fetch launchd schedule.

set -euo pipefail

LABEL="com.user.claude-watchlist-refresh"
INSTALL_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$INSTALL_PATH" ]]; then
  launchctl unload "$INSTALL_PATH" 2>/dev/null || true
  rm "$INSTALL_PATH"
  print "✓ Uninstalled $LABEL"
else
  print "Not installed (no $INSTALL_PATH)"
fi
