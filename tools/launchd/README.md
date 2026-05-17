# Daily news_log refresh via launchd

Schedules the `claude -p "更新 watchlist 新聞"` command to run weekdays
at 21:00 on this Mac. Uses `launchd` (not `cron`) so it catches up
after sleep, not just at exact-time fires.

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
tools/launchd/install.sh
```

The script:
1. Finds `claude` in your `$PATH` (override with `CLAUDE_BIN_OVERRIDE=...`)
2. Renders the template with this repo's absolute path + claude binary path
3. Writes to `~/Library/LaunchAgents/com.user.claude-news-update.plist`
4. Loads it via `launchctl`

Idempotent — safe to re-run after editing the template or moving the
repo. It unloads the old version before loading the new one.

## Verify

```bash
# Is it installed?
launchctl list | grep com.user.claude-news-update

# Trigger now for testing (won't wait for next schedule)
launchctl start com.user.claude-news-update

# Tail the log
tail -f tools/launchd/news-update.log
```

## Schedule

Currently weekdays (Mon-Fri) at 21:00. Edit
`com.user.claude-news-update.plist.template` `<StartCalendarInterval>`
block to change, then re-run `install.sh`.

Rationale for 21:00:
- 13:30 收盤 + 17:00 MOPS 截止 → 重訊全進來
- 18-21 媒體寫盤後解讀 → Yahoo 新聞流動完整
- 美股還沒開盤 → 不會被隔夜消息蓋過今天的訊息

## Uninstall

```bash
tools/launchd/uninstall.sh
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
| `com.user.claude-news-update.plist.template` | ✓ | Schedule + paths template |
| `install.sh` | ✓ | Render + launchctl load |
| `uninstall.sh` | ✓ | launchctl unload + remove |
| `news-update.log` | ✗ (gitignored) | Per-run stdout/stderr |
| `~/Library/LaunchAgents/com.user.claude-news-update.plist` | n/a (outside repo) | The actual loaded plist |
