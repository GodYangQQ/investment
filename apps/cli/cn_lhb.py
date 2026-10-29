#!/usr/bin/env python3
"""
A股龙虎榜 — 看"最牛资金"怎么交易
===============================
数据源：东方财富龙虎榜（akshare）。公开数据，盘后更新。

用法:
    python apps/cli/cn_lhb.py                      # 当日龙虎榜明细 + 机构买卖
    python apps/cli/cn_lhb.py --date 20261013
    python apps/cli/cn_lhb.py --mode yyb           # 营业部（游资）排行
    python apps/cli/cn_lhb.py --mode jigou         # 机构专用席位
    python apps/cli/cn_lhb.py --mode detail --top 30
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DATA_SOURCE = ("数据源：东方财富 datacenter（聚合交易所公开披露的龙虎榜）。"
               "原始规则：个股异动时，交易所公布当日买卖金额前5的营业部席位。"
               "注意：无官方'顶级游资'名单，营业部表现排行是统计推断，非身份认定。")


def _fmt(v, unit="万"):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    if unit == "亿":
        return f"{v/1e8:.2f}亿"
    return f"{v/1e4:.0f}万"


def _ak():
    import akshare as ak
    return ak


def _lhb_detail(ak, date: str):
    """取某日龙虎榜；失败自动回退到最近若干个交易日。"""
    from datetime import timedelta
    d = datetime.strptime(date, "%Y%m%d")
    for _ in range(7):
        ds = d.strftime("%Y%m%d")
        try:
            df = ak.stock_lhb_detail_em(start_date=ds, end_date=ds)
            if df is not None and not df.empty:
                return df, ds
        except Exception:
            pass
        d -= timedelta(days=1)
    return None, date


def mode_detail(date: str, top: int):
    ak = _ak()
    df, used = _lhb_detail(ak, date)
    if df is None or df.empty:
        print(f"{date} 起近 7 天无龙虎榜数据（非交易日/未更新）")
        return
    date = used
    df = df.copy()
    for c in ["龙虎榜净买额", "龙虎榜买入额", "龙虎榜卖出额", "涨跌幅"]:
        if c in df.columns:
            df[c] = df[c].astype(float)
    df = df.sort_values("龙虎榜净买额", ascending=False)
    print(f"\n{'='*96}\n  龙虎榜明细 {date}  (共 {len(df)} 条)\n{'='*96}")
    print(f"  {DATA_SOURCE}")
    print(f"  {'代码':<8}{'名称':<10}{'涨跌%':>8}{'净买额':>12}{'上榜原因'}")
    print("  " + "-" * 90)
    for _, r in df.head(top).iterrows():
        print(f"  {str(r['代码']):<8}{str(r['名称'])[:8]:<10}{r.get('涨跌幅',0):>8.1f}"
              f"{_fmt(r.get('龙虎榜净买额',0)):>12}  {str(r.get('解读',''))[:38]}")
    # 净卖出
    print("\n  —— 净卖出前 5 ——")
    for _, r in df.tail(5).iterrows():
        print(f"  {str(r['代码']):<8}{str(r['名称'])[:8]:<10}{r.get('涨跌幅',0):>8.1f}"
              f"{_fmt(r.get('龙虎榜净买额',0)):>12}  {str(r.get('解读',''))[:38]}")
    _save(df.head(top).to_dict("records"), f"cn_lhb_detail_{date}.csv")


def mode_jigou(date: str, top: int):
    ak = _ak()
    from datetime import timedelta
    df, used = None, date
    d = datetime.strptime(date, "%Y%m%d")
    for _ in range(7):
        ds = d.strftime("%Y%m%d")
        try:
            df = ak.stock_lhb_jgmmtj_em(start_date=ds, end_date=ds)
            if df is not None and not df.empty:
                used = ds
                break
        except Exception:
            df = None
        d -= timedelta(days=1)
    if df is None or df.empty:
        print(f"{date} 起近 7 天无机构席位数据")
        return
    date = used
    df = df.copy()
    for c in ["机构买入净额", "涨跌幅"]:
        if c in df.columns:
            df[c] = df[c].astype(float)
    df = df.sort_values("机构买入净额", ascending=False)
    print(f"\n{'='*96}\n  机构专用席位 {date}\n{'='*96}")
    print(f"  {DATA_SOURCE}")
    print(f"  {'代码':<8}{'名称':<10}{'涨跌%':>8}{'买方机构':>8}{'卖方机构':>8}{'机构净买':>12}")
    print("  " + "-" * 70)
    for _, r in df.head(top).iterrows():
        print(f"  {str(r['代码']):<8}{str(r['名称'])[:8]:<10}{r.get('涨跌幅',0):>8.1f}"
              f"{int(r.get('买方机构数',0)):>8}{int(r.get('卖方机构数',0)):>8}"
              f"{_fmt(r.get('机构买入净额',0)):>12}")
    _save(df.head(top).to_dict("records"), f"cn_lhb_jigou_{date}.csv")


def mode_yyb(period: str, top: int, min_count: int = 10):
    """营业部席位排行——按上榜后表现统计（非'游资身份'认定）。
    min_count: 最小买入次数，过滤'只上榜1次'的噪声样本。"""
    ak = _ak()
    df = ak.stock_lhb_yybph_em(symbol=period)
    if df is None or df.empty:
        print("无营业部数据")
        return
    df = df.copy()
    cnt_col = "上榜后5天-买入次数"
    col = "上榜后5天-平均涨幅"
    for c in [cnt_col, col]:
        if c in df.columns:
            df[c] = df[c].astype(float)
    if cnt_col in df.columns:
        df = df[df[cnt_col] >= min_count]
    if col in df.columns:
        df = df.sort_values(col, ascending=False)
    print(f"\n{'='*104}\n  营业部席位表现排行 — {period}（样本≥{min_count}次，按上榜后5天平均涨幅）\n{'='*104}")
    print(f"  {DATA_SOURCE}")
    show = [c for c in ["营业部名称", "上榜后5天-买入次数", "上榜后5天-平均涨幅",
                        "上榜后5天-上涨概率", "上榜后10天-平均涨幅"] if c in df.columns]
    print(f"\n  共 {len(df)} 个席位满足样本≥{min_count}")
    print("  " + "".join(f"{c[:16]:<18}" for c in show))
    print("  " + "-" * 96)
    for _, r in df.head(top).iterrows():
        print("  " + "".join(f"{str(r.get(c,''))[:16]:<18}" for c in show))
    _save(df.head(top).to_dict("records"), f"cn_lhb_yyb_{period}.csv")


def mode_jstat(period: str, top: int):
    """机构龙虎榜统计（近一月/三月等）——机构净买排行。"""
    ak = _ak()
    df = ak.stock_lhb_jgstatistic_em(symbol=period)
    if df is None or df.empty:
        print("无机构统计数据")
        return
    df = df.copy().sort_values("机构净买额", ascending=False)
    print(f"\n{'='*100}\n  机构龙虎榜净买排行 — {period}\n{'='*100}")
    print(f"  {DATA_SOURCE}")
    print(f"\n  {'代码':<8}{'名称':<10}{'涨跌%':>7}{'上榜':>5}{'机构净买':>12}{'近1月%':>9}{'近1年%':>9}")
    print("  " + "-" * 70)
    for _, r in df.head(top).iterrows():
        print(f"  {str(r['代码']):<8}{str(r['名称'])[:8]:<10}{r.get('涨跌幅',0):>7.1f}"
              f"{int(r.get('上榜次数',0)):>5}{_fmt(r.get('机构净买额',0)):>12}"
              f"{r.get('近1个月涨跌幅',0):>9.1f}{r.get('近1年涨跌幅',0):>9.1f}")
    _save(df.head(top).to_dict("records"), f"cn_lhb_jstat_{period}.csv")


def _save(rows: list, fname: str):
    if not rows:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, fname)
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print(f"\n  已保存: {path}")
    except Exception as e:
        log.warning("保存失败: %s", e)


def main():
    ap = argparse.ArgumentParser(description="A股龙虎榜（看最牛资金怎么交易）")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--mode", choices=["detail", "jigou", "yyb", "jstat"], default="detail")
    ap.add_argument("--period", default="近一月", help="周期: 近一月/近三月/近半年/近一年")
    ap.add_argument("--min-count", type=int, default=10, help="营业部最小买入次数（过滤小样本噪声）")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    if args.mode == "detail":
        mode_detail(args.date, args.top)
    elif args.mode == "jigou":
        mode_jigou(args.date, args.top)
    elif args.mode == "jstat":
        mode_jstat(args.period, args.top)
    else:
        mode_yyb(args.period, args.top, args.min_count)


if __name__ == "__main__":
    main()
