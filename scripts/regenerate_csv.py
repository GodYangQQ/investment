#!/usr/bin/env python3
"""把 SOXX 池最新行情写成 output/strategy_YYYYMMDD.csv，供监控脚本读取。"""
import csv
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))
OUTPUT_DIR = os.path.join(ROOT, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

from universe import get_soxx_universe
from us_data import fetch_realtime_quote, fetch_index_quote
from stock_strategy import fetch_daily_kline, compute_indicators, get_latest_signals


def main():
    soxx = fetch_index_quote("SOXX")
    rows = []
    for s in get_soxx_universe(top_n=20, live=True):
        try:
            q = fetch_realtime_quote(s["code"])
            df = compute_indicators(fetch_daily_kline(s["code"], days=120))
            sig = get_latest_signals(df, q)
            rows.append({
                "代码": s["code"],
                "名称": q.get("name") or s["name"],
                "细分": s.get("industry", ""),
                "现价": q.get("price"),
                "涨跌%": q.get("pct_change"),
                "PE": q.get("pe_ttm"),
                "MA20": sig.get("ma20"),
                "MA60": sig.get("ma60"),
                "量比": sig.get("vol_ratio"),
            })
        except Exception as e:
            print(f"skip {s['code']}: {e}")
    path = os.path.join(OUTPUT_DIR, f"strategy_{datetime.now().strftime('%Y%m%d')}.csv")
    if not rows:
        print("no rows")
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"SOXX {soxx.get('idx_price')} ({soxx.get('idx_pct'):+.2f}%)  {soxx.get('status')}")
    print(f"wrote {path} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
