#!/usr/bin/env python3
"""
MA 5~40 细扫（步长1）：跌破卖、站上买
======================================
规则：收盘 > MA_Y 就持有，收盘 < MA_Y 就空仓（等价 MA_Y 上穿买入/下穿卖出，无缓冲带）。
对每根均线回测，对比买入持有，找出最优 MA。

用法:
    python experiments/ma_scan.py 002463 002851 002384
    python experiments/ma_scan.py 300750 --name 宁德时代
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
from cn_backtest import sig_ma_cross, backtest  # noqa: E402
from cn_stock_test import fetch_ak, to_backtest_df  # noqa: E402

MA_MIN, MA_MAX = 5, 40


def main():
    ap = argparse.ArgumentParser(description="MA 5~40 细扫")
    ap.add_argument("codes", nargs="+", help="A股6位代码，可多个")
    ap.add_argument("--name", default="", help="单只时的名称")
    args = ap.parse_args()

    for code in args.codes:
        raw = fetch_ak(code)
        if raw is None or len(raw) == 0:
            print(f"[{code}] 数据失败")
            continue
        df = to_backtest_df(raw)
        n = len(df)
        name = args.name if len(args.codes) == 1 and args.name else code
        print(f"\n===== {name}({code})  {n}根  "
              f"{df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()} =====")

        rows = []
        for y in range(MA_MIN, MA_MAX + 1):
            sig = sig_ma_cross(df, y, 0.0)
            r = backtest(df, sig, 0, n - 1)
            calmar = r.total / abs(r.maxdd) if r.maxdd < 0 else float("nan")
            rows.append({
                "MA": y, "策略%": r.total * 100, "基准%": r.bench * 100,
                "超额%": r.excess * 100, "回撤%": r.maxdd * 100,
                "Calmar": calmar, "交易": r.trades, "胜率%": r.winrate * 100,
            })
        res = pd.DataFrame(rows)
        print(res.to_string(index=False, float_format=lambda x: f"{x:.1f}"))

        best_ex = res.loc[res["超额%"].idxmax()]
        best_cal = res.loc[res["Calmar"].idxmax()]
        print(f"\n  最优 MA（按超额）: MA={int(best_ex['MA'])}  超额 {best_ex['超额%']:+.1f}%  "
              f"(策略 {best_ex['策略%']:+.1f}% vs 基准 {best_ex['基准%']:+.1f}%)")
        print(f"  最优 MA（按Calmar）: MA={int(best_cal['MA'])}  Calmar {best_cal['Calmar']:.2f}  "
              f"回撤 {best_cal['回撤%']:.1f}%")
        print(f"  买持基准           : 收益 {best_ex['基准%']:+.1f}%")
        beat = (res["超额%"] > 0).mean()
        print(f"  跑赢买持的 MA 占比 : {beat*100:.0f}%  ({int(res['超额%'].sum() > 0)} 个正超额 / {len(res)} 个)")


if __name__ == "__main__":
    main()
