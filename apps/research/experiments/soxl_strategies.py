#!/usr/bin/env python3
"""
寻找最适合 SOXL 的战法：全面测试 A1~B8 + 扩展
============================================
基准 = SOXX 买入持有（1x 半导体的自然基准），同时报 SOXL 买入持有（朴素 3x）。
全期累计收益 + 最大回撤。信号收盘算、次日开盘成交、手续费 0.05%、只做多。

数据局限：Nasdaq API 无行业ETF/黄金/商品，轮动类只能用现有股票+债券池近似。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.datasets.us_data import fetch_daily_kline  # noqa: E402
from multi_backtest import load_aligned, FEE  # noqa: E402

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

EQUITY = ["SOXX", "SMH", "SPY", "QQQ", "XLK", "VGT", "XSD", "PSI", "FTXL"]
BOND = ["TLT", "IEF", "SHY", "LQD", "HYG", "BND", "AGG"]
POOL = EQUITY + BOND + ["SOXL", "TQQQ", "UPRO"]


def _idx(cols, name):
    return list(cols).index(name)


def _bh_w(cols, name):
    n = len(cols)
    w = np.zeros(n)
    w[_idx(cols, name)] = 1.0
    return [w.copy() for _ in range(2000)]


# ---------------- 策略：返回 target 权重序列 ----------------
def static_beta(close, base="SOXX", lev="SOXL", w_lev=0.25):
    tickers = list(close.columns)
    bi, li = _idx(tickers, base), _idx(tickers, lev)
    w = np.zeros(len(tickers))
    w[bi] = 1.0 - w_lev
    w[li] = w_lev
    return [w.copy() for _ in range(len(close))]


def vol_scaled(close, base="SOXX", lev="SOXL", target_vol=0.40, max_w=0.9, window=21):
    """SOXL 权重 = clip(target_vol/rv_SOXL, 0, max_w)，剩余持 SOXX（β下限1x）。"""
    tickers = list(close.columns)
    bi, li = _idx(tickers, base), _idx(tickers, lev)
    rv = close[lev].pct_change().rolling(window).std() * np.sqrt(252)
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(rv.iloc[t]) or rv.iloc[t] <= 0:
            w[bi] = 1.0
        else:
            wl = float(np.clip(target_vol / rv.iloc[t], 0.0, max_w))
            w[li] = wl
            w[bi] = 1.0 - wl
        out.append(w)
    return out


def ma200_tiered(close, base="SOXX", lev="SOXL"):
    """慢均线分档：>MA200 且 MA50>MA200 → 3x；>MA200 但 MA50<MA200 → 1.5x；<MA200 → 现金。"""
    tickers = list(close.columns)
    bi, li = _idx(tickers, base), _idx(tickers, lev)
    m200 = close[base].rolling(200).mean()
    m50 = close[base].rolling(50).mean()
    c = close[base]
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(m200.iloc[t]):
            out.append(w)
            continue
        if c.iloc[t] > m200.iloc[t]:
            if m50.iloc[t] > m200.iloc[t]:
                w[li] = 1.0
            else:
                w[bi] = 0.5
                w[li] = 0.25  # 1.5x
        out.append(w)
    return out


def ma200_soxl_or_soxx(close, base="SOXX", lev="SOXL"):
    """>MA200 持 SOXL，否则持 SOXX（永不在场外）。"""
    tickers = list(close.columns)
    bi, li = _idx(tickers, base), _idx(tickers, lev)
    m200 = close[base].rolling(200).mean()
    c = close[base]
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(m200.iloc[t]):
            out.append(w)
            continue
        if c.iloc[t] > m200.iloc[t]:
            w[li] = 1.0
        else:
            w[bi] = 1.0
        out.append(w)
    return out


def dual_ma_lev(close, base="SOXX", lev="SOXL", fast=50, slow=200):
    tickers = list(close.columns)
    bi, li = _idx(tickers, base), _idx(tickers, lev)
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


def momentum_topk(close, universe, top_k=2, mom=126, abs_filter=False):
    tickers = list(close.columns)
    cols = [t for t in universe if t in tickers]
    idxs = [_idx(tickers, t) for t in cols]
    sub = close[cols]
    momo = sub / sub.shift(mom) - 1.0
    out = []
    last = -1
    for t in range(len(close)):
        if t == 0:
            out.append(np.zeros(len(tickers)))
            continue
        if t - last < 21 and last >= 0:
            out.append(out[-1].copy())
            continue
        w = np.zeros(len(tickers))
        row = momo.iloc[t].fillna(-np.inf)
        order = np.argsort(row.values)[::-1]
        picks = [idxs[i] for i in order if np.isfinite(row.values[i])][:top_k]
        if abs_filter:
            picks = [p for p in picks if momo.iloc[t, idxs.index(p)] > 0]
        if picks:
            for p in picks:
                w[p] = 1.0 / len(picks)
        out.append(w)
        last = t
    return out


def sell_in_may(close, base="SOXX", dates=None):
    tickers = list(close.columns)
    bi = _idx(tickers, base)
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if dates is not None:
            m = dates[t].month
            if m >= 11 or m <= 4:
                w[bi] = 1.0
        out.append(w)
    return out


def faber_12m(close, base="SOXX", safe="SHY"):
    tickers = list(close.columns)
    bi = _idx(tickers, base)
    si = _idx(tickers, safe) if safe in tickers else -1
    m = close[base].rolling(252).mean()
    c = close[base]
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(m.iloc[t]):
            out.append(w)
            continue
        if c.iloc[t] > m.iloc[t]:
            w[bi] = 1.0
        elif si >= 0:
            w[si] = 1.0
        out.append(w)
    return out


def risk_parity(close, assets=("SOXX", "TLT", "SHY"), window=63):
    tickers = list(close.columns)
    assets = [a for a in assets if a in tickers]
    idxs = [_idx(tickers, a) for a in assets]
    rets = close[assets].pct_change()
    vol = rets.rolling(window).std()
    inv = 1.0 / vol
    out = []
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0:
            out.append(w)
            continue
        row = inv.iloc[t]
        denom = row.sum()
        if pd.notna(denom) and denom > 0:
            for i, a in enumerate(assets):
                w[idxs[i]] = row[a] / denom
        out.append(w)
    return out


def rebalance_8020(close, base="SOXX", hedge="TLT", w_base=0.8):
    tickers = list(close.columns)
    bi = _idx(tickers, base)
    hi = _idx(tickers, hedge)
    out = []
    last = -1
    for t in range(len(close)):
        if t == 0:
            out.append(np.zeros(len(tickers)))
            continue
        if t - last < 21 and last >= 0:
            out.append(out[-1].copy())
            continue
        w = np.zeros(len(tickers))
        w[bi] = w_base
        w[hi] = 1.0 - w_base
        out.append(w)
        last = t
    return out


def soxl_atr_trail(close, base="SOXX", lev="SOXL", mult=3.0, n=14):
    """SOXL + ATR 移动止损：站上 MA42 买 SOXL，跌破 峰值-3*ATR(SOXX) 离场。"""
    tickers = list(close.columns)
    bi, li = _idx(tickers, base), _idx(tickers, lev)
    m = close[base].rolling(42).mean()
    c = close[base]
    # ATR on base
    tr = pd.concat([close[base].diff().abs(),
                    (close[base] - close[base].shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
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
            if c.iloc[t] < m.iloc[t] or c.iloc[t] < peak - mult * atr.iloc[t]:
                pos = 0
        if pos == 1:
            w[li] = 1.0
        out.append(w)
    return out


# ---------------- 回测 ----------------
def backtest_full(close, open_, targets, bench_ticker, fee=FEE):
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
    eq = np.array(eq)
    maxdd = float(np.min(eq / np.maximum.accumulate(eq) - 1))
    bench = (close[bench_ticker].iloc[-1] / open_[bench_ticker].iloc[1]) * (1 - fee) ** 2
    return V - 1, maxdd, bench - 1


def main():
    print("加载资产池 ...", flush=True)
    close, open_, dates = load_aligned(POOL, 1600)
    print(f"{len(close)} 根，{close.shape[1]} 资产\n")
    bh_soxx = _bh_w(close.columns, "SOXX")
    bh_soxl = _bh_w(close.columns, "SOXL")

    results = []

    def run(name, tg):
        total, maxdd, bench = backtest_full(close, open_, tg, "SOXX")
        results.append({"strategy": name, "total": total, "maxdd": maxdd, "bench_soxx": bench})
        print(f"  {name:<28} 收益 {total*100:+7.1f}%  回撤 {maxdd*100:6.1f}%  "
              f"(SOXX买持 {(1+bench)*100:+.1f}%)")

    print("=== 基准 ===")
    t0, d0, b0 = backtest_full(close, open_, bh_soxx, "SOXX")
    t1, d1, _ = backtest_full(close, open_, bh_soxl, "SOXX")
    print(f"  SOXX 买入持有        收益 {t0*100:+7.1f}%  回撤 {d0*100:6.1f}%")
    print(f"  SOXL 买入持有        收益 {t1*100:+7.1f}%  回撤 {d1*100:6.1f}%\n")

    print("=== A. 智能杠杆 ===")
    run("A1 静态β1.25 (12.5%SOXL)", static_beta(close, w_lev=0.125))
    run("A1 静态β1.5 (25%SOXL)", static_beta(close, w_lev=0.25))
    run("A1 静态β2.0 (50%SOXL)", static_beta(close, w_lev=0.5))
    for tv in (0.35, 0.45, 0.55):
        run(f"A1 波动率目标 vt={tv}", vol_scaled(close, target_vol=tv))
    run("A2 MA200分档(3x/1.5x/现金)", ma200_tiered(close))
    run("A2 MA200:SOXL或SOXX", ma200_soxl_or_soxx(close))
    run("A2 双均线50/200杠杆", dual_ma_lev(close, base="SOXX", lev="SOXL", fast=50, slow=200))

    print("\n=== B. 轮动类 ===")
    run("B3 行业轮动top2(现有池)", momentum_topk(close, EQUITY, 2, 126))
    run("B3 行业轮动top1(现有池)", momentum_topk(close, EQUITY, 1, 126))
    run("B4 跨资产top1(股+债)", momentum_topk(close, EQUITY + BOND, 1, 126))
    run("B4 跨资产top1+绝对过滤", momentum_topk(close, EQUITY + BOND, 1, 126, abs_filter=True))

    print("\n=== C. 择时/季节 ===")
    run("B5 Sell in May(5-10月空仓)", sell_in_may(close, dates=dates))
    run("B6 Faber 12月动量(退SHY)", faber_12m(close))

    print("\n=== D. 风险预算/对冲 ===")
    run("B7 风险平价 SOXX+TLT+SHY", risk_parity(close))
    run("B8 80/20 SOXX+TLT 月再平衡", rebalance_8020(close))

    print("\n=== E. SOXL 扩展 ===")
    run("E SOXL+ATR移动止损", soxl_atr_trail(close))

    res = pd.DataFrame(results)
    res.to_csv(os.path.join(OUTDIR, "soxl_strategies.csv"), index=False, encoding="utf-8-sig")
    print(f"\n已保存: {os.path.join(OUTDIR, 'soxl_strategies.csv')}\n")
    print("===== 按收益排序（跑赢 SOXX 买持 的）=====")
    for _, r in res.sort_values("total", ascending=False).iterrows():
        flag = "✓跑赢" if r.total > r.bench_soxx else "  跑输"
        print(f"  {r.strategy:<28} 收益 {r.total*100:+7.1f}%  回撤 {r.maxdd*100:6.1f}%  {flag}")


if __name__ == "__main__":
    main()
