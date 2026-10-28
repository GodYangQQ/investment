#!/usr/bin/env python3
"""
把成交量加进 MA42+1%：测试几种用法
==================================
基线：MA42 + 1% 缓冲（收盘上穿 MA42*1.01 买，下穿 MA42*0.99 卖）
成交量变体：
  baseline        基线（无成交量，作对照）
  vol_reentry     用户想法：跌破均线照卖；但均线下方出现「放量上涨」就提前接回
  vol_confirm     只在「放量突破」时买入（突破当日成交量 > 阈值）
  vwma            用成交量加权均线 VWMA42 替代 SMA42
  vol_combo       放量突破买入 + 均线下放量上涨接回 + 放量/缩量过滤离场
口径：信号收盘算、次日开盘成交、手续费 0.05%、只做多、对比买持。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from core.datasets.us_data import fetch_daily_kline  # noqa: E402
from core.datasets.price_data import fetch_daily  # noqa: E402

FEE = 0.0005
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

US_ETFS = ["SPY", "QQQ", "SOXX", "SMH", "XLK", "VGT", "XSD", "PSI"]
US_STOCKS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM", "XOM", "JNJ"]
CN_INDEX = ["sh000001", "sh000300", "sz399006", "sh000905", "sh000016"]
CN_STOCKS = ["600519", "601318", "000858", "600036", "000333",
             "601899", "300750", "002594", "600276", "601088"]


def load_any(sym: str) -> pd.DataFrame:
    if sym in CN_STOCKS:
        path = os.path.join(OUT, "cn_stocks", f"{sym}.csv")
        df = pd.read_csv(path)
        df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low", "成交量": "volume"})
    elif sym.startswith(("sh", "sz", "bj")):
        rows, _ = fetch_daily(sym, 1500)
        df = pd.DataFrame(rows).rename(columns={"t": "date", "o": "open", "h": "high",
                                                "l": "low", "c": "close", "v": "volume"})
    else:
        df = fetch_daily_kline(sym, days=1500).copy()
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").dropna(subset=["open", "close"]).reset_index(drop=True)


def vwma(df, n):
    vp = df["close"] * df["volume"]
    vs = df["volume"].rolling(n).sum()
    return vp.rolling(n).sum() / vs


def sma(s, n):
    return s.rolling(n).mean()


def _backtest(df, sig):
    close, open_ = df["close"].values, df["open"].values
    value = 1.0
    pos = 0
    eq = [1.0]
    for t in range(1, len(df)):
        want = int(sig.iloc[t - 1]) if not pd.isna(sig.iloc[t - 1]) else 0
        if want == 1 and pos == 0:
            value *= (1 - FEE); value *= close[t] / open_[t]; pos = 1
        elif want == 0 and pos == 1:
            value *= open_[t] / close[t - 1]; value *= (1 - FEE); pos = 0
        elif pos == 1:
            value *= close[t] / close[t - 1]
        eq.append(value)
    if pos == 1:
        value *= (1 - FEE); eq[-1] = value
    bench = (close[-1] / open_[0]) * (1 - FEE) ** 2
    eq = np.array(eq)
    return {"total": value - 1, "bench": bench - 1, "excess": value - bench,
            "maxdd": float(np.min(eq / np.maximum.accumulate(eq) - 1))}


# ---------------- 信号 ----------------
def sig_baseline(df, y=42, buffer=0.01):
    c, m = df["close"], sma(df["close"], y)
    up, dn = m * (1 + buffer), m * (1 - buffer)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(m.iloc[i]):
            continue
        if pos == 0 and c.iloc[i - 1] <= up.iloc[i - 1] and c.iloc[i] > up.iloc[i]:
            pos = 1
        elif pos == 1 and c.iloc[i - 1] >= dn.iloc[i - 1] and c.iloc[i] < dn.iloc[i]:
            pos = 0
        sig.iloc[i] = pos
    return sig


def sig_vol_reentry(df, y=42, buffer=0.01, vol_thresh=1.5, up_days=1):
    """用户想法：跌破均线照卖；均线下方「放量上涨」提前接回。"""
    c, m = df["close"], sma(df["close"], y)
    up, dn = m * (1 + buffer), m * (1 - buffer)
    vr = df["volume"] / df["volume"].rolling(20).mean()
    up_streak = c > c.shift(up_days)  # 收盘高于 N 日前
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(m.iloc[i]) or np.isnan(vr.iloc[i]):
            continue
        cross_up = c.iloc[i - 1] <= up.iloc[i - 1] and c.iloc[i] > up.iloc[i]
        cross_dn = c.iloc[i - 1] >= dn.iloc[i - 1] and c.iloc[i] < dn.iloc[i]
        # 均线下方放量上涨：价涨 + 放量
        below_ma_vol_up = (c.iloc[i] < m.iloc[i]) and (c.iloc[i] > c.iloc[i - 1]) \
            and (vr.iloc[i] > vol_thresh)
        if pos == 0 and (cross_up or below_ma_vol_up):
            pos = 1
        elif pos == 1 and cross_dn:
            pos = 0
        sig.iloc[i] = pos
    return sig


def sig_vol_confirm(df, y=42, buffer=0.01, vol_thresh=1.5):
    """只在放量突破时买入。"""
    c, m = df["close"], sma(df["close"], y)
    up, dn = m * (1 + buffer), m * (1 - buffer)
    vr = df["volume"] / df["volume"].rolling(20).mean()
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(m.iloc[i]) or np.isnan(vr.iloc[i]):
            continue
        cross_up = c.iloc[i - 1] <= up.iloc[i - 1] and c.iloc[i] > up.iloc[i]
        cross_dn = c.iloc[i - 1] >= dn.iloc[i - 1] and c.iloc[i] < dn.iloc[i]
        if pos == 0 and cross_up and vr.iloc[i] > vol_thresh:
            pos = 1
        elif pos == 1 and cross_dn:
            pos = 0
        sig.iloc[i] = pos
    return sig


def sig_vwma(df, y=42, buffer=0.01):
    """用成交量加权均线替代简单均线。"""
    c, m = df["close"], vwma(df, y)
    up, dn = m * (1 + buffer), m * (1 - buffer)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(m.iloc[i]):
            continue
        if pos == 0 and c.iloc[i - 1] <= up.iloc[i - 1] and c.iloc[i] > up.iloc[i]:
            pos = 1
        elif pos == 1 and c.iloc[i - 1] >= dn.iloc[i - 1] and c.iloc[i] < dn.iloc[i]:
            pos = 0
        sig.iloc[i] = pos
    return sig


def sig_vol_combo(df, y=42, buffer=0.01, vol_thresh=1.5):
    """组合：放量突破买入 + 均线下放量上涨接回 + 缩量跌破才卖（放量下跌也卖）。"""
    c, m = df["close"], sma(df["close"], y)
    up, dn = m * (1 + buffer), m * (1 - buffer)
    vr = df["volume"] / df["volume"].rolling(20).mean()
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(m.iloc[i]) or np.isnan(vr.iloc[i]):
            continue
        cross_up = c.iloc[i - 1] <= up.iloc[i - 1] and c.iloc[i] > up.iloc[i]
        cross_dn = c.iloc[i - 1] >= dn.iloc[i - 1] and c.iloc[i] < dn.iloc[i]
        below_vol_up = (c.iloc[i] < m.iloc[i]) and (c.iloc[i] > c.iloc[i - 1]) and (vr.iloc[i] > vol_thresh)
        if pos == 0 and (cross_up or below_vol_up):
            pos = 1
        elif pos == 1 and cross_dn:
            pos = 0
        sig.iloc[i] = pos
    return sig


STRATEGIES = {
    "baseline_ma42_1%":           lambda d: sig_baseline(d),
    "vol_reentry_1.5":            lambda d: sig_vol_reentry(d, vol_thresh=1.5),
    "vol_reentry_2.0":            lambda d: sig_vol_reentry(d, vol_thresh=2.0),
    "vol_confirm_1.5":            lambda d: sig_vol_confirm(d, vol_thresh=1.5),
    "vol_confirm_2.0":            lambda d: sig_vol_confirm(d, vol_thresh=2.0),
    "vwma_42_1%":                 lambda d: sig_vwma(d),
    "vol_combo_1.5":              lambda d: sig_vol_combo(d, vol_thresh=1.5),
}


def run_group(symbols, label):
    print(f"\n===== {label} =====")
    agg = {}
    for name, fn in STRATEGIES.items():
        exs, dds, beats = [], [], 0
        for sym in symbols:
            try:
                df = load_any(sym)
                if len(df) < 300:
                    continue
                r = _backtest(df, fn(df))
                exs.append(r["excess"])
                dds.append(r["maxdd"])
                if r["excess"] > 0:
                    beats += 1
            except Exception as e:
                print(f"  [{name}] {sym} 失败 {str(e)[:40]}")
        agg[name] = {"excess": float(np.mean(exs)) if exs else 0,
                     "dd": float(np.mean(dds)) if dds else 0,
                     "beats": f"{beats}/{len(exs)}" if exs else "0/0"}
    base = agg.get("baseline_ma42_1%", {"excess": 0, "dd": 0})
    print(f"  {'策略':<22}{'平均超额':>10}{'平均回撤':>10}{'跑赢':>8}   相对基线超额")
    for name, v in agg.items():
        delta = v["excess"] - base["excess"]
        print(f"  {name:<22}{v['excess']*100:+9.1f}%{v['dd']*100:+9.1f}%{v['beats']:>8}   {delta*100:+7.1f}%")


def main():
    run_group(CN_INDEX, "A股指数 (5个)")
    run_group(CN_STOCKS, "A股个股 (10个)")
    run_group(US_ETFS, "美股ETF (8个)")
    run_group(US_STOCKS, "美股个股 (10个)")


if __name__ == "__main__":
    main()
