# Daily Claude / data jobs via launchd

Two scheduled jobs in this directory, both using `launchd` (not `cron`)
so they catch up after sleep instead of silently missing:

1. **`com.user.claude-news-update`** — weekdays 21:00 — runs
   `claude -p "更新 watchlist 新聞"` to refresh the news_log + MOPS
   sentiment annotations. (Smart-skip: idempotent within a trading day.)
2. **`com.user.claude-watchlist-refresh`** — weekdays 07:00 — runs
   `python3 -m tools.refresh_watchlist` to pre-fetch each watchlist
   code's daily cache + US market caches. No `claude` CLI involved —
   pure Python, no token cost. By 9am market open the dashboard loads
   instantly without the cold 30-60s per-stock TWSE fetch.

## Why launchd over cron

| | `cron` | `launchd` |
|---|---|---|
| Mac sleeping at schedule time | misses, no retry | runs at next wake |
| GUI session needed | no | no |
| User-scoped install | yes | yes (`~/Library/LaunchAgents/`) |
| macOS native | also present but deprecated | preferred |

## Setup (one-time)

```bash
cd /path/to/Claude

# News update — weekdays 21:00
tools/launchd/install.sh

# Watchlist pre-fetch — weekdays 07:00
tools/launchd/install_watchlist_refresh.sh
```

The scripts:
1. Find their respective binary (`claude` for news, `python3` for refresh).
   Override with `CLAUDE_BIN=...` or `PYTHON_BIN=...`.
2. Render the matching plist template with this repo's absolute path.
3. Write to `~/Library/LaunchAgents/`.
4. Load via `launchctl`.

Both are idempotent — safe to re-run after editing a template or
moving the repo. Each unloads the old version before loading the new
one.

## Verify

```bash
# Are they installed?
launchctl list | grep com.user.claude-

# Trigger now for testing (won't wait for the schedule)
launchctl start com.user.claude-news-update
launchctl start com.user.claude-watchlist-refresh

# Tail logs
tail -f tools/launchd/news-update.log
tail -f tools/launchd/watchlist-refresh.log
```

## Schedule

| Job | When | Why that time |
|---|---|---|
| news-update | weekdays 21:00 | 13:30 收盤 + 17:00 MOPS 截止 → 重訊全進來; 18-21 媒體寫盤後解讀 → Yahoo 流動完整; 美股還沒開盤 → 不會被隔夜消息蓋過 |
| watchlist-refresh | weekdays 07:00 | 前一日 TWSE 資料早已釋出; 2h 前 9:00 開盤,夠時間跑完 30-60s/檔的 TWSE 抓取 (新股冷啟動更久); 美股 cache 同時更新,strip 不會 stale |

Edit the matching `.plist.template` `<StartCalendarInterval>` block to
change times, then re-run the install script.

## Uninstall

```bash
tools/launchd/uninstall.sh                       # news update
tools/launchd/uninstall_watchlist_refresh.sh     # watchlist refresh
```

## Failure mode handling

The smart-skip in `CLAUDE.md` (`更新 watchlist 新聞` trigger) ensures
that even if launchd fires multiple times in one day (e.g. you ran a
manual trigger earlier and 21:00 fires too), the second call exits in
seconds without re-fetching.

Logs accumulate in `news-update.log`. Rotate manually or add a
weekly trim. Each daily run is typically 30-200 lines.

## Files

| File | Tracked | Purpose |
|---|---|---|
| `com.user.claude-news-update.plist.template` | ✓ | News-update schedule + paths |
| `com.user.claude-watchlist-refresh.plist.template` | ✓ | Watchlist-refresh schedule + paths |
| `install.sh` / `uninstall.sh` | ✓ | News-update lifecycle |
| `install_watchlist_refresh.sh` / `uninstall_watchlist_refresh.sh` | ✓ | Watchlist-refresh lifecycle |
| `news-update.log` | ✗ (gitignored) | Per-run stdout/stderr (news job) |
| `watchlist-refresh.log` | ✗ (gitignored) | Per-run stdout/stderr (refresh job) |
| `~/Library/LaunchAgents/com.user.claude-*.plist` | n/a (outside repo) | The actually loaded plists |
