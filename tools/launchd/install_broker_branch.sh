#!/usr/bin/env zsh
# Install (or refresh) the launchd schedule that runs (headless, weekdays
# 16:00) tools/broker_branch_pull.py --no-crawl — i.e. INGEST ONLY.
#
# The model: the user prepares step 2 (the BSR crawl) earlier in the day,
# so by 16:00 the CSVs are already in twse_web/output/. This job just
# ingests them — no crawl, no interactive pause, no terminal. The crawl
# (which solves a CAPTCHA) is never run or scheduled here; it's manual prep.
#
# Idempotent — re-run any time after editing the template or moving the
# repo. It unloads the old version before loading the new one.
#
# Usage:
#   cd /path/to/repo
#   tools/launchd/install_broker_branch.sh
#   PYTHON_BIN=/path/to/python3 tools/launchd/install_broker_branch.sh

set -euo pipefail

LABEL="com.user.claude-broker-branch"
SCRIPT_DIR="${0:a:h}"
TEMPLATE="$SCRIPT_DIR/com.user.claude-broker-branch.plist.template"
REPO_PATH="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

# Find python3: $PYTHON_BIN override → PATH → common locations.
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
    if [[ -x "$candidate" ]]; then PYTHON_BIN="$candidate"; break; fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  print -u2 "ERROR: 'python3' not found. Re-run with PYTHON_BIN=/full/path."
  exit 1
fi

# Sanity-check this Python can import the ingest module.
if ! "$PYTHON_BIN" -c "import sys; sys.path.insert(0, '$REPO_PATH'); import stock_web.broker_branch_ingest" 2>/dev/null; then
  print -u2 "WARNING: $PYTHON_BIN cannot import stock_web.broker_branch_ingest."
  print -u2 "  Continuing — fix before relying on the schedule."
fi

print ""
print "──────────────────────────────────────────────────────────────"
print "ⓘ  Runs headless weekdays 16:00 → ingest only (--no-crawl)."
print "   YOU must prepare step 2 BEFORE 16:00: run the BSR crawler so"
print "   the day's CSVs are already in twse_web/output/. This job does"
print "   NOT crawl — it just ingests whatever is there."
print "──────────────────────────────────────────────────────────────"

case "$REPO_PATH" in
  *Library/CloudStorage/*)
    print ""
    print "⚠  Repo is under ~/Library/CloudStorage/ (OneDrive/iCloud)."
    print "   launchd-spawned $PYTHON_BIN (and /bin/zsh) need Full Disk"
    print "   Access (Privacy & Security → Full Disk Access) or the job"
    print "   fails with 'Operation not permitted'. The morning"
    print "   watchlist-refresh job already uses these — if that one"
    print "   works, this one will too."
    ;;
esac

print ""
print "Repo:       $REPO_PATH"
print "Python:     $PYTHON_BIN"
print "Target:     $INSTALL_PATH"
print ""

mkdir -p "$(dirname "$INSTALL_PATH")"
sed -e "s|__REPO_PATH__|$REPO_PATH|g" \
    -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
    "$TEMPLATE" > "$INSTALL_PATH"
print "Wrote rendered plist."

if launchctl list | grep -q "$LABEL"; then
  print "Unloading existing $LABEL..."
  launchctl unload "$INSTALL_PATH" 2>/dev/null || true
fi
print "Loading $LABEL..."
launchctl load "$INSTALL_PATH"

print ""
print "✓ Installed."
print ""
print "Test now (runs ingest headlessly; check the log after):"
print "  launchctl start $LABEL"
print "  tail $REPO_PATH/tools/launchd/broker-branch.log"
print ""
print "Disable:"
print "  launchctl unload $INSTALL_PATH"
