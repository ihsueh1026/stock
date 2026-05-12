"""Fetch annual fundamentals (EPS, revenue, margins, equity) from MOPS.

Uses the 'company overview' endpoint t146sb05, which returns the last
3 fiscal years' 簡明 statements in a single HTML response:
  - 簡明資產負債:  total assets / liabilities / equity / 每股淨值
  - 簡明綜合損益:  revenue / operating profit / pre-tax profit / EPS
  - 簡明現金流量:  operating / investing / financing CF

We derive margin %s (營業利益率, 稅前淨利率) and a pre-tax ROE proxy
server-side so the front end can render compact ratios without
re-doing arithmetic. Per-year amounts are in 千元 (the MOPS native
unit) — convert to 億 / 兆 for display.

Cache: `stock_web/cache/fund_{code}_{YYYYMMDD}.json` — same daily
auto-purge cycle as other dated caches. Annual data only changes
once a year so this is effectively over-fetched, but keeping a
familiar daily rhythm avoids special-casing the purger.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

MOPS_BASE = "https://mopsov.twse.com.tw"
MOPS_WARMUP = f"{MOPS_BASE}/mops/web/t146sb05"
MOPS_AJAX = f"{MOPS_BASE}/mops/web/ajax_t146sb05"

REQUEST_TIMEOUT_SEC = 30
REQUEST_INTERVAL_SEC = 2
MAX_RETRIES = 2          # MOPS occasionally times out on the read side
RETRY_BACKOFF_SEC = 3

MARKET_TWSE = "twse"
MARKET_OTC = "otc"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# Row-label prefixes we capture. MOPS sometimes appends "(損失)" /
# "(淨損)" to negative-line items, so we match by startswith.
LABELS: dict[str, str] = {
    "資產總計":             "total_assets",
    "負債總計":             "total_liabilities",
    "權益總計":             "total_equity",
    "每股淨值":             "book_value_per_share",
    "營業收入":             "revenue",
    "營業利益":             "operating_profit",   # 營業利益(損失)
    "稅前淨利":             "pretax_profit",      # 稅前淨利(淨損)
    "基本每股盈餘":         "eps",
    "營業活動之淨現金流入": "cf_operating",
    "投資活動之淨現金流入": "cf_investing",
    "籌資活動之淨現金流入": "cf_financing",
}

_session: requests.Session | None = None
_last_req_ts: float = 0.0


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(MOPS_WARMUP, timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException:
            pass
        _session = s
    return _session


def _throttle() -> None:
    global _last_req_ts
    elapsed = time.time() - _last_req_ts
    if elapsed < REQUEST_INTERVAL_SEC:
        time.sleep(REQUEST_INTERVAL_SEC - elapsed)
    _last_req_ts = time.time()


_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
# Period labels MOPS emits: "114年度" or "115Q1". The number of
# columns varies — when the latest Q has been published the header
# becomes e.g. "115Q1 / 114Q1 / 114年度 / 113年度" (4 cols), otherwise
# it stays at the plain 3-year annual layout.
_PERIOD_RE = re.compile(r"(\d{2,3})\s*(Q\d|年度)")


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("\xa0", " ").strip()


def _to_num(s: str):
    s = s.replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return None


def _match_label(label: str) -> str | None:
    for prefix, canon in LABELS.items():
        if label.startswith(prefix):
            return canon
    return None


def _parse_period(label: str) -> dict[str, Any] | None:
    """Convert raw column label into a structured period.

    Examples:
      '114年度' → {label: '2025', year: 2025, quarter: None, type: 'annual'}
      '115Q1'   → {label: '2026Q1', year: 2026, quarter: 1, type: 'quarter'}
    """
    m = _PERIOD_RE.search(label)
    if not m:
        return None
    roc = int(m.group(1))
    suffix = m.group(2)
    year = roc + 1911
    if suffix.startswith("Q"):
        q = int(suffix[1])
        return {"label": f"{year}Q{q}", "year": year,
                "quarter": q, "type": "quarter"}
    return {"label": str(year), "year": year,
            "quarter": None, "type": "annual"}


def _parse(html: str) -> dict[str, Any]:
    """Pull the period header + per-metric N-column data from t146sb05.

    Row shapes:
      [section, label, v1..vN]   when the section td starts (rowspan=4)
      [label, v1..vN]            subsequent rows in the section
    """
    periods: list[dict[str, Any]] = []
    metrics: dict[str, list] = {}
    for tr in _TR_RE.finditer(html):
        cells = [_strip(c) for c in _TD_RE.findall(tr.group(1))]
        if not cells:
            continue
        # Period header row: starts with '期 別' / '期別' (the value is
        # rendered with a colspan=2 cell, so cells[0] is that literal).
        # Earlier announcements above this table also reference '年度' in
        # their subjects (e.g. '本公司董事會通過115年度第1季合併財務
        # 報表'), so we anchor on the period-row marker rather than any
        # '年度' match.
        if not periods and ("期 別" in cells[0] or "期別" in cells[0]):
            for c in cells[1:]:
                p = _parse_period(c)
                if p is not None:
                    periods.append(p)
            continue
        if not periods:
            continue
        n = len(periods)
        if len(cells) == n + 1:
            label, *vals = cells
        elif len(cells) == n + 2:
            _section, label, *vals = cells
        else:
            continue
        if len(vals) != n:
            continue
        canon = _match_label(label)
        if canon is None:
            continue
        metrics[canon] = [_to_num(v) for v in vals]

    if not periods or not metrics:
        return {}

    # Attach metrics to each period entry + derived ratios.
    for i, p in enumerate(periods):
        for key, vals in metrics.items():
            p[key] = vals[i] if i < len(vals) else None
        rev = p.get("revenue")
        op = p.get("operating_profit")
        pre = p.get("pretax_profit")
        eq = p.get("total_equity")
        if rev:
            if op is not None:
                p["op_margin_pct"] = round(op / rev * 100, 2)
            if pre is not None:
                p["pretax_margin_pct"] = round(pre / rev * 100, 2)
        if eq and pre is not None:
            p["pretax_roe_pct"] = round(pre / eq * 100, 2)

    # EPS YoY — MOPS lists same-type periods in newest-first order,
    # so consecutive same-type entries are a valid YoY pair.
    by_type: dict[str, list[dict[str, Any]]] = {}
    for p in periods:
        by_type.setdefault(p["type"], []).append(p)
    for items in by_type.values():
        for i in range(len(items) - 1):
            cur = items[i].get("eps")
            prev = items[i + 1].get("eps")
            if cur is not None and prev:
                items[i]["eps_yoy_pct"] = round((cur - prev) / prev * 100, 2)

    return {"periods": periods}


def _cache_path(code: str, today: date | None = None) -> Path:
    today = today or date.today()
    return CACHE_DIR / f"fund_{code}_{today.strftime('%Y%m%d')}.json"


def fetch(code: str, market: str = MARKET_TWSE,
          *, force: bool = False) -> dict[str, Any]:
    global _session
    p = _cache_path(code)
    if not force and p.exists():
        try:
            with p.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    typek = "sii" if market == MARKET_TWSE else "otc"
    today = date.today()
    payload = {
        "step": "1",
        "firstin": "true",
        "off": "1",
        "isnew": "false",
        "TYPEK": typek,
        "co_id": code,
        "year": str(today.year - 1911),
    }

    body: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        s = _get_session()
        try:
            r = s.post(MOPS_AJAX, data=payload,
                       headers={"Referer": MOPS_WARMUP},
                       timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                print(f"  [retry {attempt + 1}/{MAX_RETRIES}] {code} "
                      f"({e}); sleeping {RETRY_BACKOFF_SEC}s",
                      file=sys.stderr)
                time.sleep(RETRY_BACKOFF_SEC)
                # Force a fresh session on next attempt — the previous
                # one may have a dead keepalive socket.
                _session = None
                continue
            print(f"  [warn] fundamentals fetch failed {code}: {e}",
                  file=sys.stderr)
            return {"code": code, "market": market, "available": False}
        if (r.status_code == 200
                and "頁面無法執行" not in r.text
                and "每股淨值" in r.text):
            body = r.text
            break
        if attempt < MAX_RETRIES:
            print(f"  [retry {attempt + 1}/{MAX_RETRIES}] {code} "
                  f"(HTTP {r.status_code}, empty body); "
                  f"sleeping {RETRY_BACKOFF_SEC}s", file=sys.stderr)
            time.sleep(RETRY_BACKOFF_SEC)
    if body is None:
        return {"code": code, "market": market, "available": False}
    r_text = body

    parsed = _parse(r_text)
    if not parsed.get("periods"):
        return {"code": code, "market": market, "available": False}

    out = {
        "code": code,
        "market": market,
        "available": True,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        **parsed,
    }
    try:
        with p.open("w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  [warn] fund cache write failed {p}: {e}", file=sys.stderr)
    return out


def latest_annual_eps(periods: list[dict[str, Any]]) -> float | None:
    """Return the EPS from the most recent ANNUAL period (skip quarterly)."""
    for p in periods or []:
        if p.get("type") == "annual" and p.get("eps") is not None:
            return p["eps"]
    return None


def per_for(latest_eps: float | None,
            last_close: float | None) -> float | None:
    """Trailing P/E using the most recent published annual EPS."""
    if not (latest_eps and last_close):
        return None
    if latest_eps <= 0:
        return None  # negative or zero EPS → P/E is meaningless
    return round(last_close / latest_eps, 1)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--market", default="twse", choices=["twse", "otc"])
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    for code in args.codes:
        d = fetch(code, market=args.market, force=args.no_cache)
        if not d.get("available"):
            print(f"{code}: no data")
            continue
        print(f"\n=== {code} ({args.market}) ===")
        for p in d["periods"]:
            eps = p.get("eps")
            rev_yi = (p.get("revenue") or 0) / 1e5  # 千元 → 億
            op_m = p.get("op_margin_pct")
            pre_m = p.get("pretax_margin_pct")
            bvps = p.get("book_value_per_share")
            yoy = p.get("eps_yoy_pct")
            yoy_str = f"{yoy:+.1f}%" if yoy is not None else "  n/a"
            print(
                f"  {p['label']:>8}  "
                f"EPS={eps!s:>7}  ({yoy_str:>7})  "
                f"營收={rev_yi:>9,.0f}億  "
                f"營益率={op_m!s:>5}%  "
                f"淨利率={pre_m!s:>5}%  "
                f"每股淨值={bvps!s}"
            )


if __name__ == "__main__":
    main()
