#!/usr/bin/env python3
"""扫描 SOXX 前20，按近5年涨幅倍数倒排。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))

from core.datasets.universe import get_soxx_universe
from core.datasets.us_data import fetch_daily_kline


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    rows = []
    for s in get_soxx_universe(top_n=20, live=True):
        try:
            df = fetch_daily_kline(s["code"], days=1300)
            if df is None or len(df) < 60:
                continue
            start = float(df["close"].iloc[0])
            end = float(df["close"].iloc[-1])
            mult = end / start if start else 0
            rows.append((mult, s["code"], s["name"], start, end, len(df)))
        except Exception as e:
            print(f"skip {s['code']}: {e}")
    rows.sort(reverse=True)
    print(f"{'排名':<4} {'代码':<6} {'名称':<28} {'倍数':>8} {'起点':>10} {'现价':>10}")
    for i, (mult, code, name, start, end, _) in enumerate(rows[:n], 1):
        print(f"{i:<4} {code:<6} {name:<28} {mult:>7.2f}x {start:>10.2f} {end:>10.2f}")


if __name__ == "__main__":
    main()
