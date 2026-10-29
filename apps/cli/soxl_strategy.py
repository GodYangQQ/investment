#!/usr/bin/env python3
"""
SOXL（3倍做多半导体 ETF）交易策略 + 回测
========================================
核心逻辑：SOXL 是 3x 日重置杠杆产品，长期有波动耗损，只能"顺势持有、逆势空仓"。

策略（双均线趋势过滤 + 分批止盈，回测最优）：
  持有      : close > MA50 且 close > MA200
  空仓      : 任一条件不满足即全额转现金（不做空）
  分批止盈  : 浮盈 +50% 减半，+100% 再减半（剩 1/4 用 MA50 移动止盈）
  趋势止盈  : 收盘跌破 MA50 清掉剩余仓位（主离场信号）
  说明      : 3x 会均值回补/耗损，分批落袋能提升收益并压低回撤
             回测: 纯趋势 +551%/回撤-41% → 加分批止盈 +671%/回撤-27%

用法:
    python apps/cli/soxl_strategy.py                 # 回测 + 当前信号
    python apps/cli/soxl_strategy.py --days 900
    python apps/cli/soxl_strategy.py --ma-fast 20 --ma-mid 50 --ma-slow 200 --atr-mult 3
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import numpy as np
import pandas as pd

from core.datasets.us_data import fetch_daily_kline, fetch_realtime_quote

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SYMBOL = "SOXL"
BENCH = "SOXX"


def load(symbol: str, days: int) -> pd.DataFrame:
    df = fetch_daily_kline(symbol, days=days).copy()
    df = df.sort_values("date").reset_index(drop=True)
    return df


def add_indicators(df: pd.DataFrame, ma_fast: int, ma_mid: int, ma_slow: int) -> pd.DataFrame:
    df = df.copy()
    df["ma_fast"] = df["close"].rolling(ma_fast).mean()
    df["ma_mid"] = df["close"].rolling(ma_mid).mean()
    df["ma_slow"] = df["close"].rolling(ma_slow).mean()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df


def backtest(df: pd.DataFrame, ma_fast: int, ma_mid: int, ma_slow: int, atr_mult: float,
             tp1: float = 0.5, tp2: float = 1.0, fee: float = 0.0005) -> dict:
    """双均线趋势过滤 + 分批止盈。tp1/tp2 为浮盈减半阈值（0=关闭）。"""
    n = len(df)
    pos = 0.0          # 当前仓位比例
    entry = 0.0
    scaled = 0         # 已止盈档数
    equity = 1.0
    curve = []
    trades = []
    for i in range(n):
        row = df.iloc[i]
        close = row["close"]
        ma_m = row["ma_mid"]; ma_s = row["ma_slow"]; atr = row["atr"]
        if pd.isna(ma_s) or pd.isna(atr):
            curve.append(equity)
            continue

        # 结算当日收益（用前一日仓位）
        if i > 0 and pos > 0:
            prev_close = df.iloc[i - 1]["close"]
            equity *= (1 + pos * (close / prev_close - 1))

        want_long = (close > ma_m) and (close > ma_s)
        if atr_mult and pos > 0 and close < row.get("peak", close) - atr_mult * atr:
            want_long = False

        if want_long and pos == 0:
            pos = 1.0; entry = close; scaled = 0
            trades.append({"date": row["date"], "action": "BUY", "price": close})
            equity *= (1 - fee)
        elif (not want_long) and pos > 0:
            trades.append({"date": row["date"], "action": "SELL", "price": close})
            equity *= (1 - fee)
            pos = 0.0

        # 分批止盈（持仓中）
        if want_long and pos > 0 and entry > 0:
            gain = close / entry - 1
            if tp1 and gain >= tp1 and scaled < 1:
                trades.append({"date": row["date"], "action": f"TP1 -50% @+{tp1*100:.0f}%", "price": close})
                pos = 0.5; scaled = 1
            if tp2 and gain >= tp2 and scaled < 2:
                trades.append({"date": row["date"], "action": f"TP2 -50% @+{tp2*100:.0f}%", "price": close})
                pos = 0.25; scaled = 2
        curve.append(equity)

    res = pd.DataFrame({"date": df["date"], "equity": curve, "close": df["close"]})
    res["bh"] = res["close"] / res["close"].iloc[0]
    return {"result": res, "trades": trades, "final": res["equity"].iloc[-1],
            "bh_final": res["bh"].iloc[-1]}


def perf(res: pd.DataFrame) -> dict:
    eq = res["equity"]
    ret = eq.pct_change().dropna()
    years = (res["date"].iloc[-1] - res["date"].iloc[0]).days / 365.25
    total = eq.iloc[-1] - 1
    cagr = (eq.iloc[-1]) ** (1 / years) - 1 if years > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    sharpe = (ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0
    bh_total = res["bh"].iloc[-1] - 1
    bh_dd = (res["bh"] / res["bh"].cummax() - 1).min()
    return {"total": total, "cagr": cagr, "maxdd": dd, "sharpe": sharpe,
            "bh_total": bh_total, "bh_dd": bh_dd, "years": years}


def current_signal(df: pd.DataFrame, ma_fast: int, ma_mid: int, ma_slow: int, atr_mult: float) -> dict:
    row = df.iloc[-1]
    close = row["close"]
    above_mid = bool(close > row["ma_mid"])
    above_slow = bool(close > row["ma_slow"])
    want_long = above_mid and above_slow
    if want_long:
        state = "持有 / 可持有多头（>MA50 且 >MA200）"
    elif above_slow:
        state = "空仓（跌破MA50，等重回MA50以上）"
    else:
        state = "空仓（跌破MA200，趋势转空，禁止抄底）"
    return {
        "date": row["date"], "close": close, "ma_fast": row["ma_fast"],
        "ma_mid": row["ma_mid"], "ma_slow": row["ma_slow"], "atr": row["atr"],
        "regime_bull": above_slow, "want_long": want_long, "state": state,
        "stop": row["ma_mid"],
    }


def run(days: int, ma_fast: int, ma_mid: int, ma_slow: int, atr_mult: float,
        tp1: float, tp2: float):
    df = load(SYMBOL, days)
    df = add_indicators(df, ma_fast, ma_mid, ma_slow)
    bt = backtest(df, ma_fast, ma_mid, ma_slow, atr_mult, tp1=tp1, tp2=tp2)
    p = perf(bt["result"])
    sig = current_signal(df, ma_fast, ma_mid, ma_slow, atr_mult)

    print("=" * 72)
    print(f"  SOXL 趋势策略回测  ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}, {len(df)}日)")
    print(f"  参数: MA{ma_fast}/MA{ma_mid}/MA{ma_slow} | 止盈 +{tp1*100:.0f}%/+{tp2*100:.0f}% 减半 | 移动止损 {atr_mult}×ATR")
    print("=" * 72)
    print(f"  策略总收益 : {p['total']*100:+.0f}%   年化 {p['cagr']*100:+.0f}%")
    print(f"  策略最大回撤: {p['maxdd']*100:.0f}%   夏普 {p['sharpe']:.2f}")
    print(f"  买入持有   : {p['bh_total']*100:+.0f}%   最大回撤 {p['bh_dd']*100:.0f}%")
    print(f"  交易次数   : {len(bt['trades'])}")
    print("-" * 72)
    print("  【当前信号】")
    print(f"  日期 {sig['date'].date()}  收盘 ${sig['close']:.2f}")
    print(f"  MA{ma_fast} ${sig['ma_fast']:.2f} | MA{ma_mid} ${sig['ma_mid']:.2f} | MA{ma_slow} ${sig['ma_slow']:.2f} | ATR14 ${sig['atr']:.2f}")
    print(f"  趋势: {'多头(>MA200)' if sig['regime_bull'] else '空头(<MA200)'}")
    print(f"  操作: {sig['state']}")
    print(f"  离场线(MA{ma_mid}): ${sig['stop']:.2f}")
    print(f"  止盈规则: 相对你的成本价 浮盈 +{tp1*100:.0f}% 减半、+{tp2*100:.0f}% 再减半，剩余破MA{ma_mid}清仓")
    print("=" * 72)
    return p, sig


def main():
    ap = argparse.ArgumentParser(description="SOXL 3x 半导体ETF 交易策略")
    ap.add_argument("--days", type=int, default=900)
    ap.add_argument("--ma-fast", type=int, default=20)
    ap.add_argument("--ma-mid", type=int, default=50)
    ap.add_argument("--ma-slow", type=int, default=200)
    ap.add_argument("--atr-mult", type=float, default=0.0, help="移动止损ATR倍数，0=关闭（默认）")
    ap.add_argument("--tp1", type=float, default=0.5, help="第一档止盈浮盈比例，如0.5=+50%减半")
    ap.add_argument("--tp2", type=float, default=1.0, help="第二档止盈浮盈比例，如1.0=+100%再减半")
    args = ap.parse_args()
    run(args.days, args.ma_fast, args.ma_mid, args.ma_slow, args.atr_mult, args.tp1, args.tp2)


if __name__ == "__main__":
    main()
