#!/usr/bin/env python3
"""预加载 A股个股日线到 CSV，避免重复请求被限流。"""
import os
import time

import pandas as pd

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "cn_stocks")
os.makedirs(OUT, exist_ok=True)

CN_STOCKS = ["600519", "601318", "000858", "600036", "000333",
             "601899", "300750", "002594", "600276", "601088"]
NAMES = {"600519": "贵州茅台", "601318": "中国平安", "000858": "五粮液", "600036": "招商银行",
         "000333": "美的集团", "601899": "紫金矿业", "300750": "宁德时代", "002594": "比亚迪",
         "600276": "恒瑞医药", "601088": "中国神华"}


def main():
    import akshare as ak
    for code in CN_STOCKS:
        path = os.path.join(OUT, f"{code}.csv")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            df = pd.read_csv(path)
            print(f"{NAMES[code]}({code}) 已有 {len(df)} 根")
            continue
        ok = False
        for i in range(8):
            try:
                df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                        start_date="20200101", end_date="20261023", adjust="qfq")
                if df is not None and len(df) > 0:
                    df.to_csv(path, index=False, encoding="utf-8-sig")
                    print(f"{NAMES[code]}({code}) 拉到 {len(df)} 根  {df.iloc[0]['日期']} ~ {df.iloc[-1]['日期']}")
                    ok = True
                    break
            except Exception as e:
                print(f"{NAMES[code]}({code}) retry{i} {str(e)[:40]}")
            time.sleep(2.0 * (i + 1))
        if not ok:
            print(f"{NAMES[code]}({code}) 最终失败")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
