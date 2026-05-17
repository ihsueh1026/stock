#!/usr/bin/env zsh
# Uninstall the daily news_log refresh launchd schedule.

set -euo pipefail

LABEL="com.user.claude-news-update"
INSTALL_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$INSTALL_PATH" ]]; then
  launchctl unload "$INSTALL_PATH" 2>/dev/null || true
  rm "$INSTALL_PATH"
  print "✓ Uninstalled $LABEL"
else
  print "Not installed (no $INSTALL_PATH)"
fi
