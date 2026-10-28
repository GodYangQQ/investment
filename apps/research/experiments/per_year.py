#!/usr/bin/env python3
"""逐年验证：杠杆趋势策略 vs 买入持有，看是否「跌年亏得少、涨年赚得多」。"""
import os, sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "experiments"))
sys.path.insert(0, ROOT)
from multi_backtest import load_aligned, FEE  # noqa: E402
from us_leverage import targets_bond_switch, targets_regime, targets_volscaled  # noqa: E402


def run_full(close, open_, targets, i0, i1, fee=FEE):
    n = close.shape[1]
    V = 1.0
    w = np.zeros(n)
    eq = []
    for t in range(i0, i1 + 1):
        if t > i0:
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
        if t > i0:
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


def main():
    base, lev = "SOXX", "SOXL"
    pool = [base, lev, "SHY", "TLT", "IEF"]
    close, open_, dates = load_aligned(pool, 1600)
    print(f"数据: {len(close)} 根  起 {dates[0].date()}  止 {dates[-1].date()}\n")

    i0, i1 = 0, len(close) - 1
    bench_close = close[base].values
    bench_eq = bench_close / close[base].iloc[0]

    strats = {
        "bond_switch SHY MA42": targets_bond_switch(close, base, lev, 42, "SHY"),
        "regime MA42 (3x/1x/0x)": targets_regime(close, base, lev, 42),
        "volscaled MA42 vt=0.5": targets_volscaled(close, base, lev, 42, 0.50),
    }
    eqs = {k: run_full(close, open_, v, i0, i1) for k, v in strats.items()}

    # 按日历年分组
    dts = pd.DatetimeIndex(dates)
    years = sorted(set(d.year for d in dts))
    print(f"{'年份':<6}{'SOXX买持':>10}  " + "".join(f"{k:>20}" for k in strats) + "\n")
    for y in years:
        mask = np.array([d.year == y for d in dts])
        idx = np.where(mask)[0]
        if len(idx) < 2:
            continue
        b0, b1 = bench_eq[idx[0]], bench_eq[idx[-1]]
        row = f"{y:<6}{b1/b0-1:>+9.1%}  "
        for k in strats:
            e = eqs[k]
            row += f"{e[idx[-1]]/e[idx[0]]-1:>+19.1%}  "
        print(row)

    # 2022 熊市（半导体大跌）单独看
    print("\n=== 2022 熊市（SOXX 全年）===")
    y = 2022
    mask = np.array([d.year == y for d in dts])
    idx = np.where(mask)[0]
    b0, b1 = bench_eq[idx[0]], bench_eq[idx[-1]]
    print(f"  SOXX买持: {b1/b0-1:+.1%}   最大回撤 {np.min(bench_eq[idx]/np.maximum.accumulate(bench_eq[idx])-1):.1%}")
    for k in strats:
        e = eqs[k]
        print(f"  {k:<24} {e[idx[-1]]/e[idx[0]]-1:+.1%}   最大回撤 {np.min(e[idx]/np.maximum.accumulate(e[idx])-1):.1%}")

    # 2023-2024 牛市
    print("\n=== 2023 牛市（反弹）===")
    y = 2023
    mask = np.array([d.year == y for d in dts])
    idx = np.where(mask)[0]
    b0, b1 = bench_eq[idx[0]], bench_eq[idx[-1]]
    print(f"  SOXX买持: {b1/b0-1:+.1%}")
    for k in strats:
        e = eqs[k]
        print(f"  {k:<24} {e[idx[-1]]/e[idx[0]]-1:+.1%}")

    # 全期总收益
    print("\n=== 全期累计（2020-06 ~ 2026-10）===")
    print(f"  SOXX买持: {bench_eq[-1]/bench_eq[0]-1:+.1%}")
    for k in strats:
        e = eqs[k]
        print(f"  {k:<24} {e[-1]/e[0]-1:+.1%}")


if __name__ == "__main__":
    main()
