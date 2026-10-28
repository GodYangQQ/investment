#!/usr/bin/env python3
"""
单只股票 MA + 缓冲带 参数寻优
==============================
对一只股票做「均线突破 MA_Y + 缓冲带 ±buffer」的网格搜索，
找出最优 (MA, buffer)。同时给出稳健性判断（是平台还是尖峰）。

用法:
    python experiments/stock_ma_optimizer.py 002463 --name 沪电股份
    python experiments/stock_ma_optimizer.py 300750
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
from cn_backtest import sig_ma_cross, backtest, evaluate, windows  # noqa: E402
from cn_stock_test import fetch_ak, to_backtest_df  # noqa: E402

YS = [21, 28, 34, 42, 50, 60, 70, 84, 100, 130, 160, 200]
BUFS = [0.0, 0.005, 0.01, 0.015, 0.02, 0.03]


def main():
    ap = argparse.ArgumentParser(description="单股 MA+buffer 寻优")
    ap.add_argument("code", help="A股6位代码，如 002463")
    ap.add_argument("--name", default="", help="股票名称（可选）")
    ap.add_argument("--years", type=int, default=5, help="滚动窗口年数")
    args = ap.parse_args()

    code = args.code.strip()
    raw = fetch_ak(code)
    if raw is None or len(raw) == 0:
        print("数据获取失败")
        return
    df = to_backtest_df(raw)
    n = len(df)
    wins = windows(n, args.years)
    name = args.name or code
    print(f"===== {name}({code})  {n}根  "
          f"{df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}  "
          f"滚动{args.years}年窗口 {len(wins)}个 =====\n")

    rows = []
    for y in YS:
        for b in BUFS:
            sig = sig_ma_cross(df, y, b)
            r = backtest(df, sig, 0, n - 1)
            ev = evaluate(df, sig, wins)
            calmar = r.total / abs(r.maxdd) if r.maxdd < 0 else float("nan")
            rows.append({
                "y": y, "buffer": b,
                "full_total": r.total, "full_bench": r.bench, "full_excess": r.excess,
                "full_dd": r.maxdd, "calmar": calmar, "trades": r.trades, "winrate": r.winrate,
                "roll_excess": ev["mean_excess"], "beat_rate": ev["beat_rate"],
                "roll_dd": ev["worst_dd"],
            })
    res = pd.DataFrame(rows)

    # 全期超额热力表（y × buffer）
    piv = res.pivot_table(index="y", columns="buffer", values="full_excess") * 100
    piv = piv.reindex(YS).reindex(BUFS, axis=1)
    print("== 全期超额收益%  (行=MA周期, 列=buffer) ==")
    print(piv.round(0).to_string())
    print()

    print("== Top8 按 滚动窗口平均超额（稳健） ==")
    for _, r in res.sort_values("roll_excess", ascending=False).head(8).iterrows():
        print(f"  MA={int(r.y):<3} buf={r.buffer:<5} 滚动超额 {r.roll_excess*100:+6.1f}%  "
              f"跑赢 {r.beat_rate*100:3.0f}%  全期超额 {r.full_excess*100:+6.1f}%  "
              f"Calmar {r.calmar:5.2f}  回撤 {r.full_dd*100:5.1f}%")

    print("\n== Top8 按 全期超额（单次最优，可能过拟合） ==")
    for _, r in res.sort_values("full_excess", ascending=False).head(8).iterrows():
        print(f"  MA={int(r.y):<3} buf={r.buffer:<5} 全期超额 {r.full_excess*100:+6.1f}%  "
              f"策略 {r.full_total*100:+6.1f}%  基准 {r.full_bench*100:+6.1f}%  "
              f"回撤 {r.full_dd*100:5.1f}%  交易 {int(r.trades)}次  胜率 {r.winrate*100:3.0f}%")

    # 稳健性判断
    pos = float((res["roll_excess"] > 0).mean())
    print(f"\n== 稳健性判断 ==")
    print(f"  全网格 {len(res)} 个组合中，滚动超额为正的占 {pos*100:.0f}%")
    best_y = int(res.loc[res["roll_excess"].idxmax(), "y"])
    best_b = res.loc[res["roll_excess"].idxmax(), "buffer"]
    if pos > 0.6:
        print(f"  判定：MA+buffer 在该股上普遍有效，推荐 MA={best_y}, buffer={best_b}（宽平台，较稳）")
    elif pos > 0.3:
        print(f"  判定：只在部分参数有效，推荐 MA={best_y}, buffer={best_b}（中等，注意是平台还是尖峰）")
    else:
        print(f"  判定：绝大多数参数跑输买持 → 该股不适合 MA 择时，建议直接买入持有")


if __name__ == "__main__":
    main()
