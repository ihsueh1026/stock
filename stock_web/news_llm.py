"""LLM summarization + sentiment for MOPS material announcements.

Calls Anthropic's Claude API (Haiku) to label each 重訊 as 利多/利空/中性
and produce a brief Chinese summary. The system prompt is marked
cache_control:ephemeral so repeat calls within a 5-minute window are
~90% cheaper.

Designed to fail soft:
  - If `anthropic` SDK is missing → return items unchanged.
  - If `ANTHROPIC_API_KEY` is unset → return items unchanged.
  - If the API call fails → mark items with sentiment='?' and continue.

Public entry: `annotate(items)` mutates items in-place, adding
`sentiment` and `summary` to each. Items that already have both fields
are skipped, so calling this against a partially-annotated cache is
cheap (no redundant LLM call).

Requires: pip install anthropic ; export ANTHROPIC_API_KEY=...
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

# Haiku 4.5 — cheap, fast, sufficient for 1-line classification.
# Swap to Sonnet only if sentiment quality is visibly poor.
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2048

SYSTEM_PROMPT = """\
你是台股重大訊息分析助手。輸入是某支股票的多則重大訊息(主旨),
請逐則判斷對該股票股價的可能影響並產生簡短中文摘要。

輸出規則(必須嚴格遵守):
1. 用 JSON 陣列回覆,順序與輸入完全一致,每則一個物件。
2. 每個物件必須有 "sentiment" 與 "summary" 兩個欄位。
3. sentiment 只能是以下三個字串之一:
     "利多" — 對股價偏正面(營收成長、新合約、買回庫藏股、財報優異等)
     "利空" — 對股價偏負面(營收衰退、訴訟、處分資產損失、警示等)
     "中性" — 例行公告、子公司一般性投資/處分、董事變動、技術性公告等
4. summary 為 30 字以內的中文摘要,只描述事實,不做投資建議,不要加引號。
5. 不要輸出 JSON 以外的任何文字、註解、markdown code fence。

範例輸入:
[{"idx": 0, "title": "公告本公司115年4月份營收"},
 {"idx": 1, "title": "本公司代子公司公告處分有價證券"}]
範例輸出:
[{"sentiment": "中性", "summary": "公告115年4月營收"},
 {"sentiment": "中性", "summary": "子公司處分有價證券"}]
"""


def _has_sdk() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def is_available() -> bool:
    """Whether LLM summarization can actually run right now."""
    return _has_sdk() and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _parse_json_array(text: str) -> list[dict] | None:
    text = text.strip()
    # Strip markdown code fence if the model adds one despite instructions
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        v = json.loads(text)
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, list) else None


def annotate(items: list[dict[str, Any]],
             code: str = "") -> list[dict[str, Any]]:
    """Add 'sentiment' and 'summary' fields to each item in-place.

    Items that already have both fields are passed through (so this is
    cheap to re-call against the same cache). On any failure, fills
    with sentiment='?' / summary='' rather than raising — the caller
    can still render the raw title.
    """
    if not items:
        return items
    todo_idx = [
        i for i, it in enumerate(items)
        if not (it.get("sentiment") and it.get("summary"))
    ]
    if not todo_idx:
        return items
    if not is_available():
        # Mark as 'unprocessed' so callers know vs. a failed LLM call
        for i in todo_idx:
            items[i].setdefault("sentiment", "")
            items[i].setdefault("summary", "")
        return items

    payload = [
        {"idx": i, "title": items[i].get("title", "")}
        for i in todo_idx
    ]
    user_msg = json.dumps(payload, ensure_ascii=False)

    try:
        from anthropic import Anthropic
        client = Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
        text_block = next(
            (b for b in resp.content if getattr(b, "type", "") == "text"),
            None,
        )
        if text_block is None:
            raise RuntimeError("no text block in response")
        parsed = _parse_json_array(text_block.text)
        if parsed is None:
            raise RuntimeError(
                f"unparseable JSON: {text_block.text[:200]!r}"
            )
    except Exception as e:
        print(f"  [warn] LLM annotate failed for {code}: {e}",
              file=sys.stderr)
        for i in todo_idx:
            items[i]["sentiment"] = "?"
            items[i]["summary"] = ""
        return items

    for slot, ann in zip(todo_idx, parsed):
        if not isinstance(ann, dict):
            items[slot]["sentiment"] = "?"
            items[slot]["summary"] = ""
            continue
        s = str(ann.get("sentiment") or "?").strip()
        if s not in ("利多", "利空", "中性"):
            s = "?"
        items[slot]["sentiment"] = s
        items[slot]["summary"] = str(ann.get("summary") or "").strip()
    # If LLM returned fewer items than asked, fill the gap
    for slot in todo_idx[len(parsed):]:
        items[slot]["sentiment"] = "?"
        items[slot]["summary"] = ""
    return items


def main() -> None:
    """CLI smoke test: read items from the latest news cache and annotate."""
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    args = ap.parse_args()
    cache_dir = Path(__file__).resolve().parent / "cache"
    candidates = sorted(
        cache_dir.glob(f"news_{args.code}_*.json"), reverse=True
    )
    if not candidates:
        print(f"no news cache for {args.code}; run news_fetcher first")
        return
    p = candidates[0]
    with p.open() as f:
        data = json.load(f)
    print(f"annotating {len(data['items'])} items from {p.name}")
    print(f"LLM available: {is_available()}")
    annotate(data["items"], code=args.code)
    for it in data["items"]:
        print(f"  [{it.get('sentiment','?'):>2}] {it.get('summary','')}")
        print(f"        title: {it['title']}")
    with p.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
