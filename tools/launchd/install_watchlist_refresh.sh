#!/usr/bin/env zsh
# Install (or refresh) the launchd schedule for the morning watchlist
# pre-fetch (07:00 weekdays). Reads the template, substitutes the
# repo path + python3 location, writes to ~/Library/LaunchAgents/,
# then loads it.
#
# Idempotent — re-run any time after editing the template or moving
# the repo. It unloads the old version before loading the new one.
#
# Usage:
#   cd /path/to/repo
#   tools/launchd/install_watchlist_refresh.sh
#
# By default uses `command -v python3` (system Python). Override with:
#   PYTHON_BIN=/path/to/python3 tools/launchd/install_watchlist_refresh.sh

set -euo pipefail

LABEL="com.user.claude-watchlist-refresh"
SCRIPT_DIR="${0:a:h}"
TEMPLATE="$SCRIPT_DIR/com.user.claude-watchlist-refresh.plist.template"
REPO_PATH="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

# Find python3. Order:
#   1. $PYTHON_BIN env var (user override, highest priority)
#   2. command -v python3 (PATH lookup)
#   3. Common install locations (system, brew, pyenv) as fallback
if [[ -n "${PYTHON_BIN:-}" ]]; then
  if [[ ! -x "$PYTHON_BIN" ]]; then
    print -u2 "ERROR: \$PYTHON_BIN=$PYTHON_BIN is not executable."
    exit 1
  fi
else
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in \
    "/usr/bin/python3" \
    "/usr/local/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "$HOME/.pyenv/shims/python3" \
  ; do
    if [[ -x "$candidate" ]]; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  print -u2 "ERROR: 'python3' not found."
  print -u2 ""
  print -u2 "Re-run with explicit path:"
  print -u2 "  PYTHON_BIN=/full/path/to/python3 tools/launchd/install_watchlist_refresh.sh"
  exit 1
fi

# Sanity-check that this Python can import stock_web — if it can't,
# the launchd job will silently fail every morning. Better to refuse
# at install time.
if ! "$PYTHON_BIN" -c "import sys; sys.path.insert(0, '$REPO_PATH'); from stock_web.app import _load_watchlist" 2>/dev/null; then
  print -u2 "WARNING: $PYTHON_BIN cannot import stock_web.app."
  print -u2 "  Likely missing deps. Install them with:"
  print -u2 "    $PYTHON_BIN -m pip install --user fastapi uvicorn requests openpyxl yfinance"
  print -u2 "  Continuing anyway — fix this before relying on the schedule."
fi

print "Repo:       $REPO_PATH"
print "Python:     $PYTHON_BIN"
print "Target:     $INSTALL_PATH"
print ""

# Render template
mkdir -p "$(dirname "$INSTALL_PATH")"
sed -e "s|__REPO_PATH__|$REPO_PATH|g" \
    -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
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
print "Trigger immediately (for testing — won't wait for 07:00):"
print "  launchctl start $LABEL"
print ""
print "View log:"
print "  tail -f $REPO_PATH/tools/launchd/watchlist-refresh.log"
