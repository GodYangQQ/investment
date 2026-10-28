#!/usr/bin/env python3
"""
SOXL / SOXX 当前建议仓位（波动率目标 + MA200 趋势）
==================================================
规则：SOXL仓位 = clip(45% / SOXL近21日年化波动率, 0, 90%)
      SOXX跌破MA200 → SOXL仓位清零
      SOXX仓位 = 100% - SOXL仓位
每天收盘后运行本脚本，输出当天收盘信号对应的明日开盘建议仓位。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from core.datasets.us_data import fetch_daily_kline  # noqa: E402

TARGET_VOL = 0.45
MAX_W = 0.90
VOL_WINDOW = 21
TREND_MA = 200


def main():
    soxx = fetch_daily_kline("SOXX", days=1600)
    soxl = fetch_daily_kline("SOXL", days=1600)
    soxx = soxx.set_index("date").sort_index()
    soxl = soxl.set_index("date").sort_index()
    idx = soxx.index.intersection(soxl.index)
    soxx = soxx.loc[idx]
    soxl = soxl.loc[idx]

    # SOXL 21日年化波动率
    ret = soxl["close"].pct_change()
    rv = float(ret.rolling(VOL_WINDOW).std().iloc[-1] * np.sqrt(252))
    # SOXX MA200
    ma200 = float(soxx["close"].rolling(TREND_MA).mean().iloc[-1])
    close = float(soxx["close"].iloc[-1])
    last_date = soxx.index[-1].date()

    w_soxl = float(np.clip(TARGET_VOL / rv, 0.0, MAX_W)) if rv > 0 else 0.0
    below_ma = close < ma200
    if below_ma:
        w_soxl = 0.0
    w_soxx = 1.0 - w_soxl

    print("=" * 46)
    print(f"SOXL/SOXX 建议仓位  ({last_date})")
    print("=" * 46)
    print(f"SOXX 收盘价       : {close:8.2f}")
    print(f"SOXX MA200        : {ma200:8.2f}")
    print(f"SOXL 21日年化波动率: {rv*100:5.1f}%")
    print(f"趋势闸门          : {'跌破MA200 → 清杠杆' if below_ma else '站上MA200 → 允许杠杆'}")
    print("-" * 46)
    print(f"  SOXL 建议仓位    : {w_soxl*100:5.1f}%")
    print(f"  SOXX 建议仓位    : {w_soxx*100:5.1f}%")
    beta = w_soxx * 1.0 + w_soxl * 3.0
    print(f"  组合等效β        : {beta:.2f}x")
    print("-" * 46)
    print("说明：收盘算信号，明日开盘执行；永远满仓，只调 SOXL/SOXX 比例。")


if __name__ == "__main__":
    main()
