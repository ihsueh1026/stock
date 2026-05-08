"""FastAPI backend for the TWSE stock viewer.

Wraps the existing fetch_twse_daily.py logic and exposes one JSON endpoint.
Run with:
    uvicorn stock_web.app:app --reload --port 8000
from the parent directory.
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Body
from fastapi.staticfiles import StaticFiles

# Make the parent dir importable so we can reuse fetch_twse_daily.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fetch_twse_daily as twse  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Shorter warmup than the CLI script's 250: trades MACD-EMA precision for
# response time. 60 days fully covers MA20/RSI12/KD9; MACD-OSC will differ
# slightly from broker apps that load multi-year history.
WEB_WARMUP_DAYS = 60

# Per-stock locks so concurrent requests for the same code don't double-fetch.
_fetch_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _fetch_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _fetch_locks[key] = lock
        return lock


# 交易日切換點:下午 5:30 之後才算進入新的交易日。5:30 之前(以及週末)
# 都對應到「最近一個已收盤的交易日」,這樣收盤後整理完資料才切換快取,
# 不必擔心下午 3 點剛收盤時資料尚未完整更新。
TRADING_DAY_ROLLOVER = time(17, 30)  # 17:30


def _trading_day(now: datetime | None = None) -> date:
    now = now or datetime.now()
    d = now.date()
    if now.time() < TRADING_DAY_ROLLOVER:
        d -= timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def _today_tag() -> str:
    return _trading_day().strftime("%Y%m%d")


def _today_iso() -> str:
    return _trading_day().isoformat()


def _stock_cache(code: str) -> Path:
    return CACHE_DIR / f"{code}_{_today_tag()}.json"


def _taiex_cache() -> Path:
    # Use the real calendar date so this cache refreshes on the actual day,
    # independent of the 17:30 trading-day rollover.  Stock caches keyed by
    # trading-day tag may include the current calendar day's rows even before
    # the rollover flips; using the calendar date ensures their taiex field
    # is always populated from a fresh fetch.
    return CACHE_DIR / f"taiex_{date.today().strftime('%Y%m%d')}.json"


# Market identifiers — drives fetcher routing, T86 source, and frontend label.
MARKET_TWSE = "twse"   # 上市
MARKET_OTC = "otc"     # 上櫃


T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
T86_OTC_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
T86_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-web/1.0)",
    "Accept": "application/json",
}


def _t86_cache(date_compact: str, market: str = MARKET_TWSE) -> Path:
    prefix = "t86" if market == MARKET_TWSE else "t86otc"
    return CACHE_DIR / f"{prefix}_{date_compact}.json"


def _t86_cached_only(date_iso: str, market: str = MARKET_TWSE) -> dict | None:
    """Return cached T86 dict for the date, or None if not cached. No network."""
    cache = _t86_cache(date_iso.replace("-", ""), market)
    if not cache.exists():
        return None
    try:
        with cache.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _fetch_t86(date_iso: str, market: str = MARKET_TWSE) -> dict:
    """Return {code: {f, t, d, tot}} of net shares per stock for date_iso.

    Empty dict on non-trading days or fetch errors. Result is cached forever
    (per-date file) since historical institutional data does not change.
    Net values are in shares (not lots).
    """
    date_compact = date_iso.replace("-", "")
    cache = _t86_cache(date_compact, market)
    if cache.exists():
        try:
            with cache.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    with _lock_for(f"__t86_{market}_{date_compact}__"):
        if cache.exists():
            try:
                with cache.open() as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        if market == MARKET_TWSE:
            out = _fetch_t86_twse(date_compact)
        else:
            out = _fetch_t86_otc(date_iso)
        try:
            with cache.open("w") as f:
                json.dump(out, f)
        except OSError:
            pass
        return out


def _fetch_t86_twse(date_compact: str) -> dict:
    try:
        resp = requests.get(
            T86_URL,
            params={"date": date_compact, "selectType": "ALL", "response": "json"},
            headers=T86_HTTP_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {}
    out: dict = {}
    if data.get("stat") == "OK":
        for row in data.get("data", []):
            try:
                code = (row[0] or "").strip()
                if not code:
                    continue
                foreign = int((row[4] or "0").replace(",", ""))
                trust = int((row[10] or "0").replace(",", ""))
                dealer = int((row[11] or "0").replace(",", ""))
                total = int((row[18] or "0").replace(",", ""))
                out[code] = {"f": foreign, "t": trust, "d": dealer, "tot": total}
            except (IndexError, ValueError):
                continue
    return out


def _fetch_t86_otc(date_iso: str) -> dict:
    """TPEx 三大法人買賣明細 (24-col schema).

    Columns verified against live API:
        [0]  代號
        [1]  名稱
        [10] 外資合計買賣超股數
        [13] 投信買賣超股數
        [22] 自營商合計買賣超股數
        [23] 三大法人合計買賣超股數
    """
    try:
        resp = requests.get(
            T86_OTC_URL,
            params={"type": "Daily", "sect": "EW", "date": date_iso.replace("-", "/")},
            headers=T86_HTTP_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {}
    out: dict = {}
    tables = data.get("tables") or []
    rows = tables[0].get("data") if tables else []
    for row in rows:
        try:
            code = (row[0] or "").strip()
            if not code:
                continue
            foreign = int((row[10] or "0").replace(",", ""))
            trust = int((row[13] or "0").replace(",", ""))
            dealer = int((row[22] or "0").replace(",", ""))
            total = int((row[23] or "0").replace(",", ""))
            out[code] = {"f": foreign, "t": trust, "d": dealer, "tot": total}
        except (IndexError, ValueError):
            continue
    return out


TAIEX_MANUAL_FILE = CACHE_DIR / "taiex_manual.json"
_taiex_manual_lock = threading.Lock()


def _load_taiex_manual() -> dict:
    """Manual TAIEX overrides keyed by ISO date string (persists across days)."""
    if not TAIEX_MANUAL_FILE.exists():
        return {}
    try:
        with TAIEX_MANUAL_FILE.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_taiex_manual(data: dict) -> None:
    with TAIEX_MANUAL_FILE.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _overlay_manual(parsed: dict) -> dict:
    """Overlay manual overrides onto an auto-fetched {date: value} dict."""
    manual = _load_taiex_manual()
    for k, v in manual.items():
        try:
            parsed[datetime.fromisoformat(k).date()] = v
        except ValueError:
            continue
    return parsed


def _load_taiex(target_rows: int) -> dict:
    cache = _taiex_cache()
    if cache.exists():
        with cache.open() as f:
            data = json.load(f)
        parsed = {datetime.fromisoformat(k).date(): v for k, v in data.items()}
        return _overlay_manual(parsed)

    with _lock_for("__taiex__"):
        if cache.exists():
            with cache.open() as f:
                data = json.load(f)
            parsed = {datetime.fromisoformat(k).date(): v for k, v in data.items()}
            return _overlay_manual(parsed)
        rows = twse.fetch_recent_rows(twse.fetch_taiex_month, target_rows, "TAIEX")
        parsed = twse.parse_taiex_rows(rows)
        serial = {d.isoformat(): v for d, v in parsed.items()}
        with cache.open("w") as f:
            json.dump(serial, f)
        return _overlay_manual(parsed)


def _compute_rows(series: list[dict], taiex_close: dict) -> list[dict]:
    closes = [pt["close"] for pt in series]
    highs = [pt["high"] for pt in series]
    lows = [pt["low"] for pt in series]

    ma5 = twse.sma(closes, 5)
    ma10 = twse.sma(closes, 10)
    ma20 = twse.sma(closes, 20)
    ma60 = twse.sma(closes, 60)
    rsi6 = twse.rsi_wilder(closes, 6)
    rsi12 = twse.rsi_wilder(closes, 12)
    k_vals, d_vals = twse.kd(highs, lows, closes, 9)
    _dif, _sig, osc = twse.macd(closes, 12, 26, 9)
    chg = twse.pct_change(closes)

    out = []
    for i, pt in enumerate(series):
        out.append({
            "date": pt["date"].isoformat(),
            "taiex": taiex_close.get(pt["date"]),
            "high": pt["high"],
            "low": pt["low"],
            "close": pt["close"],
            "lots": pt["lots"],
            "change_pct": chg[i],
            "ma5": ma5[i],
            "ma10": ma10[i],
            "ma20": ma20[i],
            "ma60": ma60[i],
            "rsi6": rsi6[i],
            "rsi12": rsi12[i],
            "kd_k": k_vals[i],
            "kd_d": d_vals[i],
            "macd_osc": osc[i],
        })
    return out


def _load_stock(code: str, output_rows: int) -> list[dict]:
    cache = _stock_cache(code)
    if cache.exists():
        with cache.open() as f:
            return json.load(f).get("rows") or []

    with _lock_for(code):
        if cache.exists():
            with cache.open() as f:
                return json.load(f).get("rows") or []

        # Always fetch enough to satisfy the largest reasonable request,
        # so a single day's cache covers any output window the user picks.
        target = max(output_rows, 200) + WEB_WARMUP_DAYS
        market = _market_for(code)
        if market == MARKET_OTC:
            month_fetcher = lambda y, m: twse.fetch_otc_month(y, m, code)
        else:
            # Default to TWSE if unknown — keeps prior behavior for codes
            # not yet in the company maps (e.g. brand-new listings).
            market = MARKET_TWSE
            month_fetcher = lambda y, m: twse.fetch_stock_month(y, m, code)
        raw = twse.fetch_recent_rows(month_fetcher, target, code)
        if not raw:
            return []
        series = twse.parse_stock_rows(raw)
        if not series:
            return []
        taiex = _load_taiex(target)
        rows = _compute_rows(series, taiex)
        with cache.open("w") as f:
            json.dump({"code": code, "market": market, "rows": rows}, f)
        return rows


def _stock_cache_market(code: str) -> str | None:
    """Read the persisted market from today's cache, if any."""
    cache = _stock_cache(code)
    if not cache.exists():
        return None
    try:
        with cache.open() as f:
            return json.load(f).get("market")
    except (OSError, json.JSONDecodeError):
        return None


# ---- 7-step dashboard (mirrors stock.xlsx per-stock dashboard) -------------
#
# Lights are returned as tokens, not emojis, so the frontend can style them:
#   "green" | "yellow" | "red" | "gray" (insufficient data) | "none" (informational)
#
# All step calculations use a 20-day window ending at the most recent row,
# matching the cell ranges in stock.xlsx (B6:B25 etc.).


def _sigma(window):
    """Sample stddev of last 19 daily change %, divided by 100 (xlsx N32)."""
    changes = [r["change_pct"] for r in window[-19:] if r["change_pct"] is not None]
    if len(changes) < 2:
        return None
    return statistics.stdev(changes) / 100.0


def _step_1_market(window, last):
    taiex_today = last.get("taiex")
    taiex_vals = [r["taiex"] for r in window if r.get("taiex") is not None]
    base = {"step": 1, "title": "大盤過濾", "condition": "大盤 > 自身 MA20"}
    if taiex_today is None or len(taiex_vals) < 5:
        return {**base, "light": "gray", "detail": "大盤資料不足"}
    avg = sum(taiex_vals) / len(taiex_vals)
    light = "green" if taiex_today > avg else "red"
    detail = f"大盤={taiex_today:,.0f}  MA={avg:,.0f}"
    return {**base, "light": light, "detail": detail}


def _step_2_trend(window, last):
    ma10 = last.get("ma10")
    ma20 = last.get("ma20")
    ma60 = last.get("ma60")
    # 5-day linear regression slope on MA20 — avoids 2-point jitter at flat tops
    ma20_vals = [row.get("ma20") for row in window[-5:]]
    base = {"step": 2, "title": "趨勢結構",
            "condition": "MA10>MA20>MA60 且 MA20 五日回歸斜率向上"}
    if any(v is None for v in (ma10, ma20, ma60)) or None in ma20_vals or len(ma20_vals) < 5:
        return {**base, "light": "gray", "detail": "資料不足"}
    n = len(ma20_vals)
    x_bar = (n - 1) / 2.0
    y_bar = sum(ma20_vals) / n
    cov = sum((i - x_bar) * (ma20_vals[i] - y_bar) for i in range(n))
    var = sum((i - x_bar) ** 2 for i in range(n))  # = 10.0 for n=5
    lr_slope = cov / var if var else 0.0
    c_slope = lr_slope > 0
    c_stack = (ma10 > ma20) and (ma20 > ma60)
    c_partial = ma10 > ma20
    if c_stack and c_slope:
        light = "green"
    elif c_partial and c_slope:
        light = "yellow"
    elif c_partial or c_slope:
        light = "yellow"
    else:
        light = "red"
    trend = "多頭排列" if c_stack else ("多頭" if c_partial else "空頭")
    detail = (f"MA10={ma10:.1f}  MA20={ma20:.1f}  MA60={ma60:.1f}  "
              f"{trend}  斜率:{'↑' if c_slope else '↓'}({lr_slope:+.2f})")
    return {**base, "light": light, "detail": detail}


def _step_3_momentum(window, last, prev):
    needed = (last.get("ma5"), last.get("ma10"), last.get("rsi6"),
              last.get("kd_k"), last.get("kd_d"),
              prev.get("rsi6"), prev.get("kd_k"), prev.get("kd_d"))
    base = {"step": 3, "title": "動能三合",
            "condition": "MA5>MA10 + K>D + RSI6>50\nRSI6 自<30反彈首破50 / KD 低位金叉 / K首破50"}
    if any(v is None for v in needed):
        return {**base, "light": "gray", "detail": "資料不足"}
    ma5, ma10, rsi6, k, d = (last["ma5"], last["ma10"], last["rsi6"],
                              last["kd_k"], last["kd_d"])
    rsi6_p, k_p, d_p = prev["rsi6"], prev["kd_k"], prev["kd_d"]

    c_ma = ma5 > ma10
    c_kd = k > d
    c_rsi = rsi6 > 50

    rsi6_recent = [r["rsi6"] for r in window[-11:-1] if r.get("rsi6") is not None]
    rebounded_from_oversold = bool(rsi6_recent) and min(rsi6_recent) < 30

    kd_low_zone = (k_p < 20) or (40 <= k_p < 50)
    kd_golden_low = (k_p < d_p) and (k > d) and kd_low_zone
    rsi6_first_50 = (rsi6_p < 50) and (rsi6 >= 50) and rebounded_from_oversold
    k_first_50 = (k_p < 50) and (k >= 50)
    trigger = kd_golden_low or rsi6_first_50 or k_first_50

    if c_ma and c_kd and c_rsi and trigger:
        light = "green"
    elif c_ma and c_kd and c_rsi:
        light = "yellow"
    elif sum([c_ma, c_kd, c_rsi]) >= 2:
        light = "yellow"
    else:
        light = "red"

    kd_cross_today = "✓" if (k_p < d_p and k > d) else "✗"
    suffix = "(自<30反彈首破50)" if rsi6_first_50 else ""
    detail = (f"MA金叉:{'✓' if c_ma else '✗'}  "
              f"KD金叉:{kd_cross_today}(K={k:.0f})  "
              f"RSI6:{rsi6:.0f}{suffix}")
    return {**base, "light": light, "detail": detail}


def _step_4_volume(window, last, prev, s3_light, s6_light):
    base = {"step": 4, "title": "量能",
            "condition": "≥1.5 × 5日均量,或連2日價漲量增"}
    vol = last.get("lots")
    close = last.get("close")
    close_p = prev.get("close")
    vol_p = prev.get("lots")
    recent = [r["lots"] for r in window[-6:-1] if r.get("lots") is not None]
    if vol is None or close is None or close_p is None or len(recent) < 3:
        return {**base, "light": "gray", "detail": "資料不足"}
    avg = sum(recent) / len(recent)
    ratio = vol / avg if avg > 0 else 0
    price_up_vol_down = (close > close_p) and (vol_p is not None and vol < vol_p)

    prev2 = window[-3] if len(window) >= 3 else {}
    close_p2 = prev2.get("close")
    vol_p2 = prev2.get("lots")
    two_day_pv_up = (
        vol_p is not None and close_p2 is not None and vol_p2 is not None
        and close > close_p and vol > vol_p
        and close_p > close_p2 and vol_p > vol_p2
    )

    burst = vol >= 1.5 * avg
    healthy = vol >= avg

    if s3_light == "green":
        if burst or two_day_pv_up:
            light = "green"
        elif healthy:
            light = "yellow"
        else:
            light = "red"
    elif s6_light == "green":
        if price_up_vol_down:
            light = "red"
        elif healthy:
            light = "green"
        else:
            light = "yellow"
    else:
        if burst or two_day_pv_up:
            light = "green"
        elif healthy:
            light = "yellow"
        else:
            light = "red"

    warn = "  ⚠價漲量縮" if price_up_vol_down else ""
    pv_tag = "  連2日價漲量增" if two_day_pv_up else ""
    detail = f"今量={vol:,}  5日均={avg:,.0f}  比值={ratio:.2f}x{pv_tag}{warn}"
    return {**base, "light": light, "detail": detail}


def _stoploss_levels(last, sigma):
    """Return three stop-loss rows for the 距離預警 area (not a signal step).

    緊 = MA10 動態, 中 = 固定 -X%(依 σ), 鬆 = MA20 動態.
    """
    ma10 = last.get("ma10")
    ma20 = last.get("ma20")
    close = last.get("close")
    if any(v is None for v in (ma10, ma20, close)) or sigma is None:
        return []
    if sigma < 0.025:
        pct = 5
    elif sigma < 0.04:
        pct = 6
    else:
        pct = 8
    mid = close * (1 - pct / 100)
    rows = [
        ("🟢 緊停損", ma10, "MA10 動態停損"),
        ("🟡 中停損", mid, f"固定 -{pct}% (σ={sigma * 100:.1f}%)"),
        ("🔴 鬆停損", ma20, "MA20 動態停損"),
    ]
    out = []
    for label, target, note in rows:
        out.append({
            "label": label,
            "target": round(target, 1),
            "delta_pct": round((target - close) / close * 100, 2),
            "note": note,
        })
    return out


def _step_6_holding(window, last, prev):
    base = {"step": 6, "title": "持有訊號",
            "condition": "收盤 > MA5;跌破需連2日 或 帶量跌破"}
    close, ma5 = last.get("close"), last.get("ma5")
    close_p, ma5_p = prev.get("close"), prev.get("ma5")
    if any(v is None for v in (close, ma5, close_p, ma5_p)):
        return {**base, "light": "gray", "detail": "資料不足"}

    vol = last.get("lots")
    recent_vols = [r["lots"] for r in window[-6:-1] if r.get("lots") is not None]
    avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else None
    vol_surge = (vol is not None and avg_vol is not None
                 and avg_vol > 0 and vol >= 1.5 * avg_vol)

    if close > ma5:
        light, note = "green", "守穩"
    elif close < ma5 and close_p < ma5_p:
        light, note = "red", "連2日跌破"
    elif close < ma5 and vol_surge:
        light, note = "red", "帶量跌破"
    else:
        light, note = "yellow", "單日跌破(警示)"
    detail = f"收盤={close:.1f}  MA5={ma5:.1f}  {note}"
    return {**base, "light": light, "detail": detail}


def _step_7_exit(window, last, prev):
    """4 conditions → ≥2 = red, =1 = yellow, =0 = green."""
    base = {"step": 7, "title": "出場警示", "condition": "4 取 2"}
    needed_keys = ("close", "rsi6", "lots", "ma5", "kd_k", "kd_d")
    if any(last.get(k) is None for k in needed_keys):
        return {**base, "light": "gray", "detail": "資料不足"}
    if any(prev.get(k) is None for k in ("close", "ma5", "kd_k", "kd_d")):
        return {**base, "light": "gray", "detail": "資料不足"}

    prev10 = window[-11:-1] if len(window) >= 11 else window[:-1]
    closes_p10 = [r["close"] for r in prev10 if r.get("close") is not None]
    rsi6_p10 = [r["rsi6"] for r in prev10 if r.get("rsi6") is not None]
    vol_p10 = [r["lots"] for r in prev10 if r.get("lots") is not None]
    if not (closes_p10 and rsi6_p10 and vol_p10):
        return {**base, "light": "gray", "detail": "歷史資料不足"}

    close, rsi6, vol = last["close"], last["rsi6"], last["lots"]
    ma5 = last["ma5"]
    close_p, ma5_p = prev["close"], prev["ma5"]
    k_p, d_p = prev["kd_k"], prev["kd_d"]
    k, d = last["kd_k"], last["kd_d"]
    osc, osc_p = last.get("macd_osc"), prev.get("macd_osc")

    macd_shrinking = (osc is not None and osc_p is not None
                      and osc > 0 and osc_p > 0 and osc < osc_p)
    rsi_div = ((close > max(closes_p10)) and (rsi6 < max(rsi6_p10))
               and macd_shrinking)
    vol_div = (close > max(closes_p10)) and (vol < max(vol_p10))
    ma5_down = (close < ma5) and (ma5 < ma5_p)
    kd_high_dead = ((k_p > d_p) and (k < d) and (k_p > 80)
                    and (close < close_p))

    triggered = sum([rsi_div, vol_div, ma5_down, kd_high_dead])
    if triggered >= 2:
        light = "red"
    elif triggered == 1:
        light = "yellow"
    else:
        light = "green"

    detail = (f"RSI背離+MACD縮:{'✓' if rsi_div else '✗'}  "
              f"量價背離:{'✓' if vol_div else '✗'}  "
              f"MA5翻下:{'✓' if ma5_down else '✗'}  "
              f"KD高死叉+價跌:{'✓' if kd_high_dead else '✗'}")
    return {**base, "light": light, "detail": detail}


def _step_8_institutional(window, code, market=MARKET_TWSE, cached_only=False):
    """5 日三大法人籌碼 → 法人認養度。

    依據文件「動態法人認養度」概念,但暫不依市值動態調權重,
    以「外資買超力道 + 投信連買 + 三大合計淨流向」三條件綜合判斷:
      綠 = 5 日三大合計淨買 且 (外資力道 ≥1% 或 投信連買 ≥3 日)
      紅 = 5 日三大合計淨賣 且 (外資或投信連賣 ≥3 日)
      黃 = 其他

    cached_only=True skips the network and reads only persisted T86 — used
    when computing historical lights so old dates don't trigger fresh fetches.
    """
    base = {"step": 8, "title": "法人認養", "condition": "5 日三大法人籌碼"}
    if len(window) < 5:
        return {**base, "light": "gray", "detail": "資料不足"}
    last5 = window[-5:]

    days = []
    for r in last5:
        date_iso = r.get("date")
        if not date_iso:
            continue
        if cached_only:
            t86 = _t86_cached_only(date_iso, market)
            if t86 is None:
                continue
            info = t86.get(code)
        else:
            info = _fetch_t86(date_iso, market).get(code)
        if info is None:
            continue
        lots = r.get("lots") or 0
        days.append({
            "date": date_iso,
            "f": info["f"], "t": info["t"], "tot": info["tot"],
            "vol_shares": lots * 1000,
        })
    if len(days) < 3:
        return {**base, "light": "gray", "detail": "法人資料不足"}

    sum_f = sum(d["f"] for d in days)
    sum_t = sum(d["t"] for d in days)
    sum_tot = sum(d["tot"] for d in days)
    sum_vol = sum(d["vol_shares"] for d in days) or 1
    foreign_strength = sum_f / sum_vol * 100  # %

    def _streak(values):
        """Return (buy_streak, sell_streak) counted from the latest day backward."""
        buy = sell = 0
        for v in reversed(values):
            if v > 0 and sell == 0:
                buy += 1
            elif v < 0 and buy == 0:
                sell += 1
            else:
                break
        return buy, sell

    f_buy, f_sell = _streak([d["f"] for d in days])
    t_buy, t_sell = _streak([d["t"] for d in days])

    bullish = (sum_tot > 0) and (foreign_strength >= 1.0 or t_buy >= 3)
    bearish = (sum_tot < 0) and (f_sell >= 3 or t_sell >= 3)
    if bullish:
        light = "green"
    elif bearish:
        light = "red"
    else:
        light = "yellow"

    def _streak_txt(buy, sell):
        if buy:
            return f"連買{buy}日"
        if sell:
            return f"連賣{sell}日"
        return "持平"

    detail = (
        f"5日合計:{sum_tot/1000:+,.0f}張  "
        f"外資力道:{foreign_strength:+.2f}%  "
        f"外資{_streak_txt(f_buy, f_sell)}  "
        f"投信{_streak_txt(t_buy, t_sell)}"
    )
    return {**base, "light": light, "detail": detail}


def _summary(steps):
    greens = sum(1 for s in steps if s["light"] == "green")
    yellows = sum(1 for s in steps if s["light"] == "yellow")
    reds = sum(1 for s in steps if s["light"] == "red")
    total = len(steps)
    s1_light = steps[0]["light"]
    s2_light = steps[1]["light"]
    s3_light = steps[2]["light"]
    s4_light = steps[3]["light"]
    s7_light = steps[5]["light"]  # 出場警示 (after dropping 停損 from steps)
    s8_light = steps[6]["light"] if len(steps) >= 7 else "gray"  # 法人認養
    first_4_green = sum(1 for s in steps[:4] if s["light"] == "green")

    if (s1_light == "green" and first_4_green == 4
            and s7_light != "red" and s8_light != "red"):
        light, label = "green", "🟢 強進場"
    elif (s1_light == "green" and first_4_green >= 3
            and s7_light != "red" and s8_light != "red"):
        light, label = "green", "🟢 次強進場"
    elif (s1_light == "green" and s3_light == "green"
            and s2_light != "green" and s4_light != "green"
            and s7_light != "red" and s8_light != "red"):
        light, label = "blue", "🔵 反轉進場"
    elif s7_light == "red":
        light, label = "red", "🔴 出場"
    elif reds >= 2:
        light, label = "orange", "🟠 觀望"
    else:
        light, label = "yellow", "🟡 等待"

    return {
        "light": light, "label": label,
        "passed": greens, "warning": yellows, "danger": reds,
        "total": total,
        "score": f"通過 {greens} / {total}  警示 {yellows}  危險 {reds}",
    }


def _price_zones(summary, last, sigma, s6_light):
    close = last.get("close")
    ma5 = last.get("ma5")
    ma10 = last.get("ma10")
    if close is None or sigma is None:
        return {"mode": "wait", "zones": [], "note": "資料不足"}

    light = summary["light"]
    is_buy = light == "green"
    is_sell = (light == "red") or (light == "orange" and s6_light == "red")

    if is_buy:
        zones = [
            {"name": "🟢 積極買進", "basis": "-1σ 統計下緣",
             "low": round(close * (1 - sigma * 1.2), 1),
             "high": round(close * (1 - sigma * 0.8), 1)},
            {"name": "🟡 較安全買點", "basis": "-1.5σ 跌深區",
             "low": round(close * (1 - sigma * 1.8), 1),
             "high": round(close * (1 - sigma * 1.4), 1)},
            {"name": "🔵 強支撐買點", "basis": "-2σ 強支撐",
             "low": round(close * (1 - sigma * 2.4), 1),
             "high": round(close * (1 - sigma * 2.0), 1)},
        ]
        note = f"燈號偏多 → 顯示買進區間(以收盤 {close:.1f}、σ={sigma*100:.2f}%)"
        return {"mode": "buy", "zones": zones, "note": note}

    if is_sell and ma5 is not None and ma10 is not None:
        zones = [
            {"name": "🟡 小反彈賣", "basis": "MA5 壓力",
             "low": round(min(ma5, close * (1 + sigma * 0.8)), 1),
             "high": round(max(ma5, close * (1 + sigma * 1.2)), 1)},
            {"name": "🔴 較佳出場", "basis": "MA10 強壓",
             "low": round(min(ma10, close * (1 + sigma * 1.4)), 1),
             "high": round(max(ma10, close * (1 + sigma * 1.8)), 1)},
            {"name": "⚠️ 突破關鍵", "basis": "站上該價燈號轉強",
             "low": round(max(ma10, close * (1 + sigma * 2.0)), 1),
             "high": round(close * (1 + sigma * 2.4), 1)},
        ]
        note = f"燈號偏空 → 顯示賣出區間(以收盤 {close:.1f}、σ={sigma*100:.2f}%)"
        return {"mode": "sell", "zones": zones, "note": note}

    return {
        "mode": "wait", "zones": [],
        "note": f"目前燈號為「{summary['label']}」,建議等待、不推薦進出場",
    }


def _distance(last, sigma):
    close = last.get("close")
    ma5 = last.get("ma5")
    if close is None or sigma is None:
        return []
    buy_target = round(close * (1 - sigma), 1)
    sell_base = max(ma5, close * (1 + sigma)) if ma5 is not None else close * (1 + sigma)
    sell_target = round(sell_base, 1)
    buy_pct = (buy_target - close) / close * 100
    sell_pct = (sell_target - close) / close * 100
    return [
        {"label": "🟢 至買進區", "target": buy_target,
         "delta_pct": round(buy_pct, 2),
         "note": f"再跌 {abs(buy_pct):.1f}% 至 {buy_target} 進入積極買進區"},
        {"label": "🔴 至賣出區", "target": sell_target,
         "delta_pct": round(sell_pct, 2),
         "note": f"再漲 {abs(sell_pct):.1f}% 至 {sell_target} 進入反彈賣出區"},
    ]


def _compute_steps(window, code, market=MARKET_TWSE, t86_cached_only=False):
    """Run the 7 step checks on a 20-day window. Returns the steps list with
    UI step numbers (1..7) assigned. Window must have ≥2 rows."""
    last = window[-1]
    prev = window[-2]
    s1 = _step_1_market(window, last)
    s2 = _step_2_trend(window, last)
    s3 = _step_3_momentum(window, last, prev)
    s6 = _step_6_holding(window, last, prev)
    s4 = _step_4_volume(window, last, prev, s3["light"], s6["light"])
    s7 = _step_7_exit(window, last, prev)
    if code:
        s8 = _step_8_institutional(window, code, market=market,
                                   cached_only=t86_cached_only)
    else:
        s8 = {"step": 0, "title": "法人認養", "condition": "5 日三大法人籌碼",
              "light": "gray", "detail": "需股票代碼"}
    steps = [s1, s2, s3, s4, s6, s7, s8]
    for i, s in enumerate(steps, 1):
        s["step"] = i
    return steps


HISTORY_DAYS = 15


def _history_lights(full_rows, code, market=MARKET_TWSE, days=HISTORY_DAYS):
    """For each of the last `days` trading days, recompute the 7 lights and
    the overall summary light using a 20-day window ending at that day.
    Step 7 (法人) reads only from cached T86 to avoid extra network calls;
    missing dates show gray."""
    if not full_rows:
        return []
    out = []
    start = max(1, len(full_rows) - days)
    for end_idx in range(start, len(full_rows)):
        sub = full_rows[: end_idx + 1]
        window = sub[-20:]
        if len(window) < 2:
            continue
        steps = _compute_steps(window, code, market=market, t86_cached_only=True)
        summary = _summary(steps)
        out.append({
            "date": window[-1]["date"],
            "lights": [s["light"] for s in steps],
            "overall": summary["light"],
            "overall_label": summary["label"],
        })
    return out


def compute_dashboard(full_rows: list[dict], code: str | None = None,
                      market: str = MARKET_TWSE) -> dict:
    if not full_rows:
        return {"as_of": None, "sigma": None, "steps": [],
                "summary": None, "price_zones": None, "distance": [],
                "history": []}
    window = full_rows[-20:]
    last = window[-1]
    if len(window) < 2:
        return {"as_of": last["date"], "sigma": None, "steps": [],
                "summary": None, "price_zones": None, "distance": [],
                "history": []}
    sigma = _sigma(window)
    steps = _compute_steps(window, code, market=market, t86_cached_only=False)
    s6 = steps[4]  # 持有

    summary = _summary(steps)
    zones = _price_zones(summary, last, sigma, s6["light"])
    distance = _distance(last, sigma) + _stoploss_levels(last, sigma)
    history = _history_lights(full_rows, code, market=market)

    return {
        "as_of": last["date"],
        "sigma": sigma,
        "steps": steps,
        "summary": summary,
        "price_zones": zones,
        "distance": distance,
        "history": history,
    }


# ---- Company info (TWSE OpenAPI) -------------------------------------------
#
# Pulls 公司簡稱 + 產業類別 from the TWSE listed-company basic-info endpoint
# once per day. The full list is ~1MB and rarely changes, so a single daily
# fetch is plenty.

COMPANY_INFO_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
COMPANY_INFO_URL_OTC = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
COMPANY_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-web/1.0)",
    "Accept": "application/json",
}

# TWSE 產業別代碼 → 中文名稱。OpenAPI 回傳的是代碼,自己對照。
INDUSTRY_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造", "15": "航運業", "16": "觀光餐旅", "17": "金融保險業",
    "18": "貿易百貨", "19": "綜合", "20": "其他業",
    "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業",
    "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業",
    "31": "其他電子業", "32": "文化創意業", "33": "農業科技業", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "管理股票", "91": "存託憑證",
}

_company_lock = threading.Lock()
_company_cache_mem = {"date": None, "twse": {}, "otc": {}}


def _company_cache_path(market: str = MARKET_TWSE) -> Path:
    suffix = "" if market == MARKET_TWSE else "_otc"
    return CACHE_DIR / f"companies{suffix}_{_today_tag()}.json"


def _fetch_companies_twse() -> dict:
    cache = _company_cache_path(MARKET_TWSE)
    if cache.exists():
        try:
            with cache.open() as f:
                rows = json.load(f)
            return {r["code"]: r for r in rows}
        except (OSError, json.JSONDecodeError):
            pass
    with _company_lock:
        if cache.exists():
            try:
                with cache.open() as f:
                    rows = json.load(f)
                return {r["code"]: r for r in rows}
            except (OSError, json.JSONDecodeError):
                pass
        try:
            resp = requests.get(COMPANY_INFO_URL,
                                headers=COMPANY_HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
            raw = resp.json()
        except (requests.RequestException, ValueError):
            return {}
        out = {}
        for row in raw:
            code = (row.get("公司代號") or "").strip()
            if not code:
                continue
            ind_code = (row.get("產業別") or "").strip()
            ind_name = INDUSTRY_NAMES.get(ind_code, ind_code)
            out[code] = {
                "code": code,
                "short_name": (row.get("公司簡稱") or "").strip(),
                "industry": ind_name,
                "industry_code": ind_code,
                "market": MARKET_TWSE,
            }
        try:
            with cache.open("w") as f:
                json.dump(list(out.values()), f, ensure_ascii=False)
        except OSError:
            pass
        return out


def _fetch_companies_otc() -> dict:
    cache = _company_cache_path(MARKET_OTC)
    if cache.exists():
        try:
            with cache.open() as f:
                rows = json.load(f)
            return {r["code"]: r for r in rows}
        except (OSError, json.JSONDecodeError):
            pass
    with _company_lock:
        if cache.exists():
            try:
                with cache.open() as f:
                    rows = json.load(f)
                return {r["code"]: r for r in rows}
            except (OSError, json.JSONDecodeError):
                pass
        try:
            resp = requests.get(COMPANY_INFO_URL_OTC,
                                headers=COMPANY_HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
            raw = resp.json()
        except (requests.RequestException, ValueError):
            return {}
        out = {}
        for row in raw:
            code = (row.get("SecuritiesCompanyCode") or "").strip()
            if not code:
                continue
            ind_code = (row.get("SecuritiesIndustryCode") or "").strip()
            # TPEx codes are 1-2 digits; pad to match TWSE 2-digit map.
            ind_key = ind_code.zfill(2) if ind_code.isdigit() else ind_code
            ind_name = INDUSTRY_NAMES.get(ind_key, ind_code)
            out[code] = {
                "code": code,
                "short_name": (row.get("CompanyAbbreviation") or "").strip(),
                "industry": ind_name,
                "industry_code": ind_code,
                "market": MARKET_OTC,
            }
        try:
            with cache.open("w") as f:
                json.dump(list(out.values()), f, ensure_ascii=False)
        except OSError:
            pass
        return out


def _refresh_company_cache() -> None:
    today = _today_tag()
    if _company_cache_mem["date"] != today:
        _company_cache_mem["twse"] = _fetch_companies_twse()
        _company_cache_mem["otc"] = _fetch_companies_otc()
        _company_cache_mem["date"] = today


def _company_info(code: str) -> dict:
    _refresh_company_cache()
    info = _company_cache_mem["twse"].get(code)
    if info:
        return info
    info = _company_cache_mem["otc"].get(code)
    if info:
        return info
    # Unknown code — fall back to whatever today's stock cache claims.
    market = _stock_cache_market(code) or ""
    return {"code": code, "short_name": "", "industry": "",
            "industry_code": "", "market": market}


def _market_for(code: str) -> str | None:
    """Look up a code's market via the daily company maps. Returns
    MARKET_TWSE / MARKET_OTC, or None if the code isn't in either."""
    _refresh_company_cache()
    if code in _company_cache_mem["twse"]:
        return MARKET_TWSE
    if code in _company_cache_mem["otc"]:
        return MARKET_OTC
    # If we already fetched data for this code today, trust the persisted market.
    return _stock_cache_market(code)


# ---- Watchlist persistence --------------------------------------------------

WATCHLIST_FILE = Path(__file__).resolve().parent / "watchlist.json"
_watchlist_lock = threading.Lock()


def _load_watchlist():
    if not WATCHLIST_FILE.exists():
        return []
    try:
        with WATCHLIST_FILE.open() as f:
            return json.load(f).get("codes", [])
    except (OSError, json.JSONDecodeError):
        return []


def _save_watchlist(codes):
    with _watchlist_lock:
        with WATCHLIST_FILE.open("w") as f:
            json.dump({"codes": codes}, f, ensure_ascii=False, indent=2)


def _validate_code(code: str):
    if not code.isdigit() or not (4 <= len(code) <= 6):
        raise HTTPException(400, "code must be a 4-6 digit TWSE stock number")


def _watchlist_item(code: str) -> dict:
    """Read today's cache (if any) and return a compact summary for the watchlist."""
    info = _company_info(code)
    market = info.get("market") or _stock_cache_market(code) or MARKET_TWSE
    base = {
        "code": code, "cached": False,
        "short_name": info["short_name"],
        "industry": info["industry"],
        "market": market,
    }
    cache = _stock_cache(code)
    if not cache.exists():
        return base
    try:
        with cache.open() as f:
            payload = json.load(f)
        full = payload.get("rows") or []
        cached_market = payload.get("market") or market
        if not full:
            return base
        last = full[-1]
        dash = compute_dashboard(full, code, market=cached_market)
        return {
            **base,
            "market": cached_market,
            "cached": True,
            "as_of": last.get("date"),
            "close": last.get("close"),
            "change_pct": last.get("change_pct"),
            "high": last.get("high"),
            "low": last.get("low"),
            "lots": last.get("lots"),
            "summary": dash["summary"],
            "step_lights": [s["light"] for s in dash["steps"]],
        }
    except (OSError, json.JSONDecodeError, KeyError):
        return base


# ---- App --------------------------------------------------------------------

app = FastAPI(title="TWSE Stock Viewer")


@app.get("/api/stock/{code}")
def get_stock(code: str, rows: int = 30):
    _validate_code(code)
    if rows < 1 or rows > 500:
        raise HTTPException(400, "rows must be between 1 and 500")

    full = _load_stock(code, rows)
    if not full:
        raise HTTPException(404, f"no data found for {code}")
    info = _company_info(code)
    market = _stock_cache_market(code) or info.get("market") or MARKET_TWSE
    if not info.get("market"):
        info = {**info, "market": market}
    return {
        "code": code,
        "info": info,
        "market": market,
        "rows": full[-rows:],
        "dashboard": compute_dashboard(full, code, market=market),
    }


@app.get("/api/watchlist")
def watchlist_get():
    codes = _load_watchlist()
    return {"codes": codes, "items": [_watchlist_item(c) for c in codes]}


@app.post("/api/watchlist/reorder")
def watchlist_reorder(codes: list[str] = Body(..., embed=True)):
    """Persist a new order for the watchlist. Unknown codes ignored;
    existing codes missing from the request are appended at the end."""
    current = _load_watchlist()
    current_set = set(current)
    seen = set()
    new_order = []
    for c in codes:
        if c in current_set and c not in seen:
            new_order.append(c)
            seen.add(c)
    for c in current:
        if c not in seen:
            new_order.append(c)
    _save_watchlist(new_order)
    return {"ok": True, "codes": new_order}


@app.put("/api/watchlist/{code}")
def watchlist_add(code: str):
    _validate_code(code)
    codes = _load_watchlist()
    if code not in codes:
        codes.append(code)
        _save_watchlist(codes)
    return {"ok": True, "codes": codes, "item": _watchlist_item(code)}


@app.delete("/api/watchlist/{code}")
def watchlist_remove(code: str):
    _validate_code(code)
    codes = _load_watchlist()
    if code in codes:
        codes.remove(code)
        _save_watchlist(codes)
    for f in CACHE_DIR.glob(f"{code}_*.json"):
        f.unlink(missing_ok=True)
    return {"ok": True, "codes": codes}


@app.post("/api/watchlist/{code}/refresh")
def watchlist_refresh(code: str):
    """Drop today's cache for this stock and re-fetch from TWSE.

    Synchronous and slow (~30-60s for first fetch). The frontend should
    show a loading state while this runs.
    """
    _validate_code(code)
    cache = _stock_cache(code)
    if cache.exists():
        cache.unlink()
    full = _load_stock(code, 30)
    if not full:
        raise HTTPException(404, f"no data for {code}")
    return {"ok": True, "item": _watchlist_item(code)}


@app.get("/api/taiex/today")
def taiex_today_get():
    today_iso = _today_iso()
    manual = _load_taiex_manual()
    if today_iso in manual:
        return {"date": today_iso, "close": manual[today_iso], "source": "manual"}
    cache = _taiex_cache()
    if cache.exists():
        try:
            with cache.open() as f:
                data = json.load(f)
            v = data.get(today_iso)
            if v is not None:
                return {"date": today_iso, "close": v, "source": "auto"}
        except (OSError, json.JSONDecodeError):
            pass
    return {"date": today_iso, "close": None, "source": None}


@app.put("/api/taiex/today")
def taiex_today_put(payload: dict = Body(...)):
    """Manually set today's TAIEX close. Patches existing same-day stock caches
    whose last row's `taiex` field is missing."""
    raw = payload.get("close") if isinstance(payload, dict) else None
    try:
        close = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "close must be a number")
    if close <= 0 or close > 100000:
        raise HTTPException(400, "close out of range")
    today_iso = _today_iso()
    with _taiex_manual_lock:
        manual = _load_taiex_manual()
        manual[today_iso] = close
        _save_taiex_manual(manual)
        # Also write into today's auto cache so next _load_taiex sees it
        # without overlay (defensive — overlay still applies regardless).
        cache = _taiex_cache()
        try:
            data = {}
            if cache.exists():
                with cache.open() as f:
                    data = json.load(f)
            data[today_iso] = close
            with cache.open("w") as f:
                json.dump(data, f)
        except (OSError, json.JSONDecodeError):
            pass
    # Patch existing per-stock caches for today: if last row's date == today
    # and taiex is None, fill it in. Restrict to numeric-prefixed files
    # (stock codes are 4-6 digits) so taiex/t86/companies caches are skipped.
    today_tag = _today_tag()
    patched = 0
    for stock_cache in CACHE_DIR.glob(f"*_{today_tag}.json"):
        prefix = stock_cache.stem.rsplit("_", 1)[0]
        if not (prefix.isdigit() and 4 <= len(prefix) <= 6):
            continue
        try:
            with stock_cache.open() as f:
                stock_payload = json.load(f)
            if not isinstance(stock_payload, dict):
                continue
            rows = stock_payload.get("rows") or []
            if not rows:
                continue
            last = rows[-1]
            if last.get("date") == today_iso and last.get("taiex") is None:
                last["taiex"] = close
                with stock_cache.open("w") as f:
                    json.dump(stock_payload, f)
                patched += 1
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return {"ok": True, "date": today_iso, "close": close,
            "source": "manual", "patched": patched}


@app.delete("/api/taiex/today")
def taiex_today_delete():
    """Clear today's manual override (does not touch auto cache or patched stock rows)."""
    today_iso = _today_iso()
    with _taiex_manual_lock:
        manual = _load_taiex_manual()
        if today_iso in manual:
            del manual[today_iso]
            _save_taiex_manual(manual)
    return {"ok": True, "date": today_iso}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
