#!/usr/bin/env python3
"""
SOXL 战法稳健性验证：
1) 逐年收益/回撤（尤其 2022 熊市）
2) 波动率目标参数敏感性（target_vol / max_w / window 网格）
3) Calmar（收益/最大回撤）排名
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soxl_strategies import (  # noqa: E402
    POOL, backtest_full, static_beta, vol_scaled, ma200_tiered,
    dual_ma_lev, momentum_topk, EQUITY, BOND,
)
from multi_backtest import load_aligned  # noqa: E402

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def equity_curve(close, open_, targets, fee=0.0005):
    n = close.shape[1]
    V = 1.0
    w = np.zeros(n)
    eq = []
    for t in range(len(close)):
        if t > 0:
            gap = 0.0
            for a in range(n):
                if w[a] > 0:
                    pc = close.iloc[t - 1, a]
                    o = open_.iloc[t, a]
                    if pc and pc > 0 and not pd.isna(o) and o > 0:
                        gap += w[a] * (o / pc - 1.0)
            V *= (1 + gap)
        target = targets[t - 1] if t > 0 else np.zeros(n)
        V *= (1 - fee * float(np.abs(target - w).sum()))
        w = target
        if t > 0:
            dr = 0.0
            for a in range(n):
                if w[a] > 0:
                    o = open_.iloc[t, a]
                    c = close.iloc[t, a]
                    if o and o > 0 and not pd.isna(c):
                        dr += w[a] * (c / o - 1.0)
            V *= (1 + dr)
        eq.append(V)
    return np.array(eq)


def maxdd(eq):
    return float(np.min(eq / np.maximum.accumulate(eq) - 1))


def per_year(eq, dates):
    df = pd.DataFrame({"v": eq, "y": pd.to_datetime(dates).year})
    rows = []
    for y, g in df.groupby("y"):
        r = g["v"].iloc[-1] / g["v"].iloc[0] - 1
        dd = maxdd(g["v"].values)
        rows.append({"year": y, "ret": r, "dd": dd})
    return rows


def main():
    close, open_, dates = load_aligned(POOL, 1600)
    print(f"{len(close)} 根, {close.shape[1]} 资产, "
          f"{pd.to_datetime(dates[0]).date()} ~ {pd.to_datetime(dates[-1]).date()}\n")

    strategies = {
        "SOXX 买持": None,
        "SOXL 买持": None,
        "静态β1.25": static_beta(close, w_lev=0.125),
        "静态β1.5": static_beta(close, w_lev=0.25),
        "静态β2.0": static_beta(close, w_lev=0.5),
        "波动率目标0.35": vol_scaled(close, target_vol=0.35),
        "波动率目标0.45": vol_scaled(close, target_vol=0.45),
        "波动率目标0.55": vol_scaled(close, target_vol=0.55),
        "双均线50/200": dual_ma_lev(close, base="SOXX", lev="SOXL", fast=50, slow=200),
        "MA200分档": ma200_tiered(close),
        "行业轮动top2": momentum_topk(close, EQUITY, 2, 126),
    }

    # 买持特殊处理（用 backtest_full 的基准口径）
    from soxl_strategies import _bh_w
    strategies["SOXX 买持"] = _bh_w(close.columns, "SOXX")
    strategies["SOXL 买持"] = _bh_w(close.columns, "SOXL")

    print("=== 逐年收益（%） / 逐年最大回撤（%） ===")
    years = sorted(set(pd.to_datetime(dates).year))
    header = "策略".ljust(18) + "".join(f"{y:>12}" for y in years)
    print(header)
    rows_by_strat = {}
    for name, tg in strategies.items():
        eq = equity_curve(close, open_, tg)
        py = per_year(eq, dates)
        rows_by_strat[name] = py
        line = name.ljust(18)
        for y in years:
            r = next((p for p in py if p["year"] == y), None)
            if r:
                line += f"{r['ret']*100:>6.0f}/{r['dd']*100:>5.0f}"
            else:
                line += " " * 11
        print(line)

    print("\n=== 全期 Calmar 排名（收益/回撤） ===")
    rows = []
    for name, tg in strategies.items():
        eq = equity_curve(close, open_, tg)
        total = eq[-1] / eq[0] - 1
        dd = maxdd(eq)
        calmar = total / abs(dd) if dd < 0 else float("nan")
        rows.append({"策略": name, "全期收益%": total * 100, "最大回撤%": dd * 100, "Calmar": calmar})
    res = pd.DataFrame(rows).sort_values("Calmar", ascending=False)
    print(res.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    print("\n=== 波动率目标 参数敏感性（全期收益% / 回撤%） ===")
    grid = []
    for tv in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        for mw in (0.7, 0.9, 1.0):
            for wd in (10, 21, 42, 63):
                tg = vol_scaled(close, target_vol=tv, max_w=mw, window=wd)
                eq = equity_curve(close, open_, tg)
                total = eq[-1] / eq[0] - 1
                dd = maxdd(eq)
                grid.append({"tv": tv, "max_w": mw, "win": wd,
                             "ret%": total * 100, "dd%": dd * 100})
    gdf = pd.DataFrame(grid)
    piv = gdf.pivot_table(index=["tv", "max_w"], columns="win", values="ret%")
    print("收益%  (tv x max_w) 列=window")
    print(piv.round(0).to_string())
    print("\n最大回撤%  (tv x max_w) 列=window")
    piv2 = gdf.pivot_table(index=["tv", "max_w"], columns="win", values="dd%")
    print(piv2.round(0).to_string())


if __name__ == "__main__":
    main()
