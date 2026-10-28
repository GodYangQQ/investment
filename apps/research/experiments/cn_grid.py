#!/usr/bin/env python3
"""A股 MA 突破 + 缓冲带 参数网格搜索：锁定最优 (y, buffer)。"""
import os, sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "experiments"))
sys.path.insert(0, ROOT)
from cn_backtest import load, evaluate, sig_ma_cross, sig_turtle, windows, INDICES  # noqa: E402

OUTDIR = os.path.join(ROOT, "experiments", "output")


def main():
    syms = ["sh000300", "sz399006", "sh000905", "sh000001", "sh000016"]
    ys = [21, 28, 34, 42, 50, 60, 70, 84, 100]
    bufs = [0.0, 0.005, 0.01, 0.015, 0.02]
    rows = []
    for sym in syms:
        df = load(sym, 1500)
        wins = windows(len(df), 5)
        name = INDICES.get(sym, sym)
        for y in ys:
            for b in bufs:
                sig = sig_ma_cross(df, y, b)
                r = evaluate(df, sig, wins)
                r.update({"index": name, "y": y, "buffer": b})
                rows.append(r)
        print(f"[{name}] 完成", flush=True)
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(OUTDIR, "cn_grid.csv"), index=False, encoding="utf-8-sig")
    # 每个指数单独 top
    for sym in syms:
        name = INDICES.get(sym, sym)
        sub = res[res["index"] == name].sort_values("mean_excess", ascending=False).head(6)
        print(f"\n--- {name} Top6 (y, buffer) ---")
        for _, r in sub.iterrows():
            print(f"  y={int(r.y):<3} buf={r.buffer:<5} 超额 {r.mean_excess*100:+6.1f}%  "
                  f"跑赢 {r.beat_rate*100:3.0f}%  夏普 {r.mean_sharpe:4.2f}  回撤 {r.worst_dd*100:5.1f}%")
    # 跨指数综合（按 y,buffer 平均）
    print("\n===== 跨指数综合 Top10 (y,buffer) =====")
    agg = (res.groupby(["y", "buffer"]).agg(
        mean_excess=("mean_excess", "mean"), beat_rate=("beat_rate", "mean"),
        mean_sharpe=("mean_sharpe", "mean"), worst_dd=("worst_dd", "min")).reset_index())
    for _, r in agg.sort_values("mean_excess", ascending=False).head(10).iterrows():
        print(f"  y={int(r.y):<3} buf={r.buffer:<5} 平均超额 {r.mean_excess*100:+6.1f}%  "
              f"跑赢 {r.beat_rate*100:3.0f}%  夏普 {r.mean_sharpe:4.2f}  最差回撤 {r.worst_dd*100:5.1f}%")


if __name__ == "__main__":
    main()
