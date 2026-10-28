#!/usr/bin/env python3
"""
MA42+1% 战法在个股上的测试：沪电股份 / 麦格米特 / 东山精密
==========================================================
策略（与 cn_backtest 的 sig_ma_cross 完全一致）：
  收盘上穿 MA42*(1+1%) 买入，收盘跌破 MA42*(1-1%) 卖出，只做多。
  信号收盘算、次日开盘成交、手续费 0.05%，基准=买入持有。
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cn_backtest import sig_ma_cross, backtest  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "cn_stocks")
os.makedirs(OUT, exist_ok=True)

STOCKS = {"002463": "沪电股份", "002851": "麦格米特", "002384": "东山精密"}


def fetch_ak(code: str) -> pd.DataFrame:
    path = os.path.join(OUT, f"{code}.csv")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return pd.read_csv(path)
    import akshare as ak
    df = None
    prefix = "sh" if code.startswith(("60", "68", "90")) else "sz"
    # 首选新浪源（东财源经常被断连）
    for i in range(4):
        try:
            df = ak.stock_zh_a_daily(symbol=prefix + code, start_date="20200101",
                                     end_date="20261023", adjust="qfq")
            if df is not None and len(df) > 0:
                df.to_csv(path, index=False, encoding="utf-8-sig")
                break
        except Exception as e:
            print(f"  sina retry{i} {str(e)[:50]}")
        time.sleep(1.0 * (i + 1))
    if df is None or len(df) == 0:
        for i in range(3):
            try:
                df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                        start_date="20200101", end_date="20261023", adjust="qfq")
                if df is not None and len(df) > 0:
                    df.to_csv(path, index=False, encoding="utf-8-sig")
                    break
            except Exception as e:
                print(f"  em retry{i} {str(e)[:50]}")
            time.sleep(2.0 * (i + 1))
    return df


def to_backtest_df(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    df = df.rename(columns={
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
    })
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").dropna(subset=["open", "close"]).reset_index(drop=True)


def main():
    print("=" * 78)
    print("MA42+1% 个股测试  (2020-01 ~ 2026-10, 手续费0.05%, 基准=买入持有)")
    print("=" * 78)
    for code, name in STOCKS.items():
        raw = fetch_ak(code)
        if raw is None or len(raw) == 0:
            print(f"[{name}] 数据失败")
            continue
        df = to_backtest_df(raw)
        n = len(df)
        sig = sig_ma_cross(df, 42, 0.01)
        r = backtest(df, sig, 0, n - 1)
        print(f"\n{name}({code})  {n}根  {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
        print(f"  策略收益  {r.total*100:+8.1f}%   基准买持 {r.bench*100:+8.1f}%   "
              f"超额 {r.excess*100:+8.1f}%")
        print(f"  策略回撤  {r.maxdd*100:6.1f}%   基准回撤 {r.bench_maxdd*100:6.1f}%   "
              f"交易 {r.trades} 次  胜率 {r.winrate*100:3.0f}%  在场 {r.exposure*100:3.0f}%")
        # 逐年
        df2 = df.copy()
        df2["year"] = df2["date"].dt.year
        print("  逐年（策略 / 基准）:")
        for y, g in df2.groupby("year"):
            i0 = g.index[0]
            i1 = g.index[-1]
            if i1 - i0 < 5:
                continue
            ry = backtest(df, sig, i0, i1)
            print(f"    {y}: 策略 {ry.total*100:+7.1f}%  基准 {ry.bench*100:+7.1f}%  超额 {ry.excess*100:+7.1f}%")

    print("\n说明：buffer=1% 表示上穿 MA42*1.01 才买、下破 MA42*0.99 才卖。")


if __name__ == "__main__":
    main()
