#!/usr/bin/env zsh
# Install (or refresh) the launchd schedule for the daily news_log
# refresh. Reads the template, substitutes the repo path + claude CLI
# location, writes to ~/Library/LaunchAgents/, then loads it.
#
# Idempotent — re-run any time after editing the template or moving
# the repo. It unloads the old version before loading the new one.
#
# Usage:
#   cd /path/to/repo
#   tools/launchd/install.sh

set -euo pipefail

LABEL="com.user.claude-news-update"
SCRIPT_DIR="${0:a:h}"
TEMPLATE="$SCRIPT_DIR/com.user.claude-news-update.plist.template"
REPO_PATH="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

# Find the claude CLI. Prefer PATH lookup so users on different
# install methods (brew, npm, cargo) all work without editing.
CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
  print -u2 "ERROR: 'claude' CLI not found in PATH."
  print -u2 "  Install Claude Code first, or set CLAUDE_BIN env var:"
  print -u2 "    CLAUDE_BIN=/path/to/claude tools/launchd/install.sh"
  exit 1
fi
# Allow override
CLAUDE_BIN="${CLAUDE_BIN_OVERRIDE:-$CLAUDE_BIN}"

print "Repo:       $REPO_PATH"
print "Claude CLI: $CLAUDE_BIN"
print "Target:     $INSTALL_PATH"
print ""

# Render template
mkdir -p "$(dirname "$INSTALL_PATH")"
sed -e "s|__REPO_PATH__|$REPO_PATH|g" \
    -e "s|__CLAUDE_BIN__|$CLAUDE_BIN|g" \
    "$TEMPLATE" > "$INSTALL_PATH"
print "Wrote rendered plist."

# Unload prior version if present (idempotent)
if launchctl list | grep -q "$LABEL"; then
  print "Unloading existing $LABEL..."
  launchctl unload "$INSTALL_PATH" 2>/dev/null || true
fi

# Load new version
print "Loading $LABEL..."
launchctl load "$INSTALL_PATH"

print ""
print "✓ Installed."
print ""
print "Verify with:"
print "  launchctl list | grep $LABEL"
print ""
print "Trigger immediately (for testing — won't wait for next schedule):"
print "  launchctl start $LABEL"
print ""
print "View log:"
print "  tail -f $REPO_PATH/tools/launchd/news-update.log"
