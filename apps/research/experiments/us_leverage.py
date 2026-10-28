#!/usr/bin/env python3
"""
美股杠杆趋势策略改进：目标是「下跌亏得少、上涨赚得多」
======================================================
基础结论：SOXX 纯择时跑不赢买持；趋势+3x 杠杆是唯一能勉强跑赢的（MA42，+1.7%），但回撤 -62%。
改进方向：分级杠杆 / 不对称进出 / 波动率缩放 / Donchian，压低回撤同时保住超额。

所有策略只做多，信号收盘算、次日开盘成交、手续费 0.05%。
基准 = SOXX 买入持有。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "experiments"))
sys.path.insert(0, ROOT)
from multi_backtest import (load_aligned, backtest_portfolio, evaluate, windows,  # noqa: E402
                            UNIVERSE, BASE_TO_LEV, BASE_TO_INV, FEE)

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def _m(cols, name):
    return list(cols).index(name)


def targets_regime(close, base, lev, ma_n=42, slope_k=5, vol_filter=False):
    """分级：close>MA且MA向上→3x；close>MA但MA不向上→1x；close<MA→现金。"""
    tickers = list(close.columns)
    bi, li = _m(tickers, base), _m(tickers, lev)
    m = close[base].rolling(ma_n).mean()
    m_slope = m > m.shift(slope_k)
    c = close[base]
    rv = close[base].pct_change().rolling(20).std() * np.sqrt(252)
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(m.iloc[t]):
            out.append(w)
            continue
        above = c.iloc[t] > m.iloc[t]
        if above:
            if m_slope.iloc[t]:
                if not vol_filter or (pd.notna(rv.iloc[t]) and rv.iloc[t] < 0.6):
                    w[li] = 1.0
                else:
                    w[bi] = 1.0
            else:
                w[bi] = 1.0
        out.append(w)
    return out


def targets_donchian(close, base, lev, entry=20, exit_n=10):
    tickers = list(close.columns)
    bi, li = _m(tickers, base), _m(tickers, lev)
    hi = close[base].rolling(entry).max().shift(1)
    lo = close[base].rolling(exit_n).min().shift(1)
    c = close[base]
    out = []
    pos = 0
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(hi.iloc[t]):
            out.append(w)
            continue
        if pos == 0 and c.iloc[t] > hi.iloc[t]:
            pos = 1
        elif pos == 1 and c.iloc[t] < lo.iloc[t]:
            pos = 0
        if pos == 1:
            w[li] = 1.0
        out.append(w)
    return out


def targets_volscaled(close, base, lev, ma_n=42, vol_target=0.40, max_lev=3.0, window=21):
    tickers = list(close.columns)
    bi, li = _m(tickers, base), _m(tickers, lev)
    m = close[base].rolling(ma_n).mean()
    c = close[base]
    rv = close[lev].pct_change().rolling(window).std() * np.sqrt(252)
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(m.iloc[t]):
            out.append(w)
            continue
        if c.iloc[t] > m.iloc[t] and pd.notna(rv.iloc[t]) and rv.iloc[t] > 0:
            scale = float(np.clip(vol_target / rv.iloc[t], 0.0, max_lev))
            w[li] = scale
        out.append(w)
    return out


def targets_asym(close, base, lev, entry_ma=42, exit_ma=21):
    tickers = list(close.columns)
    bi, li = _m(tickers, base), _m(tickers, lev)
    me = close[base].rolling(entry_ma).mean()
    mx = close[base].rolling(exit_ma).mean()
    c = close[base]
    out = []
    pos = 0
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(me.iloc[t]) or pd.isna(mx.iloc[t]):
            out.append(w)
            continue
        if pos == 0 and c.iloc[t] > me.iloc[t]:
            pos = 1
        elif pos == 1 and c.iloc[t] < mx.iloc[t]:
            pos = 0
        if pos == 1:
            w[li] = 1.0
        out.append(w)
    return out


def targets_regime_tstop(close, base, lev, ma_n=42, stop_pct=0.12):
    """regime + 移动止损：入场 close>MA；持仓中若 base 从峰值回落 stop_pct 则清仓。"""
    tickers = list(close.columns)
    bi, li = _m(tickers, base), _m(tickers, lev)
    m = close[base].rolling(ma_n).mean()
    c = close[base]
    out = []
    pos = 0
    peak = 0.0
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(m.iloc[t]):
            out.append(w)
            continue
        if pos == 0 and c.iloc[t] > m.iloc[t]:
            pos = 1
            peak = c.iloc[t]
        elif pos == 1:
            peak = max(peak, c.iloc[t])
            if c.iloc[t] < m.iloc[t] or c.iloc[t] < peak * (1 - stop_pct):
                pos = 0
        if pos == 1:
            w[li] = 1.0
        out.append(w)
    return out


def targets_bond_switch(close, base, lev, ma_n=42, safe="SHY"):
    """牛市持3x，熊市切换至安全资产（现金/SHY/TLT），永不在场外。"""
    tickers = list(close.columns)
    bi, li = _m(tickers, base), _m(tickers, lev)
    si = _m(tickers, safe) if safe in tickers else -1
    m = close[base].rolling(ma_n).mean()
    c = close[base]
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(m.iloc[t]):
            out.append(w)
            continue
        if c.iloc[t] > m.iloc[t]:
            w[li] = 1.0
        elif si >= 0:
            w[si] = 1.0
        out.append(w)
    return out


def targets_vol_entry(close, base, lev, ma_n=42, vol_thresh=0.35, max_lev=3.0):
    """波动率缩放 + 入场过滤：只有 base 低波动(趋势平稳)时才上杠杆，否则只持 1x。"""
    tickers = list(close.columns)
    bi, li = _m(tickers, base), _m(tickers, lev)
    m = close[base].rolling(ma_n).mean()
    c = close[base]
    rv = close[base].pct_change().rolling(20).std() * np.sqrt(252)
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(m.iloc[t]):
            out.append(w)
            continue
        if c.iloc[t] > m.iloc[t]:
            if pd.notna(rv.iloc[t]) and rv.iloc[t] < vol_thresh:
                w[li] = 1.0
            else:
                w[bi] = 1.0
        out.append(w)
    return out


def targets_dual_ma_lev(close, base, lev, fast=21, slow=42):
    tickers = list(close.columns)
    bi, li = _m(tickers, base), _m(tickers, lev)
    f = close[base].rolling(fast).mean()
    s = close[base].rolling(slow).mean()
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(s.iloc[t]):
            out.append(w)
            continue
        if f.iloc[t] > s.iloc[t]:
            w[li] = 1.0
        out.append(w)
    return out


def _run(close, open_, targets, bench, wins, label, show=True):
    r = evaluate(close, open_, targets, wins, bench)
    r["strategy"] = label
    if show:
        print(f"  {label:<40} 超额 {r['mean_excess']*100:+6.1f}%  跑赢 {r['beat_rate']*100:3.0f}%  "
              f"夏普 {r['mean_sharpe']:4.2f}  策略 {r['mean_total']*100:+6.1f}%  "
              f"回撤 {r['worst_dd']*100:5.1f}%  基准回撤 {r['mean_bench_dd']*100:5.1f}%  "
              f"在场 {r['mean_exposure']*100:3.0f}%")
    return r


def main():
    base, lev = "SOXX", "SOXL"
    pool = [base, lev, "QQQ", "TQQQ", "SPY", "UPRO", "SHY", "TLT", "IEF"]
    print(f"加载 {pool} ...", flush=True)
    close, open_, dates = load_aligned(pool, 1600)
    wins = windows(len(close), 5)
    print(f"{len(close)} 根，{len(wins)} 个1年窗口，基准={base}，费{FEE*100:.2f}%\n")
    # 基准本身
    bh = [np.array([1.0 if i == _m(list(close.columns), base) else 0.0 for i in range(close.shape[1])])] * len(close)
    _run(close, open_, bh, base, wins, f"BENCH {base} 买入持有")

    rows = []
    for ma_n in (21, 30, 42, 50, 63):
        rows.append(_run(close, open_, targets_regime(close, base, lev, ma_n=ma_n), base, wins,
                         f"regime MA{ma_n} (3x/1x/0x)"))
    for ma_n in (42, 50, 63):
        rows.append(_run(close, open_, targets_regime(close, base, lev, ma_n=ma_n, vol_filter=True), base, wins,
                         f"regime+volfilter MA{ma_n}"))
    for e, x in [(20, 10), (55, 20), (20, 5), (40, 10)]:
        rows.append(_run(close, open_, targets_donchian(close, base, lev, e, x), base, wins,
                         f"donchian {e}/{x} ->3x"))
    for vt in (0.30, 0.40, 0.50, 0.60):
        rows.append(_run(close, open_, targets_volscaled(close, base, lev, ma_n=42, vol_target=vt), base, wins,
                         f"volscaled MA42 vt={vt}"))
    for e, x in [(42, 21), (42, 10), (50, 21), (63, 21)]:
        rows.append(_run(close, open_, targets_asym(close, base, lev, e, x), base, wins,
                         f"asym entry{e}/exit{x}"))
    for f, s in [(10, 42), (21, 42), (21, 63), (50, 200)]:
        rows.append(_run(close, open_, targets_dual_ma_lev(close, base, lev, f, s), base, wins,
                         f"dualMA_lev {f}/{s}"))

    # ---- 回撤控制系列 ----
    for ma_n, sp in [(42, 0.08), (42, 0.12), (42, 0.15), (50, 0.12)]:
        rows.append(_run(close, open_, targets_regime_tstop(close, base, lev, ma_n, sp), base, wins,
                         f"regime+tstop MA{ma_n} stop{sp}"))
    for safe in ("SHY", "TLT", "IEF"):
        for ma_n in (42, 50):
            rows.append(_run(close, open_, targets_bond_switch(close, base, lev, ma_n, safe), base, wins,
                             f"bond_switch {safe} MA{ma_n}"))
    for vt in (0.25, 0.30, 0.35, 0.40):
        rows.append(_run(close, open_, targets_vol_entry(close, base, lev, 42, vt), base, wins,
                         f"vol_entry MA42 thresh={vt}"))

    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(OUTDIR, "us_leverage.csv"), index=False, encoding="utf-8-sig")
    print(f"\n已保存: {os.path.join(OUTDIR,'us_leverage.csv')}\n")
    print("===== Top 按平均超额 =====")
    for _, r in res.sort_values("mean_excess", ascending=False).head(12).iterrows():
        print(f"  {r.strategy:<40} 超额 {r.mean_excess*100:+6.1f}%  跑赢 {r.beat_rate*100:3.0f}%  "
              f"夏普 {r.mean_sharpe:4.2f}  回撤 {r.worst_dd*100:5.1f}%")
    print("\n===== Top 按夏普 =====")
    for _, r in res.sort_values("mean_sharpe", ascending=False).head(8).iterrows():
        print(f"  {r.strategy:<40} 夏普 {r.mean_sharpe:4.2f}  超额 {r.mean_excess*100:+6.1f}%  "
              f"回撤 {r.worst_dd*100:5.1f}%")


if __name__ == "__main__":
    main()
