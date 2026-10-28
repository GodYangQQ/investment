#!/usr/bin/env python3
"""
周线级 W底 / M顶 形态回测
=========================
把日线重采样成周线（周开=本周首日开盘，周收=本周末日收盘），
再在周线上做 W底/M顶 识别与回测，对比买入持有。

用法:
    python experiments/pattern_weekly.py 002463 002851 002384
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cn_backtest import backtest  # noqa: E402
from cn_stock_test import fetch_ak, to_backtest_df  # noqa: E402
from pattern_test import detect_events, build_signal  # noqa: E402


def to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    w = df.set_index("date")
    o = w["open"].resample("W").first()
    c = w["close"].resample("W").last()
    out = pd.DataFrame({"open": o, "close": c}).dropna().reset_index()
    return out


def run(code, w=3, tol=0.03, min_dist=3, max_dist=60):
    raw = fetch_ak(code)
    if raw is None or len(raw) == 0:
        return None
    df = to_weekly(to_backtest_df(raw))
    n = len(df)
    c = df["close"].values
    ev = detect_events(c, w, tol, min_dist, max_dist)
    sig = build_signal(n, ev)
    r = backtest(df, sig, 0, n - 1)
    nb = sum(1 for _, t in ev if t == "buy")
    ns = sum(1 for _, t in ev if t == "sell")
    return {"n": n, "策略%": r.total * 100, "基准%": r.bench * 100, "超额%": r.excess * 100,
            "回撤%": r.maxdd * 100, "W底": nb, "M顶": ns}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+")
    args = ap.parse_args()

    print("周线 W底/M顶 默认: w=3周, tol=3%, min_dist=3, max_dist=60")
    print("=" * 78)
    for code in args.codes:
        r = run(code)
        if r is None:
            print(f"[{code}] 数据失败")
            continue
        print(f"{code}: 周线{r['n']}根  策略 {r['策略%']:+.1f}%  基准 {r['基准%']:+.1f}%  "
              f"超额 {r['超额%']:+.1f}%  回撤 {r['回撤%']:.1f}%  W底{r['W底']} M顶{r['M顶']}")

    print("\n== 参数敏感性（w × tol 网格，找是否存在正超额） ==")
    print("=" * 78)
    for code in args.codes:
        best = None
        bp = None
        for w in (2, 3, 4, 5):
            for tol in (0.02, 0.03, 0.05):
                r = run(code, w=w, tol=tol)
                if r and (best is None or r["超额%"] > best["超额%"]):
                    best = r
                    bp = (w, tol)
        if best:
            print(f"{code}: 网格最优 超额 {best['超额%']:+.1f}%  (策略 {best['策略%']:+.1f}% vs 基准 {best['基准%']:+.1f}%)  @w={bp[0]},tol={bp[1]}")


if __name__ == "__main__":
    main()
