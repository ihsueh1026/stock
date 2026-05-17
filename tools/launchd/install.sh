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

# Find the claude CLI. Order:
#   1. $CLAUDE_BIN env var (user override, highest priority)
#   2. command -v claude (PATH lookup)
#   3. Common install locations (brew, npm, etc.) as fallback
if [[ -n "${CLAUDE_BIN:-}" ]]; then
  if [[ ! -x "$CLAUDE_BIN" ]]; then
    print -u2 "ERROR: \$CLAUDE_BIN=$CLAUDE_BIN is not executable."
    exit 1
  fi
else
  CLAUDE_BIN="$(command -v claude || true)"
fi
if [[ -z "$CLAUDE_BIN" ]]; then
  # Fallback: probe common locations so user often doesn't even
  # need to set $CLAUDE_BIN. Returns first match.
  for candidate in \
    "/opt/homebrew/bin/claude" \
    "/usr/local/bin/claude" \
    "$HOME/.local/bin/claude" \
    "$HOME/.npm-global/bin/claude" \
    "$HOME/.volta/bin/claude" \
    "$HOME/.nvm/versions/node/*/bin/claude"(N) \
  ; do
    if [[ -x "$candidate" ]]; then
      CLAUDE_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$CLAUDE_BIN" ]]; then
  print -u2 "ERROR: 'claude' CLI not found."
  print -u2 ""
  print -u2 "Try one of these to locate it manually:"
  print -u2 "  which claude            # if a login shell has it"
  print -u2 "  ls ~/.npm-global/bin/   # npm global install"
  print -u2 "  ls /opt/homebrew/bin/ | grep claude  # Homebrew (M1/M2)"
  print -u2 "  ls /usr/local/bin/ | grep claude     # Homebrew (Intel)"
  print -u2 "  find / -name 'claude' -type f 2>/dev/null | head"
  print -u2 ""
  print -u2 "Then re-run with explicit path:"
  print -u2 "  CLAUDE_BIN=/full/path/to/claude tools/launchd/install.sh"
  exit 1
fi

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
