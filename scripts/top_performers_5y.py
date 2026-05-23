#!/usr/bin/env python3
"""
扫描全A股，按近5年涨幅倍数倒排，找出涨幅最高的股票。
用法: python top_performers_5y.py [数量]
"""

import sys
import time
import requests
import re
import numpy as np
from datetime import datetime, timedelta


def get_all_stocks() -> list[tuple[str, str]]:
    """Get all A-share stock codes from shdjt.com, names fetched later."""
    try:
        r = requests.get(
            "http://www.shdjt.com/js/lib/astock.js",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.encoding = "gbk"
        # Just extract codes: ~CODE`...~
        codes = re.findall(r"~(\d{6})`", r.text)
        # Filter: only Shanghai/Shenzhen/ChiNext (0,3,6 prefix)
        codes = [c for c in codes if c[0] in "036"]
        # Remove duplicates while preserving order
        seen = set()
        unique = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return [(c, "") for c in unique]
    except Exception as e:
        print(f"获取股票列表失败: {e}")
        return []


def fetch_price_name(code: str) -> tuple[float | None, str]:
    """Quick fetch current price and name from Tencent API."""
    prefix = "sh" if code.startswith("6") else "sz"
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={prefix}{code}", timeout=3)
        r.encoding = "gbk"
        m = re.search(r'="(.+?)"', r.text)
        if m:
            f = m.group(1).split("~")
            price = float(f[3]) if len(f) > 3 and f[3] else 0
            name = f[1] if len(f) > 1 else ""
            return (price if price > 0 else None, name)
    except Exception:
        pass
    return (None, "")


def fetch_kline(code: str, days: int = 1300) -> list[float] | None:
    """Fetch daily K-line from Tencent API (approx 5 years, qfq)."""
    prefix = "sh" if code.startswith("6") else "sz"
    symbol = f"{prefix}{code}"
    try:
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={symbol},day,,,{days},qfq"
        )
        r = requests.get(url, timeout=10)
        data = r.json()

        # Navigate: data -> symbol -> qfqday or day
        inner = data.get("data", {})
        if symbol in inner:
            stock_data = inner[symbol]
            klines = stock_data.get("qfqday") or stock_data.get("day") or []
        else:
            return None

        closes = [float(k[2]) for k in klines if len(k) > 2 and k[2]]
        return closes if len(closes) >= 200 else None
    except Exception:
        return None


def calc_return(closes: list[float]) -> float | None:
    """Calculate 5-year return multiple. returns (latest / 5years-ago)."""
    if len(closes) < 1200:
        # Not enough data for 5 years, use earliest available
        pass

    # Use the earliest available price (up to ~1250 trading days ≈ 5 years)
    lookback = min(1250, len(closes) - 1)
    if lookback < 200:
        return None

    earliest = closes[-lookback]
    latest = closes[-1]

    if earliest <= 0:
        return None

    return latest / earliest


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50

    print("获取全A股股票列表...")
    all_stocks = get_all_stocks()
    print(f"共 {len(all_stocks)} 只股票")

    results = []
    total = len(all_stocks)
    start = time.time()

    for i, (code, _name_placeholder) in enumerate(all_stocks):
        if i % 200 == 0:
            elapsed = time.time() - start
            eta = (elapsed / max(i, 1)) * (total - i) / 60
            print(f"  进度: {i}/{total} ({i/total*100:.0f}%)  ETA: {eta:.0f}min")

        closes = fetch_kline(code)
        if closes is None:
            continue

        multiple = calc_return(closes)
        if multiple is None or multiple <= 0:
            continue

        # Get current price and name
        cur_price, name = fetch_price_name(code)
        if cur_price is None:
            cur_price = closes[-1]
        if not name:
            name = code
        cur = cur_price
        yr1 = closes[-min(250, len(closes)-1)]
        pct_1y = (cur / yr1 - 1) * 100 if yr1 > 0 else 0
        pct_20d = (cur / closes[-min(21, len(closes)-1)] - 1) * 100 if len(closes) >= 22 else 0
        mo3 = closes[-min(63, len(closes)-1)]
        pct_3m = (cur / mo3 - 1) * 100 if mo3 > 0 else 0

        results.append({
            "code": code,
            "name": name,
            "multiple": multiple,
            "cur": cur,
            "pct_1y": pct_1y,
            "pct_3m": pct_3m,
            "pct_20d": pct_20d,
        })

        time.sleep(0.03)  # rate limit

    # Sort by multiple descending
    results.sort(key=lambda x: x["multiple"], reverse=True)

    # Print top N
    top_n = min(limit, len(results))
    print(f"\n{'='*100}")
    print(f"  近5年涨幅最高 TOP {top_n} (共扫描 {len(results)} 只有效数据)")
    print(f"{'='*100}")
    print(f"{'排名':<4} {'代码':<8} {'名称':<10} {'5年倍数':<8} {'当前价':<10} {'1年%':<8} {'3月%':<8} {'20日%':<8}")
    print("-" * 100)

    for rank, r in enumerate(results[:top_n], 1):
        print(
            f"{rank:<4} {r['code']:<8} {r['name']:<10} "
            f"{r['multiple']:<8.1f}x {r['cur']:<10.2f} "
            f"{r['pct_1y']:+7.1f}% {r['pct_3m']:+7.1f}% {r['pct_20d']:+7.1f}%"
        )

    # Also show stocks with most data (closest to true 5y)
    print(f"\n  扫描耗时: {(time.time()-start)/60:.1f} 分钟")
    print(f"  有效数据: {len(results)} 只")


if __name__ == "__main__":
    main()
