#!/usr/bin/env python3
"""
A股每日资金摘要（digest）
========================
一条命令看完：
  1) 大盘主力资金（近5日，判断主力在买还是卖）
  2) 板块资金流 Top流入/流出（钱去了哪）
  3) 个股主力净流入/流出 Top（谁在被抢/被砸）
  4) 龙虎榜机构净买 Top（真实席位，事后确认）
  5) 一句话总结

用法:
    python apps/cli/cn_digest.py
    python apps/cli/cn_digest.py --top 5
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

RUN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RUN_DIR)
sys.path.insert(0, os.path.join(RUN_DIR, "..", ".."))

import cn_moneyflow as mf  # noqa: E402
import cn_lhb as lhb  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(asctime)s [%(levelname)s] %(message)s")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def digest(top: int = 5):
    ak = mf._ak()
    print("\n" + "=" * 92)
    print(f"  A股每日资金摘要  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 92)

    # 1) 大盘
    try:
        m = ak.stock_market_fund_flow()
        if m is not None and not m.empty:
            main_c = [c for c in m.columns if "主力净流入" in c and "净额" in c][0]
            print("\n【1】大盘主力资金（近5日，亿）")
            print(f"  {'日期':<12}{'上证%':>7}{'主力净流入':>13}{'超大单':>13}")
            xl_c = [c for c in m.columns if "超大单净流入" in c and "净额" in c]
            for _, r in m.tail(5).iloc[::-1].iterrows():
                xl = mf._yi(r[xl_c[0]]) if xl_c else ""
                print(f"  {str(r['日期'])[:10]:<12}{_num(r.get('上证-涨跌幅')):>7.2f}"
                      f"{mf._yi(r[main_c]):>13}{xl:>13}")
            net5 = sum(_num(x) for x in m.tail(5)[main_c])
            trend = "主力净流出" if net5 < 0 else "主力净流入"
            print(f"  → 近5日累计 {mf._yi(net5)}（{trend}）")
    except Exception as e:
        print("  [1] 大盘资金流失败:", str(e)[:60])

    # 2) 板块
    try:
        s = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        if s is not None and not s.empty:
            net_c = [c for c in s.columns if c.startswith("今日") and "主力净流入" in c and "净额" in c][0]
            s = s.copy(); s[net_c] = s[net_c].map(_num)
            s = s.sort_values(net_c, ascending=False)
            print(f"\n【2】行业板块资金流（今日，亿）")
            print("  流入 Top: " + " | ".join(
                f"{str(r['名称'])[:8]} {mf._yi(r[net_c])}" for _, r in s.head(top).iterrows()))
            print("  流出 Top: " + " | ".join(
                f"{str(r['名称'])[:8]} {mf._yi(r[net_c])}" for _, r in s.tail(top).iloc[::-1].iterrows()))
    except Exception as e:
        print("  [2] 板块资金流失败:", str(e)[:60])

    # 3) 个股
    try:
        inflow = mf._em_fundflow_today(True, top)
        outflow = mf._em_fundflow_today(False, top)
        print("\n【3】个股主力净流入（今日，亿）")
        if inflow:
            print("  买入: " + " | ".join(f"{r['名称'][:6]}({r['代码']}) {mf._yi(r['主力净流入'])}" for r in inflow))
        if outflow:
            print("  卖出: " + " | ".join(f"{r['名称'][:6]}({r['代码']}) {mf._yi(r['主力净流入'])}" for r in outflow))
    except Exception as e:
        print("  [3] 个股资金流失败:", str(e)[:60])

    # 4) 龙虎榜机构净买
    try:
        df = ak.stock_lhb_jgstatistic_em(symbol="近一月")
        if df is not None and not df.empty and "机构净买额" in df.columns:
            df = df.copy(); df["机构净买额"] = df["机构净买额"].map(_num)
            df = df.sort_values("机构净买额", ascending=False)
            print("\n【4】龙虎榜机构净买 Top（近一月，亿）")
            print("  " + " | ".join(
                f"{str(r['名称'])[:6]}({r['代码']}) {mf._yi(r['机构净买额'])}" for _, r in df.head(top).iterrows()))
    except Exception as e:
        print("  [4] 龙虎榜机构净买失败:", str(e)[:60])

    print("\n" + "-" * 92)
    print("  ⚠️ 1-3 为'大单推算'的估算；4 为交易所披露的真实席位（事后）。仅供参考。")
    print("=" * 92)


def main():
    ap = argparse.ArgumentParser(description="A股每日资金摘要")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    digest(args.top)


if __name__ == "__main__":
    main()
