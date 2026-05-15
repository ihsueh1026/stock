# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run / develop

```bash
# install (one-time)
pip3 install --user fastapi uvicorn requests openpyxl
# optional: enables LLM sentiment/summary on MOPS news (news_llm.py)
pip3 install --user anthropic
export ANTHROPIC_API_KEY=...

# web app — run from repo root, NOT from stock_web/
python3 -m uvicorn stock_web.app:app --host 0.0.0.0 --port 8000
# then open http://localhost:8000

# CLI fetcher (writes stock.xlsx with one sheet per stock)
python3 fetch_twse_daily.py                  # default: 2324, 30 days
python3 fetch_twse_daily.py -s 2330 2317 -n 60
python3 fetch_twse_daily.py --no-taiex
```

There are no tests, no linter config, and no build step. Verification is manual: run the server, hit endpoints, and watch logs.

## Architecture

The repo is a Taiwan-stock (TWSE 上市 + TPEX 上櫃) technical-analysis tool with two layers that share fetcher/indicator logic:

- **[fetch_twse_daily.py](fetch_twse_daily.py)** — standalone CLI. Pulls daily OHLCV from TWSE `STOCK_DAY` / TPEX `tradingStock`, plus TAIEX from `FMTQIK`, computes MA/RSI/KD/MACD/%change, and writes `stock.xlsx` (one sheet per stock). Uses a 250-day warmup buffer so the 26-day EMA inside MACD converges close to broker-app values. Throttles requests with `REQUEST_INTERVAL_SEC = 3` and retries transient TWSE "查詢日期大於今日" errors with backoff.

- **[stock_web/app.py](stock_web/app.py)** — FastAPI backend. Imports `fetch_twse_daily` as `twse` (note `sys.path` insertion at the top) and reuses its fetchers + `sma/rsi_wilder/kd/macd/pct_change/parse_*`. Adds an ADX(14) Wilder implementation locally. Serves [stock_web/static/index.html](stock_web/static/index.html) (single-file SPA, ~1700 lines) at `/`.

- **[stock_web/static/index.html](stock_web/static/index.html)** — entire frontend in one file (HTML + CSS + JS). Dark theme, mobile-aware, no build tooling.

- **Sibling fetchers in [stock_web/](stock_web/)** — each module is a single-responsibility fetcher with its own daily-cached JSON output and a matching `/api/...` endpoint:
  - [news_fetcher.py](stock_web/news_fetcher.py) — MOPS 重大訊息 (t05st01). Parses Big5 HTML, throttles at 2 s/request.
  - [news_llm.py](stock_web/news_llm.py) — optional: calls Anthropic API (Haiku 4.5) to tag each 重訊 as 利多/利空/中性 + summary. **Fails soft**: missing `anthropic` SDK or `ANTHROPIC_API_KEY` → items pass through unchanged. Idempotent — items already carrying `sentiment` + `summary` are skipped, so re-runs against partially-annotated cache are free. System prompt is `cache_control:ephemeral` for the 5-min prompt-cache window.
  - [fundamentals_fetcher.py](stock_web/fundamentals_fetcher.py) — MOPS t146sb05 簡明 statements (3-4 most recent periods). Server-side derives margin % and pre-tax ROE proxy so the SPA doesn't redo arithmetic. Amounts arrive in 千元.
  - [eps_history_fetcher.py](stock_web/eps_history_fetcher.py) — multi-year quarterly EPS via MOPS ajax_t164sb04. **Q4 quirk**: t164sb04 returns standalone EPS for Q1-Q3 but **cumulative full-year** EPS for Q4 (because the Q4 filing is the annual report). The fetcher derives Q4 = annual − (Q1+Q2+Q3) explicitly.
  - [dividend_fetcher.py](stock_web/dividend_fetcher.py) — TWSE/TPEX OpenAPI (BWIBBU_ALL / tpex_mainboard_peratio_analysis) for yield + per-share cash dividend. TWSE only exposes yield; TPEX exposes both.
  - [industry_pe_fetcher.py](stock_web/industry_pe_fetcher.py) — joins the same OpenAPI PER feeds with the daily company-info dump to compute median/quartile P/E per industry. Negative-or-zero PERs are excluded.
  - [revenue_fetcher.py](stock_web/revenue_fetcher.py) — monthly 月營收 from `t21sc03_{ROC_Y}_{M}_0.html`. Cached per (market, year, month) under names like `revenue_sii_202604.json` — these files are **immutable once published** and intentionally sit outside the dated-cache purge window (the filename has no YYYYMMDD suffix, so `_parse_cache_date` returns None and the purger skips them).

- **[backtest/](backtest/)** — event-study pipeline. [study.py](backtest/study.py) walks the full history of a single stock, recomputes the 7 lights at every bar (no look-ahead), and measures forward returns / alpha at 5/10/20/40 trading days for each summary-label transition. [build_stats.py](backtest/build_stats.py) pools those results across every stock under `backtest/data/*.json` into `backtest/data/_summary_stats.json`, which the live dashboard reads via `/api/backtest_stats` to display historical hit rates next to the current summary label. [prefetch.py](backtest/prefetch.py) / [prefetch_t86.py](backtest/prefetch_t86.py) seed the long-horizon caches study.py needs.

### Trading-day key

`_trading_day()` in [stock_web/app.py](stock_web/app.py) returns today's calendar date, walking back over weekends to the prior Friday. All daily caches (`{code}_{YYYYMMDD}.json`, `taiex_{YYYYMMDD}.json`, `t86_{YYYYMMDD}.json`, `companies_{YYYYMMDD}.json`) share this same tag. If today's TAIEX close hasn't been published yet (e.g. fetched intraday or right after 13:30 close), `taiex` rows come back null and the frontend's `taiexBar` ([stock_web/static/index.html](stock_web/static/index.html)) prompts the user to enter the close manually — which `PUT /api/taiex/today` persists and back-patches into existing per-stock caches.

### Cache layer ([stock_web/cache/](stock_web/cache/))

All caches are flat JSON files in `stock_web/cache/`, keyed by date in the filename:

- `{code}_{YYYYMMDD}.json` — per-stock parsed series + computed indicators. Stored as `{"code", "market", "rows": [...]}`. Old caches without `adx` are backfilled in-memory on load via `_backfill_adx`.
- `taiex_{YYYYMMDD}.json` — TAIEX close history (date-iso → close).
- `taiex_manual.json` — user-entered TAIEX overrides per date (PUT/DELETE `/api/taiex/today`). Overlay applies on top of auto cache. **Persistent — never purged** (no date suffix).
- `t86_{YYYYMMDD}.json` / `t86otc_{YYYYMMDD}.json` — three-major-investor net-shares per stock for that date. **Empty OR truncated results are NEVER persisted** (see `_fetch_t86` + `_t86_looks_complete`). TWSE occasionally serves a partial dump that's missing 200-300 listings but still includes warrants/ETFs, so a raw entry count isn't enough — we count 4-digit codes and require ≥ `T86_TWSE_MIN_STOCKS` / `T86_OTC_MIN_STOCKS`. Incomplete dumps would otherwise pin step 7 (法人) to gray for affected stocks indefinitely.
- `companies[_otc]_{YYYYMMDD}.json` — daily TWSE/TPEX company-info dump (~1MB). One fetch per trading day.

**Retention**: dated cache files older than `CACHE_RETENTION_DAYS` (7) are deleted on startup by `_purge_old_caches()`, called from the FastAPI startup event in a background thread. The 7-day floor is load-bearing — `_load_stock` looks back through the same window to find a prior cache for incremental refresh (see below). Don't drop below 7 without auditing both.

**Incremental per-stock refresh**: when today's cache is missing, `_load_stock` calls `_find_recent_stock_cache(code)` to grab the freshest cache within the last 7 days, then fetches only the current month (plus the prior month if `last_date` is in a different calendar month) via `month_fetcher`. Indicators are recomputed from the full merged series (recomputation is cheap; indicator state isn't persisted). Falls back to the original 13-month walk only when no prior cache exists. This drops a typical refresh from ~30–40s to a few seconds.

Concurrency: per-key locks via `_lock_for(key)` (with a guard mutex for the lock dict). Used for stock fetches, T86 fetches per market+date, TAIEX, and company info — prevents thundering-herd duplicate fetches when multiple watchlist items refresh at once.

### Markets (TWSE vs OTC) routing

`MARKET_TWSE = "twse"` (上市) and `MARKET_OTC = "otc"` (上櫃) are passed through every fetcher. A code's market is resolved via `_market_for(code)` against the daily company-info caches; falls back to TWSE for unknown codes (e.g. brand-new listings). The market is persisted into the per-stock cache JSON so subsequent loads don't have to re-resolve.

### 7-step dashboard

**Positioning**: this is a **market-state dashboard, not an entry/exit signal generator**. Backtest in [backtest/](backtest/) on 2395/5388/2357 over 5 years showed no signal predicts forward alpha across stocks/horizons. Summary labels (`🟢 多頭擴張` / `多頭發展` / `🔵 反彈訊號` / `🔴 趨勢轉弱` / `🟠 訊號分歧` / `🟡 盤整中`) describe *current state* — they do not say "buy now" or "sell now". The label strings live in `SUMMARY_LABELS` in [stock_web/app.py](stock_web/app.py) and are re-imported by `backtest/study.py` (`SIGNAL_DEFS`) and `backtest/exit_rules.py` (`BAD_SUMMARIES`), so a label rename propagates without manual sync.

`compute_dashboard()` runs seven traffic-light checks over the last 20 trading days: `_step_1_market`, `_step_2_trend`, `_step_3_momentum`, `_step_4_volume`, `_step_6_holding`, `_step_7_exit`, `_step_8_institutional`. (Steps are numbered for the UI 1..7 after assembly — original numbering 1/2/3/4/6/7/8 reflects an earlier draft that included a 停損 step now surfaced separately via `_stoploss_levels`.) Each step returns `{light: green|yellow|red|gray, detail, ...}`. `_summary()` collapses them into a single overall light/label, `_price_zones()` emits buy/sell ranges based on σ (std-dev of last 19 daily changes), and `_history_lights()` recomputes the lights for each of the last 15 days. Before the history loop, `_history_lights` prefetches T86 for the 19 dates that step 8 will need (the 15-day window plus the 4-day lookback) so every history row's institutional light is populated, not just where cache happened to exist. T86 is whole-market data shared across stocks, so a cold sync pays this cost once per day across the watchlist.

The trigger logic and cell ranges in `_step_*` functions mirror the formulas in `stock.xlsx` (see comments referencing B6:B25, N32, etc.) — when changing thresholds, keep that mental cross-reference in mind.

**Divergence + alerts (layered on top of the 7 lights, not gates)**: `_divergence()` compares the last 5 bars to the prior 10 looking for price/RSI6 disagreement; a bearish (頂背離) result soft-downgrades step 3 green→yellow, and either direction surfaces as an alert chip. `_compute_alerts()` emits chips for 爆量 / 量縮 / 法人連 N 日同向 / 背離 — these annotate the current view only and don't recompute over the history strip. Alerts respect `cached_only` so they can be evaluated without fanning out T86 fetches.

### API surface (all under `/api/`)

- `GET /api/stock/{code}?rows=N` — full series + dashboard (rows clamped 1..500).
- `GET|POST /api/watchlist`, `PUT|DELETE /api/watchlist/{code}`, `POST /api/watchlist/{code}/refresh`, `POST /api/watchlist/refresh` (batch — refresh every code), `POST /api/watchlist/reorder` — watchlist persisted to [stock_web/watchlist.json](stock_web/watchlist.json).
- `GET|PUT|DELETE /api/taiex/today` — manual TAIEX override. PUT also patches every existing per-stock cache for today whose last row's `taiex` is null (so dashboards reflect the override without a full refetch).
- `GET /api/news/{code}?days=14` — MOPS 重訊 with optional LLM annotations. The frontend renders a "copy news for Claude" button when `news_llm.is_available()` returns false, so users without an API key can still get sentiment by pasting into a chat.
- `GET /api/fundamentals/{code}?close=...` — annual EPS/margins/equity panel. `close` is optional and only used to derive a trailing P/E.
- `GET /api/eps_history/{code}?years=3` — multi-year quarterly EPS trend (with the Q4 fix-up).
- `GET /api/dividend/{code}?close=...`, `GET /api/industry_pe/{code}?per=...` — yield + per-industry P/E context.
- `GET /api/revenue/{code}` — monthly revenue series (immutable monthly cache).
- `GET /api/backtest_stats?label=...` — historical forward-return distribution for the current summary label, served from `backtest/data/_summary_stats.json`. **Stale unless [backtest/build_stats.py](backtest/build_stats.py) is re-run** after collecting fresh per-stock studies.

`/api/watchlist/{code}/refresh` and `/api/watchlist/refresh` are both synchronous. With incremental refresh and warm shared caches (TAIEX/T86/companies), single-stock refresh is a few seconds and a 30-stock batch is typically under a minute. Cold first-ever fetch with no prior cache still hits the 13-month walk (~30–60s per stock).

## Notes for editing

- Do NOT lower `REQUEST_INTERVAL_SEC` below 3 — TWSE rate-limits aggressively and starts returning bogus "查詢日期大於今日" errors.
- When changing cache JSON shape, account for the in-memory backfill path (`_backfill_adx`) so day-old caches still load.
- When adding step logic that needs T86 data, respect `cached_only` — historical-strip recomputes must not fan out network calls.
- Stock codes are validated as 4–6 digits in `_validate_code`; the file-prefix check in `taiex_today_put` (`prefix.isdigit() and 4 <= len(prefix) <= 6`) keeps non-stock caches (`taiex_`, `t86_`, `companies_`) from being patched.
- The web layer's `WEB_WARMUP_DAYS = 60` is shorter than the CLI's `WARMUP_DAYS = 250`; this trades a tiny MACD-EMA precision loss for response time. Don't unify them without understanding the trade-off.
- Summary labels are defined once in `stock_web/app.py` (`SUMMARY_LABELS`) and consumed by `backtest/study.py` (`SIGNAL_DEFS`) and `backtest/exit_rules.py` (`BAD_SUMMARIES`). Renaming a key (e.g. `"strong"`) is a real refactor — find/replace it everywhere. Renaming a label *value* (the emoji/string) is safe but invalidates pooled stats in `backtest/data/_summary_stats.json` keyed by the old string, so re-run [backtest/build_stats.py](backtest/build_stats.py) after.
- `news_llm.py` must keep failing soft. The MOPS news panel is the only feature that touches a paid API; never make it a hard dependency or fan it out into batch refresh paths.
- Monthly revenue files (`revenue_*_{YYYYMM}.json`) must keep their non-YYYYMMDD filename so `_purge_old_caches()` skips them. Don't rename to a date-suffixed scheme without teaching the purger.
