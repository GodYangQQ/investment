#!/usr/bin/env python3
"""
项目选股指标 因子回测（横截面 IC + 分位数）
============================================
把项目里出现的、能用历史K线计算的技术/量价因子，在一篮子 A股上做
月度横截面检验：每个月底按因子排序，算因子与「未来21日收益」的
Spearman IC（秩相关）与多空价差，判断哪个因子有预测力。

因子（对应项目指标）:
  mom_1d/5d/20d/60d/250d  动量/反转(calc_momentum_factors)
  gap_ma20/gap_ma60       趋势位置(score_trend_position)
  dd_60d                  60日高回撤(score_trend_position)
  atr_pct                 波动率(score_volatility)
  vol_ratio               量比(score_volume_price_action)
  bb_pos                  布林位置(score_fib_structure)
  main_net_20d            资金流占比(money_flow: signed dollar volume)
  rsi14                   RSI(sector_heat 健康度)

口径：月度换仓(每21日)、未来21日收益、零未来函数。
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cn_stock_test import fetch_ak, to_backtest_df  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "cn_stocks")

UNIVERSE = [
    "600519", "601318", "000858", "600036", "000333", "601899", "300750", "002594",
    "600276", "601088", "601012", "600030", "601888", "600887", "601166", "600900",
    "000651", "002475", "600809", "000568", "300059", "601398", "601857", "600028",
    "000002", "600048", "601668", "002415", "000063", "600031", "600585", "601225",
    "600438", "002460", "300014", "600309", "002714", "601633", "002230", "002371",
    "688111", "600745", "603501", "002049", "002463", "002851", "002384",
]


def rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h, l = df["high"], df["low"]
    v = df["volume"].astype(float)
    o = df["open"].astype(float)

    f = pd.DataFrame(index=df.index)
    f["mom_1d"] = c.pct_change(1)
    f["mom_5d"] = c.pct_change(5)
    f["mom_20d"] = c.pct_change(20)
    f["mom_60d"] = c.pct_change(60)
    f["mom_250d"] = c.pct_change(250)

    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    f["gap_ma20"] = c / ma20 - 1
    f["gap_ma60"] = c / ma60 - 1

    f["dd_60d"] = (h.rolling(60).max() - c) / h.rolling(60).max()
    f["above_low20"] = c / l.rolling(20).min() - 1

    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f["atr_pct"] = tr.ewm(alpha=1 / 14, adjust=False).mean() / c

    f["vol_ratio"] = v.rolling(5).mean() / v.rolling(20).mean()

    sd20 = c.rolling(20).std()
    bb_up = ma20 + 2 * sd20
    bb_lo = ma20 - 2 * sd20
    f["bb_pos"] = (c - bb_lo) / (bb_up - bb_lo)

    signed = v * c * np.sign(c - o)
    dollar = v * c
    f["main_net_20d"] = signed.rolling(20).sum() / dollar.rolling(20).sum()

    f["rsi14"] = rsi(c, 14)

    f["fwd_21d"] = c.shift(-21) / c - 1
    f.index = df["date"].values
    return f


def main():
    print(f"拉取 {len(UNIVERSE)} 只 A股日线 ...", flush=True)
    panels = {}
    ok = 0
    for code in UNIVERSE:
        raw = fetch_ak(code)
        if raw is None or len(raw) == 0:
            continue
        df = to_backtest_df(raw)
        if len(df) < 300:
            continue
        panels[code] = compute_factors(df)
        ok += 1
        time.sleep(0.15)
    print(f"成功 {ok} 只\n")

    FACTORS = ["mom_1d", "mom_5d", "mom_20d", "mom_60d", "mom_250d",
               "gap_ma20", "gap_ma60", "dd_60d", "above_low20", "atr_pct",
               "vol_ratio", "bb_pos", "main_net_20d", "rsi14"]

    # 汇总成 (month, stock, factor..., fwd) 记录
    records = []
    for code, f in panels.items():
        n = len(f)
        for t in range(260, n - 21, 21):  # 260 后开始，保证1年动量有值
            row = f.iloc[t]
            if pd.isna(row["fwd_21d"]):
                continue
            rec = {"stock": code}
            for fac in FACTORS:
                rec[fac] = row[fac]
            rec["fwd_21d"] = row["fwd_21d"]
            rec["date"] = f.index[t]
            records.append(rec)
    df = pd.DataFrame(records)
    df["month"] = df["date"].dt.to_period("M")
    print(f"有效样本 {len(df)} 条，覆盖 {df['month'].nunique()} 个月\n")

    rows = []
    for fac in FACTORS:
        ics = []
        longs = []
        shorts = []
        for m, g in df.groupby("month"):
            g = g.dropna(subset=[fac, "fwd_21d"])
            if len(g) < 8:
                continue
            ic, _ = spearmanr(g[fac], g["fwd_21d"])
            ics.append(ic)
            g2 = g.sort_values(fac)
            k = len(g2) // 5
            if k < 1:
                continue
            top = g2.iloc[-k:]["fwd_21d"].mean()
            bot = g2.iloc[:k]["fwd_21d"].mean()
            longs.append(top)
            shorts.append(bot)
        if not ics:
            continue
        ics = np.array(ics)
        ic_mean = ics.mean()
        ic_t = ic_mean / (ics.std(ddof=1) / np.sqrt(len(ics))) if len(ics) > 1 else np.nan
        ls_spread = (np.mean(longs) - np.mean(shorts)) * 100
        rows.append({
            "因子": fac, "IC均值": ic_mean, "IC_t值": ic_t,
            "IC>0占比%": (ics > 0).mean() * 100,
            "多空价差%": ls_spread, "正IC月份": (ics > 0).sum(), "月份数": len(ics),
        })

    res = pd.DataFrame(rows).sort_values("IC_t值", ascending=False)
    print("=" * 72)
    print("因子预测力排名（按 IC t值，|t|>2 视为显著）")
    print("=" * 72)
    print(res.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    print("\n说明：")
    print("  IC>0  → 因子越大未来收益越高（动量/趋势类，顺势选股）")
    print("  IC<0  → 因子越大未来收益越低（反转/均值回归类，逆势选股）")
    print("  |t|≥2 基本可靠，|t|≥3 较强。多空价差=按因子分5档的顶档-底档21日收益差。")


if __name__ == "__main__":
    main()
