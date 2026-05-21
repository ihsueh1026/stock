#!/usr/bin/env python3
"""Scraper for TWSE broker buy/sell daily report (bsr.twse.com.tw).

Workflow for each stock code:
  1. GET bsMenu.aspx to obtain ASP.NET hidden fields and the CAPTCHA image.
  2. OCR the 5-character CAPTCHA with ddddocr.
  3. POST the form (stock code + CAPTCHA). Retry on CAPTCHA failure.
  4. On success, follow the HyperLink_DownloadCSV href to download the CSV.
  5. Decode the Big5 payload and save it as UTF-8.

Public market data only; please respect the site's terms of use and keep
request volume low.
"""

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import ddddocr
except ImportError:
    sys.exit("Missing dependency 'ddddocr'. Install with: pip3 install -r requirements.txt")

BASE_URL = "https://bsr.twse.com.tw/bshtm/"
MENU_URL = BASE_URL + "bsMenu.aspx"
HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def make_ocr():
    """Build a single reusable OCR engine (model load is slow)."""
    return ddddocr.DdddOcr(show_ad=False)


def _input_value(soup, name):
    el = soup.select_one(f"input[name='{name}']")
    return el["value"] if el and el.has_attr("value") else ""


def fetch_form(session):
    """GET the menu page and return (hidden_fields, captcha_image_url)."""
    resp = session.get(MENU_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    hidden = {name: _input_value(soup, name) for name in HIDDEN_FIELDS}
    img = soup.select_one("#Panel_bshtm img")
    if img is None or not img.get("src"):
        raise RuntimeError("Could not locate CAPTCHA image on the page.")
    return hidden, urljoin(BASE_URL, img["src"])


def solve_captcha(session, ocr, img_url):
    """Download the CAPTCHA image and return the recognised 5-char code."""
    img_bytes = session.get(img_url, timeout=30).content
    code = ocr.classification(img_bytes)
    return re.sub(r"[^A-Za-z0-9]", "", code)


def submit_query(session, hidden, stock_no, captcha):
    """POST the query form. Return (success, error_text, result_soup)."""
    data = {
        **hidden,
        "RadioButton_Normal": "RadioButton_Normal",
        "TextBox_Stkno": stock_no,
        "CaptchaControl1": captcha,
        "btnOK": "查詢",
    }
    resp = session.post(MENU_URL, data=data, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    err_el = soup.select_one("#Label_ErrorMsg")
    err_text = err_el.get_text(strip=True) if err_el else ""
    return (not err_text), err_text, soup


def download_csv(session, result_soup):
    """Follow the download link in the result page; return decoded CSV text."""
    link = result_soup.select_one("#HyperLink_DownloadCSV")
    if link is None or not link.get("href"):
        return None
    csv_url = urljoin(BASE_URL, link["href"])
    resp = session.get(csv_url, timeout=60)
    resp.raise_for_status()
    # Official payload is Big5-encoded; fall back permissively on stray bytes.
    return resp.content.decode("big5", errors="replace")


def scrape_stock(session, ocr, stock_no, max_retries=10):
    """Run the full retry loop for one stock code. Return CSV text or None."""
    for attempt in range(1, max_retries + 1):
        hidden, img_url = fetch_form(session)
        captcha = solve_captcha(session, ocr, img_url)
        if len(captcha) != 5:
            print(f"  [{attempt}/{max_retries}] CAPTCHA OCR gave {captcha!r}, retrying")
            continue
        ok, err_text, soup = submit_query(session, hidden, stock_no, captcha)
        if not ok:
            print(f"  [{attempt}/{max_retries}] rejected: {err_text}")
            continue
        csv_text = download_csv(session, soup)
        if csv_text is None:
            print(f"  No data / no download link for {stock_no}.")
            return None
        return csv_text
    print(f"  Gave up on {stock_no} after {max_retries} attempts.")
    return None


def load_stock_codes(args):
    """Collect stock codes from positional args and/or an input file."""
    codes = list(args.stocks)
    if args.input:
        for raw in Path(args.input).read_text(encoding="utf-8").splitlines():
            code = raw.strip()
            if code and not code.startswith("#"):
                codes.append(code)
    # De-duplicate while preserving order.
    seen, ordered = set(), []
    for code in codes:
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered


def main():
    parser = argparse.ArgumentParser(
        description="Download TWSE broker buy/sell daily reports as CSV."
    )
    parser.add_argument("stocks", nargs="*", help="One or more stock codes, e.g. 2330 2317")
    parser.add_argument("-i", "--input", help="Text file with one stock code per line")
    parser.add_argument("-o", "--outdir", default="output", help="Output directory (default: output)")
    parser.add_argument("-r", "--retries", type=int, default=10, help="Max CAPTCHA retries per stock")
    parser.add_argument("-d", "--delay", type=float, default=2.0, help="Seconds to wait between stocks")
    args = parser.parse_args()

    codes = load_stock_codes(args)
    if not codes:
        parser.error("No stock codes provided. Pass codes as arguments or via --input.")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Loading OCR model... ({len(codes)} stock(s) to fetch)")
    ocr = make_ocr()
    session = requests.Session()
    session.headers.update(HEADERS)

    succeeded, failed = [], []
    for idx, code in enumerate(codes, 1):
        print(f"[{idx}/{len(codes)}] {code}")
        try:
            csv_text = scrape_stock(session, ocr, code, args.retries)
        except requests.RequestException as exc:
            print(f"  Network error: {exc}")
            csv_text = None
        if csv_text:
            out_path = outdir / f"{code}.csv"
            # UTF-8 BOM so Excel opens the Chinese headers correctly.
            out_path.write_text(csv_text, encoding="utf-8-sig")
            print(f"  Saved {out_path} ({len(csv_text)} chars)")
            succeeded.append(code)
        else:
            failed.append(code)
        if idx < len(codes) and args.delay > 0:
            time.sleep(args.delay)

    print(f"\nDone. {len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        print("Failed:", " ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
