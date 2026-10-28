#!/usr/bin/env python3
"""
W底 / M顶 形态识别回测
======================
W底(双底)：两谷近似同高，价突破颈线(中间峰) → 买入
M顶(双顶)：两峰近似同高，价跌破颈线(中间谷) → 卖出
只做多：W底进场，M顶离场。对比买入持有。

参数：
  w       摆动点窗口（左右各 w 根确认局部高低点）
  tol     双峰/双谷高度差容差（如 3%）
  min/max 两峰(谷)之间最小/最大间隔

用法:
    python experiments/pattern_test.py 002463 002851 002384
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cn_backtest import backtest  # noqa: E402
from cn_stock_test import fetch_ak, to_backtest_df  # noqa: E402


def find_swings(c: np.ndarray, w: int):
    highs, lows = [], []
    for i in range(w, len(c) - w):
        seg = c[i - w:i + w + 1]
        if c[i] == seg.max():
            highs.append(i)
        if c[i] == seg.min():
            lows.append(i)
    return highs, lows


def detect_events(c: np.ndarray, w: int, tol: float, min_dist: int, max_dist: int):
    highs, lows = find_swings(c, w)
    events = []
    # M顶 → 卖
    for i in range(len(highs) - 1):
        h1, h2 = highs[i], highs[i + 1]
        if not (min_dist <= h2 - h1 <= max_dist):
            continue
        if abs(c[h1] - c[h2]) / c[h1] > tol:
            continue
        troughs = [l for l in lows if h1 < l < h2]
        if not troughs:
            continue
        neck = min(c[l] for l in troughs)
        for t in range(h2 + w, len(c)):
            if c[t] < neck:
                events.append((t, "sell"))
                break
    # W底 → 买
    for i in range(len(lows) - 1):
        l1, l2 = lows[i], lows[i + 1]
        if not (min_dist <= l2 - l1 <= max_dist):
            continue
        if abs(c[l1] - c[l2]) / c[l1] > tol:
            continue
        peaks = [h for h in highs if l1 < h < l2]
        if not peaks:
            continue
        neck = max(c[h] for h in peaks)
        for t in range(l2 + w, len(c)):
            if c[t] > neck:
                events.append((t, "buy"))
                break
    return events


def build_signal(n: int, events) -> pd.Series:
    trans = {}
    for t, typ in events:
        trans[t] = 1 if typ == "buy" else 0
    sig = np.zeros(n)
    pos = 0
    for t in range(n):
        if t in trans:
            pos = trans[t]
        sig[t] = pos
    return pd.Series(sig)


def run_stock(code, name, w=5, tol=0.03, min_dist=5, max_dist=120):
    raw = fetch_ak(code)
    if raw is None or len(raw) == 0:
        return None
    df = to_backtest_df(raw)
    n = len(df)
    c = df["close"].values
    ev = detect_events(c, w, tol, min_dist, max_dist)
    sig = build_signal(n, ev)
    r = backtest(df, sig, 0, n - 1)
    nb = sum(1 for _, t in ev if t == "buy")
    ns = sum(1 for _, t in ev if t == "sell")
    return {"name": name, "code": code, "策略%": r.total * 100, "基准%": r.bench * 100,
            "超额%": r.excess * 100, "回撤%": r.maxdd * 100, "W底": nb, "M顶": ns}


def main():
    ap = argparse.ArgumentParser(description="W底/M顶 回测")
    ap.add_argument("codes", nargs="+", help="A股6位代码")
    args = ap.parse_args()

    print(f"W底/M顶 默认参数: w=5, tol=3%, min_dist=5, max_dist=120")
    print("=" * 78)
    for code in args.codes:
        r = run_stock(code, code)
        if r is None:
            print(f"[{code}] 数据失败")
            continue
        print(f"{r['name']}({r['code']}): 策略 {r['策略%']:+.1f}%  "
              f"基准 {r['基准%']:+.1f}%  超额 {r['超额%']:+.1f}%  "
              f"回撤 {r['回撤%']:.1f}%  W底{r['W底']}次 M顶{r['M顶']}次")

    # 参数敏感性（只看有没有任何参数组合能跑赢买持）
    print("\n== 参数敏感性（w × tol 网格，看是否存在正超额） ==")
    print("=" * 78)
    for code in args.codes:
        best = None
        for w in (3, 5, 7):
            for tol in (0.02, 0.03, 0.05):
                r = run_stock(code, code, w=w, tol=tol)
                if r and (best is None or r["超额%"] > best["超额%"]):
                    best = r
        if best:
            print(f"{best['name']}({best['code']}): 网格内最优 超额 {best['超额%']:+.1f}%  "
                  f"(策略 {best['策略%']:+.1f}% vs 基准 {best['基准%']:+.1f}%)")


if __name__ == "__main__":
    main()
