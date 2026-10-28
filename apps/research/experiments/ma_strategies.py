#!/usr/bin/env python3
"""
复杂均线策略横评
================
同一套数据/执行/费率/窗口下比较：
  cross      : 价格上穿/下穿 MA_Y（带缓冲带）
  dual       : 快均线上穿/下穿慢均线
  triple     : 价格 > MA20 > MA50 > MA200
  gmma       : Guppy 多均线（短组全部在长组之上）
  kama       : 价格 > KAMA
  hma        : 价格 > HMA
  macd       : MACD 线 > 信号线
  turtle     : 20日突破买入 / 10日跌破卖出
  supertrend : ATR SuperTrend
  ma_atr     : 价格 > MA50，ATR 移动止损

全部只做多；信号收盘计算、次日开盘成交；对比同窗口买入持有。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from core.datasets.us_data import fetch_daily_kline  # noqa: E402

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
WINDOW, STEP, FEE = 252, 21, 0.0005


# ---------------- 指标 ----------------
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def hull(s: pd.Series, n: int = 21) -> pd.Series:
    return wma(2 * wma(s, n // 2) - wma(s, n), int(np.sqrt(n)))


def kama(s: pd.Series, n: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    change = (s - s.shift(n)).abs()
    vol = s.diff().abs().rolling(n).sum()
    er = (change / vol.replace(0, np.nan)).fillna(0)
    sc = (er * (2 / (fast + 1) - 2 / (slow + 1)) + 2 / (slow + 1)) ** 2
    out = pd.Series(index=s.index, dtype=float)
    if len(s) <= n:
        return out
    out.iloc[n] = s.iloc[:n + 1].mean()
    for i in range(n + 1, len(s)):
        out.iloc[i] = out.iloc[i - 1] + sc.iloc[i] * (s.iloc[i] - out.iloc[i - 1])
    return out


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


# ---------------- 信号（0/1，代表次日应持否） ----------------
def sig_cross(df, y=42, buffer=0.01):
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


def sig_triple(df, a=20, b=50, c=200):
    ca = df["close"]
    ma_a, ma_b, ma_c = sma(ca, a), sma(ca, b), sma(ca, c)
    ok = (ca > ma_a) & (ma_a > ma_b) & (ma_b > ma_c) & ma_c.notna()
    return ok.astype(int)


def sig_gmma(df):
    short = [3, 5, 8, 10, 12, 15]
    long_ = [30, 35, 40, 45, 50, 60]
    ca = df["close"]
    s_ma = [sma(ca, n) for n in short]
    l_ma = [sma(ca, n) for n in long_]
    s_min = pd.concat(s_ma, axis=1).min(axis=1)
    l_max = pd.concat(l_ma, axis=1).max(axis=1)
    return ((s_min > l_max) & l_max.notna()).astype(int)


def sig_hma(df, n=21):
    h = hull(df["close"], n)
    return ((df["close"] > h) & h.notna()).astype(int)


def sig_kama(df, n=10):
    k = kama(df["close"], n)
    return ((df["close"] > k) & k.notna()).astype(int)


def sig_macd(df, fast=12, slow=26, sig=9):
    line = ema(df["close"], fast) - ema(df["close"], slow)
    signal = ema(line, sig)
    return ((line > signal) & signal.notna()).astype(int)


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


def sig_supertrend(df, n=10, mult=3.0):
    a = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    st = pd.Series(index=df.index, dtype=float)
    trend = pd.Series(1, index=df.index)
    for i in range(1, len(df)):
        if np.isnan(a.iloc[i]):
            continue
        if df["close"].iloc[i - 1] <= st.iloc[i - 1] if not np.isnan(st.iloc[i - 1]) else True:
            trend.iloc[i] = 1 if df["close"].iloc[i] > (st.iloc[i - 1] if not np.isnan(st.iloc[i - 1]) else lower.iloc[i]) else -1
        else:
            trend.iloc[i] = -1 if df["close"].iloc[i] < (st.iloc[i - 1] if not np.isnan(st.iloc[i - 1]) else upper.iloc[i]) else 1
        if trend.iloc[i] == 1:
            st.iloc[i] = max(lower.iloc[i], st.iloc[i - 1]) if not np.isnan(st.iloc[i - 1]) else lower.iloc[i]
        else:
            st.iloc[i] = min(upper.iloc[i], st.iloc[i - 1]) if not np.isnan(st.iloc[i - 1]) else upper.iloc[i]
    return (trend > 0).astype(int)


def sig_ma_atr(df, ma=50, n=14, mult=2.5):
    m = sma(df["close"], ma)
    a = atr(df, n)
    sig = pd.Series(0, index=df.index)
    pos = 0
    peak = 0.0
    for i in range(1, len(df)):
        c = df["close"].iloc[i]
        if np.isnan(m.iloc[i]) or np.isnan(a.iloc[i]):
            continue
        if pos == 0 and c > m.iloc[i]:
            pos = 1
            peak = c
        elif pos == 1:
            peak = max(peak, c)
            if c < m.iloc[i] or c < peak - mult * a.iloc[i]:
                pos = 0
        sig.iloc[i] = pos
    return sig


STRATEGIES = {
    "cross_42":        lambda d: sig_cross(d, 42, 0.01),
    "cross_21":        lambda d: sig_cross(d, 21, 0.01),
    "dual_20_50":      lambda d: sig_dual(d, 20, 50),
    "dual_50_200":     lambda d: sig_dual(d, 50, 200),
    "triple_20_50_200": lambda d: sig_triple(d, 20, 50, 200),
    "gmma":            sig_gmma,
    "kama_10":         lambda d: sig_kama(d, 10),
    "kama_20":         lambda d: sig_kama(d, 20),
    "hma_21":          lambda d: sig_hma(d, 21),
    "hma_55":          lambda d: sig_hma(d, 55),
    "macd":            sig_macd,
    "turtle_20_10":    lambda d: sig_turtle(d, 20, 10),
    "turtle_55_20":    lambda d: sig_turtle(d, 55, 20),
    "supertrend":      sig_supertrend,
    "ma50_atr":        sig_ma_atr,
}


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


def backtest_signal(df: pd.DataFrame, sig: pd.Series, i0: int, i1: int,
                    fee: float = FEE) -> Result:
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
    return Result(
        total=float(value - 1), bench=float(bench - 1), excess=float(value - bench),
        maxdd=maxdd, bench_maxdd=bench_dd, sharpe=sharpe, trades=len(pnls),
        winrate=sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0,
        exposure=expo / max(len(eq), 1),
    )


def windows(n: int, years: int = 5) -> list[tuple[int, int]]:
    end = n - 1
    start = max(WINDOW + 5, n - years * 252)
    out = []
    s = start
    while s + WINDOW <= end:
        out.append((s, s + WINDOW - 1))
        s += STEP
    return out or [(max(WINDOW + 5, 0), end)]


def load(ticker: str, days: int = 1600) -> pd.DataFrame:
    df = fetch_daily_kline(ticker, days=days).copy().sort_values("date").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["open", "close"]).reset_index(drop=True)


def evaluate(df: pd.DataFrame, sig: pd.Series, wins: list[tuple[int, int]]) -> dict:
    rs = [backtest_signal(df, sig, a, b) for a, b in wins]
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


def main() -> None:
    ap = argparse.ArgumentParser(description="复杂均线策略横评")
    ap.add_argument("--ticker", default="SOXX")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--names", default="all")
    ap.add_argument("--days", type=int, default=1600)
    args = ap.parse_args()

    names = list(STRATEGIES) if args.names == "all" else [x.strip() for x in args.names.split(",")]
    df = load(args.ticker, args.days)
    wins = windows(len(df), args.years)
    print(f"\n{args.ticker}  {len(df)}根日线  {len(wins)}个滚动1年窗口  手续费{FEE*100:.2f}%\n")
    rows = []
    for nm in names:
        fn = STRATEGIES.get(nm)
        if not fn:
            print("未知策略:", nm); continue
        sig = fn(df)
        r = evaluate(df, sig, wins)
        r["strategy"] = nm
        rows.append(r)
        print(f"  {nm:<18} 超额 {r['mean_excess']*100:+6.1f}%  跑赢 {r['beat_rate']*100:3.0f}%  "
              f"夏普 {r['mean_sharpe']:4.2f}  策略 {r['mean_total']*100:+6.1f}%  "
              f"基准 {r['mean_bench']*100:+6.1f}%  回撤 {r['worst_dd']*100:5.1f}%  "
              f"交易 {r['mean_trades']:4.1f}  在场 {r['mean_exposure']*100:3.0f}%")

    res = pd.DataFrame(rows)
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, f"strategies_{args.ticker}.csv")
    res.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n已保存: {path}")
    print("\n按平均超额排序：")
    for _, r in res.sort_values("mean_excess", ascending=False).iterrows():
        print(f"  {r.strategy:<18} {r.mean_excess*100:+6.1f}%  夏普 {r.mean_sharpe:4.2f}  回撤 {r.worst_dd*100:5.1f}%")
    print("\n按夏普排序：")
    for _, r in res.sort_values("mean_sharpe", ascending=False).head(8).iterrows():
        print(f"  {r.strategy:<18} 夏普 {r.mean_sharpe:4.2f}  超额 {r.mean_excess*100:+6.1f}%  回撤 {r.worst_dd*100:5.1f}%")


if __name__ == "__main__":
    main()
