#!/usr/bin/env python3
"""
SOXL 智能杠杆：非对称趋势离场（快出慢进）
=========================================
核心矛盾：SOXX 自己 2022 就 -37%/-46%，MA200 又滞后，所以「退回SOXX」只能压到 -50%。
这里试：跌破 MA_fast 就清掉 SOXL（快出），站回 MA_slow 才恢复（慢进），
      配合波动率目标，目标是 2022 回撤 < -46% 且全期收益仍 > SOXX。
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

from soxl_strategies import POOL, _idx, vol_scaled, _bh_w  # noqa: E402
from multi_backtest import load_aligned  # noqa: E402
from soxl_validate import equity_curve, maxdd, per_year  # noqa: E402


def vol_asym(close, base="SOXX", lev="SOXL", target_vol=0.45, max_w=0.9, window=21,
             exit_ma=50, entry_ma=200, base_mode="base"):
    """跌破 exit_ma 清杠杆；站回 entry_ma 恢复。base_mode: base=持1x SOXX / cash=空仓。"""
    tickers = list(close.columns)
    bi, li = _idx(tickers, base), _idx(tickers, lev)
    rv = close[lev].pct_change().rolling(window).std() * np.sqrt(252)
    f = close[base].rolling(exit_ma).mean()
    s = close[base].rolling(entry_ma).mean()
    c = close[base]
    out = []
    on = True
    for t in range(len(close)):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(rv.iloc[t]) or rv.iloc[t] <= 0:
            w[bi] = 1.0
            out.append(w)
            continue
        if pd.notna(f.iloc[t]) and on and c.iloc[t] <= f.iloc[t]:
            on = False
        elif pd.notna(s.iloc[t]) and not on and c.iloc[t] > s.iloc[t]:
            on = True
        if on:
            wl = float(np.clip(target_vol / rv.iloc[t], 0.0, max_w))
            w[li] = wl
            w[bi] = 1.0 - wl
        elif base_mode == "base":
            w[bi] = 1.0
        out.append(w)
    return out


def main():
    close, open_, dates = load_aligned(POOL, 1600)
    print(f"{len(close)} 根, {pd.to_datetime(dates[0]).date()} ~ {pd.to_datetime(dates[-1]).date()}\n")

    variants = {
        "SOXX 买持": _bh_w(close.columns, "SOXX"),
        "波动率目标0.45(无趋势)": vol_scaled(close, target_vol=0.45),
        "波动率+MA200对称(base)": None,
        "非对称 出50/进200(base)": vol_asym(close, exit_ma=50, entry_ma=200, base_mode="base"),
        "非对称 出50/进200(cash)": vol_asym(close, exit_ma=50, entry_ma=200, base_mode="cash"),
        "非对称 出21/进200(base)": vol_asym(close, exit_ma=21, entry_ma=200, base_mode="base"),
        "非对称 出42/进100(base)": vol_asym(close, exit_ma=42, entry_ma=100, base_mode="base"),
    }
    # 对称 MA200 base（复用 soxl_trend 里的实现）
    from soxl_trend import vol_scaled_trend
    variants["波动率+MA200对称(base)"] = vol_scaled_trend(close, target_vol=0.45, trend_ma=200, down_mode="base")

    years = sorted(set(pd.to_datetime(dates).year))
    header = "策略".ljust(24) + "".join(f"{y:>11}" for y in years) + f"{'全期':>11} {'回撤':>7} {'Calmar':>7}"
    print(header)
    rows = []
    for name, tg in variants.items():
        eq = equity_curve(close, open_, tg)
        total = eq[-1] / eq[0] - 1
        dd = maxdd(eq)
        calmar = total / abs(dd) if dd < 0 else float("nan")
        py = {p["year"]: p for p in per_year(eq, dates)}
        line = name.ljust(24)
        for y in years:
            p = py.get(y)
            line += f"{p['ret']*100:>6.0f}/{p['dd']*100:>4.0f}" if p else " " * 10
        line += f"{total*100:>10.0f}% {dd*100:>6.0f}% {calmar:>7.2f}"
        print(line)
        rows.append({"策略": name, "全期收益%": total * 100, "全期回撤%": dd * 100, "Calmar": calmar})

    print("\n=== Calmar 排名 ===")
    print(pd.DataFrame(rows).sort_values("Calmar", ascending=False)
          .to_string(index=False, float_format=lambda x: f"{x:.2f}"))


if __name__ == "__main__":
    main()
