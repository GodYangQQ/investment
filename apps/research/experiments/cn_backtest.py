#!/usr/bin/env python3
"""
A股指数策略回测：寻找能跑赢买入持有的方法
==========================================
A股近 6 年（2020-08 ~ 2026-10）买持表现差、回撤大，趋势跟踪大概率能跑赢。

策略（全部只做多，信号收盘算、次日开盘成交、手续费0.05%）：
  ma_cross      : 收盘上穿/下穿 MA_Y（带缓冲带）
  dual          : 快均线上穿/下穿慢均线
  turtle        : Donchian 突破（entry 日新高买 / exit 日新低卖）
  abs_mom       : 绝对动量（过去 mom 日收益 > 0 才持有）
  macd          : MACD 线 > 信号线
  trend_cross   : 趋势过滤 X（MA_X 向上） + 突破 Y

对比：同窗口「开盘买入、期末收盘卖出」的买入持有。

用法:
    python experiments/cn_backtest.py
    python experiments/cn_backtest.py --idx sh000300,sz399006
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from core.datasets.price_data import fetch_daily  # noqa: E402

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
WINDOW, STEP, FEE = 252, 21, 0.0005

INDICES = {
    "sh000001": "上证指数", "sh000300": "沪深300", "sz399006": "创业板指",
    "sh000905": "中证500", "sh000016": "上证50", "sz399001": "深证成指",
}


# ---------------- 指标 ----------------
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def load(sym: str, days: int = 1500) -> pd.DataFrame:
    rows = []
    for _ in range(4):
        rows, _ = fetch_daily(sym, days)
        if rows:
            break
        time.sleep(1.5)
    if not rows:
        raise RuntimeError(f"empty data for {sym}")
    df = pd.DataFrame(rows)
    df = df.rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").dropna(subset=["open", "close"]).reset_index(drop=True)


# ---------------- 信号 ----------------
def sig_ma_cross(df, y=42, buffer=0.0):
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


def sig_dual(df, fast=20, slow=50):
    f, s = sma(df["close"], fast), sma(df["close"], slow)
    return ((f > s) & s.notna()).astype(int)


def sig_turtle(df, entry=20, exit_n=10):
    hi = df["high"].rolling(entry).max().shift(1)
    lo = df["low"].rolling(exit_n).min().shift(1)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(hi.iloc[i]) or np.isnan(lo.iloc[i]):
            continue
        if pos == 0 and df["close"].iloc[i] > hi.iloc[i]:
            pos = 1
        elif pos == 1 and df["close"].iloc[i] < lo.iloc[i]:
            pos = 0
        sig.iloc[i] = pos
    return sig


def sig_abs_mom(df, mom=126):
    c = df["close"]
    return ((c > c.shift(mom)) & c.shift(mom).notna()).astype(int)


def sig_macd(df, fast=12, slow=26, sig=9):
    line = ema(df["close"], fast) - ema(df["close"], slow)
    signal = ema(line, sig)
    return ((line > signal) & signal.notna()).astype(int)


def sig_trend_cross(df, x=45, y=21, buffer=0.0):
    c = df["close"]
    ma_x, ma_y = sma(c, x), sma(c, y)
    up, dn = ma_y * (1 + buffer), ma_y * (1 - buffer)
    x_up = ma_x > ma_x.shift(5)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(ma_y.iloc[i]) or np.isnan(ma_x.iloc[i]):
            continue
        if pos == 0 and x_up.iloc[i] and c.iloc[i - 1] <= up.iloc[i - 1] and c.iloc[i] > up.iloc[i]:
            pos = 1
        elif pos == 1 and (c.iloc[i - 1] >= dn.iloc[i - 1] and c.iloc[i] < dn.iloc[i]):
            pos = 0
        sig.iloc[i] = pos
    return sig


# ---------------- 回测 ----------------
@dataclass
class Result:
    total: float
    bench: float
    excess: float
    maxdd: float
    bench_maxdd: float
    sharpe: float
    trades: int
    winrate: float
    exposure: float


def backtest(df: pd.DataFrame, sig: pd.Series, i0: int, i1: int, fee: float = FEE) -> Result:
    close, open_ = df["close"].values, df["open"].values
    value = 1.0
    pos = 0
    entry = 0.0
    pnls = []
    eq = []
    expo = 0
    for t in range(i0, i1 + 1):
        if t > i0:
            want = int(sig.iloc[t - 1]) if not pd.isna(sig.iloc[t - 1]) else 0
            if want == 1 and pos == 0:
                value *= (1 - fee)
                value *= close[t] / open_[t]
                pos = 1
                entry = open_[t]
            elif want == 0 and pos == 1:
                value *= open_[t] / close[t - 1]
                value *= (1 - fee)
                pos = 0
                if entry:
                    pnls.append(open_[t] / entry - 1)
            elif pos == 1:
                value *= close[t] / close[t - 1]
        eq.append(value)
        expo += 1 if pos == 1 else 0
    if pos == 1:
        value *= (1 - fee)
        if entry:
            pnls.append(close[i1] / entry - 1)
        eq[-1] = value
    bench = (close[i1] / open_[i0]) * (1 - fee) ** 2
    bench_eq = close[i0:i1 + 1] / open_[i0]
    bench_dd = float(np.min(bench_eq / np.maximum.accumulate(bench_eq) - 1))
    eq = np.array(eq)
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([])
    maxdd = float(np.min(eq / np.maximum.accumulate(eq) - 1)) if len(eq) else 0.0
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if len(rets) > 1 and np.std(rets) > 0 else 0.0
    return Result(total=float(value - 1), bench=float(bench - 1), excess=float(value - bench),
                  maxdd=maxdd, bench_maxdd=bench_dd, sharpe=sharpe, trades=len(pnls),
                  winrate=sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0,
                  exposure=expo / max(len(eq), 1))


def windows(n: int, years: int = 5) -> list[tuple[int, int]]:
    end = n - 1
    start = max(WINDOW + 5, n - years * 252)
    out = []
    s = start
    while s + WINDOW <= end:
        out.append((s, s + WINDOW - 1))
        s += STEP
    return out or [(max(WINDOW + 5, 0), end)]


def evaluate(df, sig, wins) -> dict:
    rs = [backtest(df, sig, a, b) for a, b in wins]
    ex = np.array([r.excess for r in rs])
    return {
        "mean_excess": ex.mean(), "median_excess": float(np.median(ex)),
        "beat_rate": float(np.mean(ex > 0)),
        "mean_sharpe": np.mean([r.sharpe for r in rs]),
        "mean_total": np.mean([r.total for r in rs]),
        "mean_bench": np.mean([r.bench for r in rs]),
        "worst_dd": min(r.maxdd for r in rs),
        "mean_bench_dd": np.mean([r.bench_maxdd for r in rs]),
        "mean_trades": np.mean([r.trades for r in rs]),
        "mean_winrate": np.mean([r.winrate for r in rs]),
        "mean_exposure": np.mean([r.exposure for r in rs]),
    }


def strategies():
    return {
        "ma_cross_21_b0":     lambda d: sig_ma_cross(d, 21, 0.0),
        "ma_cross_42_b0":     lambda d: sig_ma_cross(d, 42, 0.0),
        "ma_cross_42_b01":    lambda d: sig_ma_cross(d, 42, 0.01),
        "ma_cross_60_b0":     lambda d: sig_ma_cross(d, 60, 0.0),
        "ma_cross_100_b0":    lambda d: sig_ma_cross(d, 100, 0.0),
        "dual_20_50":         lambda d: sig_dual(d, 20, 50),
        "dual_50_200":        lambda d: sig_dual(d, 50, 200),
        "dual_20_60":         lambda d: sig_dual(d, 20, 60),
        "turtle_20_10":       lambda d: sig_turtle(d, 20, 10),
        "turtle_55_20":       lambda d: sig_turtle(d, 55, 20),
        "abs_mom_63":         lambda d: sig_abs_mom(d, 63),
        "abs_mom_126":        lambda d: sig_abs_mom(d, 126),
        "abs_mom_252":        lambda d: sig_abs_mom(d, 252),
        "macd":               sig_macd,
        "trend_cross_45_21":  lambda d: sig_trend_cross(d, 45, 21),
        "trend_cross_130_42": lambda d: sig_trend_cross(d, 130, 42),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="A股指数策略回测")
    ap.add_argument("--idx", default="sh000001,sh000300,sz399006,sh000905,sh000016",
                    help="逗号分隔的指数代码")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--days", type=int, default=1500)
    args = ap.parse_args()

    syms = [s.strip() for s in args.idx.split(",") if s.strip()]
    all_rows = []
    for sym in syms:
        name = INDICES.get(sym, sym)
        try:
            df = load(sym, args.days)
        except Exception as e:
            print(f"[{sym}] 数据失败: {e}")
            continue
        if len(df) < WINDOW + 60:
            print(f"[{sym}] 数据不足: {len(df)}")
            continue
        wins = windows(len(df), args.years)
        print(f"\n===== {name}({sym})  {len(df)}根  {len(wins)}个滚动1年窗口 =====")
        strat = strategies()
        for nm, fn in strat.items():
            sig = fn(df)
            r = evaluate(df, sig, wins)
            r["strategy"] = f"{name}_{nm}"
            all_rows.append(r)
            print(f"  {nm:<20} 超额 {r['mean_excess']*100:+6.1f}%  跑赢 {r['beat_rate']*100:3.0f}%  "
                  f"夏普 {r['mean_sharpe']:4.2f}  策略 {r['mean_total']*100:+6.1f}%  "
                  f"基准 {r['mean_bench']*100:+6.1f}%  回撤 {r['worst_dd']*100:5.1f}%  "
                  f"基准回撤 {r['mean_bench_dd']*100:5.1f}%")

    res = pd.DataFrame(all_rows)
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, "cn_indices.csv")
    res.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n已保存: {path}\n")

    print("===== 跨指数综合 Top15（按平均超额）=====")
    agg = (res.groupby("strategy").agg(
        mean_excess=("mean_excess", "mean"), beat_rate=("beat_rate", "mean"),
        mean_sharpe=("mean_sharpe", "mean"), worst_dd=("worst_dd", "min"),
        mean_trades=("mean_trades", "mean")).reset_index())
    for _, r in agg.sort_values("mean_excess", ascending=False).head(15).iterrows():
        print(f"  {r.strategy:<22} 超额 {r.mean_excess*100:+6.1f}%  跑赢 {r.beat_rate*100:3.0f}%  "
              f"夏普 {r.mean_sharpe:4.2f}  最差回撤 {r.worst_dd*100:5.1f}%")


if __name__ == "__main__":
    main()
