#!/usr/bin/env python3
"""
通道突破择时策略：突破 N 日新高买入，跌破 M 日新低卖出
====================================================
用户描述：「上升突破某个一直没突破的高点=突破，下降跌破某个一直没跌破的低点=跌破」
即 Donchian 通道突破（Turtle 同源）。

口径：信号收盘算、次日开盘成交、手续费 0.05%、只做多、对比同标的买入持有。
测试对象：
  1) 24 资产池（美股ETF + A股指数）
  2) 随机 10 美股个股 + 10 A股个股（A股用 akshare 全量历史）
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from core.datasets.us_data import fetch_daily_kline  # noqa: E402
from core.datasets.price_data import fetch_daily  # noqa: E402

FEE = 0.0005

US_STOCKS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM", "XOM", "JNJ"]
CN_STOCKS = ["600519", "601318", "000858", "600036", "000333",
             "601899", "300750", "002594", "600276", "601088"]
CN_NAMES = {"600519": "贵州茅台", "601318": "中国平安", "000858": "五粮液", "600036": "招商银行",
            "000333": "美的集团", "601899": "紫金矿业", "300750": "宁德时代", "002594": "比亚迪",
            "600276": "恒瑞医药", "601088": "中国神华"}

UNIVERSE = [
    "SPY", "QQQ", "SOXX", "SMH", "XLK", "VGT", "XSD", "PSI",
    "TLT", "IEF", "SHY", "LQD", "HYG", "BND", "AGG",
    "SOXL", "TQQQ", "UPRO",
    "sh000001", "sh000300", "sz399006", "sh000905", "sh000016", "sz399001",
]
CLASS = {}
for _s in ["SPY", "QQQ", "SOXX", "SMH", "XLK", "VGT", "XSD", "PSI"]:
    CLASS[_s] = "US_equity"
for _s in ["TLT", "IEF", "SHY", "LQD", "HYG", "BND", "AGG"]:
    CLASS[_s] = "US_bond"
for _s in ["SOXL", "TQQQ", "UPRO"]:
    CLASS[_s] = "US_lev"
for _s in ["sh000001", "sh000300", "sz399006", "sh000905", "sh000016", "sz399001"]:
    CLASS[_s] = "A_index"


_CACHE: dict[str, pd.DataFrame] = {}


def load_cn_stock(code: str) -> pd.DataFrame:
    if code in _CACHE:
        return _CACHE[code]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "cn_stocks", f"{code}.csv")
    if os.path.exists(path):
        df = pd.read_csv(path)
        df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low", "成交量": "volume"})
    else:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date="20200101", end_date="20261023", adjust="qfq")
        df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low", "成交量": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "close"]).reset_index(drop=True)
    _CACHE[code] = df
    return df


def load_any(sym: str) -> pd.DataFrame:
    if sym in CN_STOCKS:
        return load_cn_stock(sym)
    if sym.startswith(("sh", "sz", "bj")):
        rows, _ = fetch_daily(sym, 1500)
        df = pd.DataFrame(rows).rename(columns={"t": "date", "o": "open", "h": "high",
                                                "l": "low", "c": "close", "v": "volume"})
        df["date"] = pd.to_datetime(df["date"])
    else:
        df = fetch_daily_kline(sym, days=1500).copy()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").dropna(subset=["open", "close"]).reset_index(drop=True)


def breakout_signal(df: pd.DataFrame, n: int, m: int) -> pd.Series:
    """突破 N 日新高买入，跌破 M 日新低卖出。shift(1) 避免未来函数。"""
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(m).min().shift(1)
    c = df["close"]
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if pd.isna(hi.iloc[i]) or pd.isna(lo.iloc[i]):
            continue
        if pos == 0 and c.iloc[i] > hi.iloc[i]:
            pos = 1
        elif pos == 1 and c.iloc[i] < lo.iloc[i]:
            pos = 0
        sig.iloc[i] = pos
    return sig


def backtest(df: pd.DataFrame, sig: pd.Series, fee: float = FEE) -> dict:
    close, open_ = df["close"].values, df["open"].values
    value = 1.0
    pos = 0
    entry = 0.0
    eq = [1.0]
    trades = 0
    for t in range(1, len(df)):
        want = int(sig.iloc[t - 1]) if not pd.isna(sig.iloc[t - 1]) else 0
        if want == 1 and pos == 0:
            value *= (1 - fee)
            value *= close[t] / open_[t]
            pos = 1
            entry = open_[t]
            trades += 1
        elif want == 0 and pos == 1:
            value *= open_[t] / close[t - 1]
            value *= (1 - fee)
            pos = 0
        elif pos == 1:
            value *= close[t] / close[t - 1]
        eq.append(value)
    if pos == 1:
        value *= (1 - fee)
        eq[-1] = value
    bench = (close[-1] / open_[0]) * (1 - fee) ** 2
    eq = np.array(eq)
    bench_eq = close / open_[0]
    bench_dd = float(np.min(bench_eq / np.maximum.accumulate(bench_eq) - 1))
    maxdd = float(np.min(eq / np.maximum.accumulate(eq) - 1))
    return {"total": value - 1, "bench": bench - 1, "excess": value - bench,
            "maxdd": maxdd, "bench_dd": bench_dd, "trades": trades}


def run_group(symbols: list[str], n: int, m: int, label: str) -> None:
    print(f"\n===== {label} | 突破 {n} 日新高 / 跌破 {m} 日新低 =====")
    exs, dds, bdds, beats = [], [], [], 0
    per = {}
    for sym in symbols:
        try:
            df = load_any(sym)
            if len(df) < 300:
                print(f"  {sym:12s} 数据不足 {len(df)}")
                continue
            sig = breakout_signal(df, n, m)
            r = backtest(df, sig)
            exs.append(r["excess"])
            dds.append(r["maxdd"])
            bdds.append(r["bench_dd"])
            if r["excess"] > 0:
                beats += 1
            cl = CLASS.get(sym, "US_stock" if sym in US_STOCKS else "A_stock")
            per.setdefault(cl, {"ex": [], "dd": []})
            per[cl]["ex"].append(r["excess"])
            per[cl]["dd"].append(r["maxdd"])
            print(f"  {sym:12s} 策略 {r['total']*100:+7.1f}%  买持 {r['bench']*100:+7.1f}%  "
                  f"超额 {r['excess']*100:+7.1f}%  回撤 {r['maxdd']*100:6.1f}%  {r['trades']}次")
        except Exception as e:
            print(f"  {sym:12s} 失败 {str(e)[:50]}")
    if exs:
        print(f"  → 平均超额 {np.mean(exs)*100:+6.1f}%  跑赢 {beats}/{len(exs)}  "
              f"平均回撤 {np.mean(dds)*100:5.1f}%  基准回撤 {np.mean(bdds)*100:5.1f}%")
        for cl, v in per.items():
            if v["ex"]:
                print(f"     [{cl}] 超额 {np.mean(v['ex'])*100:+6.1f}%  回撤 {np.mean(v['dd'])*100:5.1f}%")


def main() -> None:
    # 1) 24 资产池
    for n, m in [(20, 10), (55, 20), (100, 20), (200, 50)]:
        run_group(UNIVERSE, n, m, "24资产池(ETF+指数)")
    # 2) 个股（10 美股 + 10 A股）
    print("\n\n############ 随机个股测试 ############")
    print("预加载 A股个股（akshare 全量历史）...")
    for code in CN_STOCKS:
        try:
            df = load_cn_stock(code)
            print(f"  {CN_NAMES[code]}({code}): {len(df)} 根  {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
        except Exception as e:
            print(f"  {CN_NAMES[code]}({code}) 失败 {str(e)[:40]}")
        time.sleep(0.5)
    for n, m in [(20, 10), (55, 20), (100, 20)]:
        run_group(US_STOCKS, n, m, "10 美股个股")
        run_group(CN_STOCKS, n, m, "10 A股个股")


if __name__ == "__main__":
    main()
