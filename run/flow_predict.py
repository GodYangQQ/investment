#!/usr/bin/env python3
"""
主力资金流向 能否预测板块未来涨跌？—— 实证
============================================
对东财行业板块，取近 N 日：
  · 主力净占比(f57)  与 当日涨跌(f63)         → 同期相关
  · 主力净占比(t)    与 未来1日/5日涨跌        → 预测相关
  · 横截面：每日按主力净占比分组，比较未来收益

用法:
    python run/flow_predict.py
    python run/flow_predict.py --boards 60 --days 120
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

import numpy as np
import pandas as pd

from cn_data import _em_get

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")


def board_list(n: int) -> list[tuple[str, str]]:
    url = ("https://push2.eastmoney.com/api/qt/clist/get?fid=f6&po=1&pz=%d&pn=1&np=1"
           "&fltt=2&invt=2&fs=m:90+t:2&fields=f12,f14" % n)
    js = _em_get(url)
    diff = ((js or {}).get("data", {}) or {}).get("diff")
    items = list(diff.values()) if isinstance(diff, dict) else (diff or [])
    return [(it.get("f12"), it.get("f14")) for it in items]


def flow_hist(code: str, days: int) -> pd.DataFrame:
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=%d&klt=101"
           "&secid=90.%s&fields1=f1,f2,f3,f7"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63" % (days, code))
    js = _em_get(url)
    kl = ((js or {}).get("data", {}) or {}).get("klines") or []
    rows = []
    for k in kl:
        p = str(k).split(",")
        try:
            rows.append({"date": p[0], "main": float(p[1]), "ratio": float(p[6]), "pct": float(p[12])})
        except (IndexError, ValueError):
            continue
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description="资金流能否预测板块未来涨跌")
    ap.add_argument("--boards", type=int, default=60)
    ap.add_argument("--days", type=int, default=150)
    args = ap.parse_args()

    boards = board_list(args.boards)
    print(f"取行业板块 {len(boards)} 个，各取近 {args.days} 日...")
    panel = []
    ok = 0
    for code, name in boards:
        df = flow_hist(code, args.days)
        if len(df) < 40:
            continue
        ok += 1
        df["code"] = code; df["name"] = name
        df["ret"] = df["pct"] / 100.0
        panel.append(df)
    if not panel:
        print("无数据"); return
    df = pd.concat(panel, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["code", "date"])
    # 未来收益（按各板块内部）
    df["fwd1"] = df.groupby("code")["ret"].shift(-1)
    df["fwd5"] = df.groupby("code")["ret"].transform(
        lambda s: (1 + s).rolling(5).apply(np.prod, raw=True).shift(-5) - 1)
    df["fwd20"] = df.groupby("code")["ret"].transform(
        lambda s: (1 + s).rolling(20).apply(np.prod, raw=True).shift(-20) - 1)

    print(f"有效板块 {ok} 个，样本 {len(df)} 条\n")
    print("=" * 64)
    print("  一、相关系数（主力净占比 vs 涨跌）")
    print("=" * 64)
    for col, lab in [("ret", "同期(当日)"), ("fwd1", "未来1日"), ("fwd5", "未来5日"), ("fwd20", "未来20日")]:
        c = df["ratio"].corr(df[col])
        # 各板块分别算再平均
        per = df.dropna(subset=[col]).groupby("code").apply(
            lambda g: g["ratio"].corr(g[col])).dropna()
        print(f"  {lab:<10} 混合相关 {c:+.3f}   各板块均值 {per.mean():+.3f}  (中位 {per.median():+.3f}, n={len(per)})")

    print("\n" + "=" * 64)
    print("  二、横截面：每日按主力净占比分组，看『未来』收益")
    print("=" * 64)
    d = df.dropna(subset=["fwd5"]).copy()
    d["grp"] = d.groupby("date")["ratio"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if s.notna().sum() >= 5 else np.nan)
    g = d.dropna(subset=["grp"]).groupby("grp")[["fwd1", "fwd5"]].mean() * 100
    names = {0: "最低(净流出最多)", 1: "偏低", 2: "中", 3: "偏高", 4: "最高(净流入最多)"}
    print(f"  {'分组':<16}{'未来1日%':>10}{'未来5日%':>10}")
    for i, row in g.iterrows():
        print(f"  {names.get(int(i),i):<16}{row['fwd1']:>10.3f}{row['fwd5']:>10.3f}")
    if len(g) >= 2:
        spread1 = g.loc[4, "fwd1"] - g.loc[0, "fwd1"]
        spread5 = g.loc[4, "fwd5"] - g.loc[0, "fwd5"]
        print(f"\n  最高组 − 最低组：  未来1日 {spread1:+.3f}%   未来5日 {spread5:+.3f}%")
        print("  （>0 表示'资金流入多的板块未来跑赢'，<0 表示反向/反转）")

    print("\n" + "=" * 64)
    print("  结论解读")
    print("=" * 64)
    c_sync = df["ratio"].corr(df["ret"])
    c_f1 = df["ratio"].corr(df["fwd1"])
    c_f5 = df["ratio"].corr(df["fwd5"])
    print(f"  同期相关 {c_sync:+.3f}：资金流与当日涨跌高度同向（几乎定义如此）")
    print(f"  未来1日   {c_f1:+.3f}：预测力")
    print(f"  未来5日   {c_f5:+.3f}：预测力")
    print("  ⚠️ 资金流是『同步/追随』指标；对未来方向的预测力通常很弱，极端处甚至反向。")


if __name__ == "__main__":
    main()
