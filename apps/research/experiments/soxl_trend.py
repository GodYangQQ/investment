#!/usr/bin/env python3
"""
SOXL 智能杠杆「趋势离场」版：目标真降回撤
=========================================
问题：波动率目标虽跑赢 SOXX 收益，但 2022 回撤 -70%（SOXX 只有 -46%），
      因为它始终带着 SOXL 仓位。这里在波动率目标上叠加趋势过滤：
      SOXX 跌破 MA_N → SOXL 仓位清零，退回 1x SOXX（down_mode=base）或现金（cash）。

验证：
  1) 全期 + 逐年（尤其 2022）
  2) 滚动 1 年窗口的跑赢率（样本外/多窗口）
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

from soxl_strategies import POOL, _idx, vol_scaled, static_beta, _bh_w  # noqa: E402
from multi_backtest import load_aligned, backtest_portfolio, windows  # noqa: E402
from soxl_validate import equity_curve, maxdd, per_year  # noqa: E402

FEE = 0.0005


def vol_scaled_trend(close, base="SOXX", lev="SOXL", target_vol=0.45, max_w=0.9,
                     window=21, trend_ma=200, down_mode="base"):
    tickers = list(close.columns)
    bi, li = _idx(tickers, base), _idx(tickers, lev)
    rv = close[lev].pct_change().rolling(window).std() * np.sqrt(252)
    ma = close[base].rolling(trend_ma).mean()
    c = close[base]
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(rv.iloc[t]) or rv.iloc[t] <= 0:
            w[bi] = 1.0
            out.append(w)
            continue
        wl = float(np.clip(target_vol / rv.iloc[t], 0.0, max_w))
        if pd.notna(ma.iloc[t]) and c.iloc[t] <= ma.iloc[t]:
            wl = 0.0
        w[li] = wl
        w[bi] = 1.0 - wl
        if down_mode == "cash" and wl == 0.0 and pd.notna(ma.iloc[t]) and c.iloc[t] <= ma.iloc[t]:
            w[bi] = 0.0
        out.append(w)
    return out


def beat_vs(close_mat, open_mat, targets, wins, bench_ticker):
    rs = [backtest_portfolio(close_mat, open_mat, targets, a, b, bench_ticker) for a, b in wins]
    ex = np.array([r.excess for r in rs])
    return {
        "beat": float(np.mean(ex > 0)),
        "mean_excess": float(ex.mean()),
        "worst_dd": min(r.maxdd for r in rs),
        "mean_sharpe": float(np.mean([r.sharpe for r in rs])),
    }


def main():
    close, open_, dates = load_aligned(POOL, 1600)
    print(f"{len(close)} 根, {close.shape[1]} 资产, "
          f"{pd.to_datetime(dates[0]).date()} ~ {pd.to_datetime(dates[-1]).date()}\n")
    wins = windows(len(close), years=5)
    print(f"滚动 1 年窗口 {len(wins)} 个\n")

    soxx_bh = _bh_w(close.columns, "SOXX")
    soxl_bh = _bh_w(close.columns, "SOXL")

    variants = {
        "SOXX 买持": soxx_bh,
        "SOXL 买持": soxl_bh,
        "波动率目标0.45(无趋势)": vol_scaled(close, target_vol=0.45),
        "波动率+MA200退回SOXX": vol_scaled_trend(close, target_vol=0.45, trend_ma=200, down_mode="base"),
        "波动率+MA100退回SOXX": vol_scaled_trend(close, target_vol=0.45, trend_ma=100, down_mode="base"),
        "波动率+MA200退回现金": vol_scaled_trend(close, target_vol=0.45, trend_ma=200, down_mode="cash"),
        "波动率+MA100退回现金": vol_scaled_trend(close, target_vol=0.45, trend_ma=100, down_mode="cash"),
        "静态β1.5": static_beta(close, w_lev=0.25),
    }

    print("=== 全期 + 逐年（收益%/回撤%） ===")
    years = sorted(set(pd.to_datetime(dates).year))
    header = "策略".ljust(22) + "".join(f"{y:>12}" for y in years) + f"{'全期':>12} {'全期回撤':>8}"
    print(header)
    summary = []
    for name, tg in variants.items():
        eq = equity_curve(close, open_, tg)
        total = eq[-1] / eq[0] - 1
        dd = maxdd(eq)
        calmar = total / abs(dd) if dd < 0 else float("nan")
        py = {p["year"]: p for p in per_year(eq, dates)}
        line = name.ljust(22)
        for y in years:
            p = py.get(y)
            line += f"{p['ret']*100:>6.0f}/{p['dd']*100:>5.0f}" if p else " " * 11
        line += f"{total*100:>11.0f}% {dd*100:>7.0f}%"
        print(line)
        summary.append({"策略": name, "全期收益%": total * 100, "全期回撤%": dd * 100, "Calmar": calmar})

    print("\n=== 滚动 1 年窗口跑赢率（vs SOXX 和 vs SOXL） ===")
    for name, tg in variants.items():
        vs_soxx = beat_vs(close, open_, tg, wins, "SOXX")
        vs_soxl = beat_vs(close, open_, tg, wins, "SOXL")
        print(f"  {name:<22} vsSOXX跑赢率 {vs_soxx['beat']*100:3.0f}% 超额 {vs_soxx['mean_excess']*100:+6.1f}%  "
              f"最差回撤 {vs_soxx['worst_dd']*100:5.1f}%  |  "
              f"vsSOXL跑赢率 {vs_soxl['beat']*100:3.0f}% 超额 {vs_soxl['mean_excess']*100:+6.1f}%  "
              f"夏普 {vs_soxl['mean_sharpe']:4.2f}")

    print("\n=== 全期 Calmar 排名 ===")
    res = pd.DataFrame(summary).sort_values("Calmar", ascending=False)
    print(res.to_string(index=False, float_format=lambda x: f"{x:.2f}"))


if __name__ == "__main__":
    main()
