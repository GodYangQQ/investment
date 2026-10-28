#!/usr/bin/env python3
"""
A股"近似主力资金流向"聚合
=========================
⚠️ 重要：市面上的"主力资金流向"是**按成交单金额分档（小/中/大/超大单）推算的估算**，
   不是真实账户数据。交易所不披露账户级流向。本工具只做多源交叉参考。

数据源（东方财富 / 同花顺，经 akshare）：
  - 大盘主力净流入（stock_market_fund_flow）
  - 板块资金流排名（stock_sector_fund_flow_rank）行业/概念
  - 个股主力净流入排名（stock_individual_fund_flow_rank）
  - 同花顺概念资金流（stock_fund_flow_concept）
  - 大宗交易（stock_dzjy_mrmx）—— 含买卖方营业部席位

用法:
    python apps/cli/cn_moneyflow.py                 # 全景
    python apps/cli/cn_moneyflow.py --mode market
    python apps/cli/cn_moneyflow.py --mode sector --sector-type 概念资金流
    python apps/cli/cn_moneyflow.py --mode stock --top 20
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CAVEAT = ("⚠️ '主力资金'是按大单成交额推算的**估算**，非真实账户；龙虎榜才是个股真实席位（但事后）。"
          "不同平台算法不同、数值不一致。")


def _ak():
    import akshare as ak
    return ak


def _yi(v):
    try:
        return f"{float(v)/1e8:+.2f}亿"
    except (TypeError, ValueError):
        return "?"


def _col(df, kw, prefer="今日"):
    for c in df.columns:
        if c.startswith(prefer) and kw in c and "净额" in c:
            return c
    for c in df.columns:
        if kw in c and "净额" in c:
            return c
    return None


def mode_market(ak):
    df = ak.stock_market_fund_flow()
    if df is None or df.empty:
        print("无大盘资金流数据"); return
    cols = ["日期", "上证-涨跌幅", "主力净流入-净额", "超大单净流入-净额", "大单净流入-净额",
            "中单净流入-净额", "小单净流入-净额"]
    cols = [c for c in cols if c in df.columns]
    print(f"\n{'='*90}\n  大盘主力资金（近 5 日，单位：亿）\n{'='*90}")
    print("  " + "".join(f"{c[:12]:<16}" for c in cols))
    print("  " + "-" * 86)
    for _, r in df.tail(5).iloc[::-1].iterrows():
        cells = []
        for c in cols:
            v = r[c]
            cells.append(f"{_yi(v):<16}" if "净额" in c else f"{str(v)[:12]:<16}")
        print("  " + "".join(cells))


def mode_sector(ak, sector_type: str, top: int):
    df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type=sector_type)
    if df is None or df.empty:
        print(f"无 {sector_type} 数据"); return
    name_c = "名称"
    net_c = _col(df, "主力净流入")
    xl_c = _col(df, "超大单净流入")
    chg_c = "今日涨跌幅"
    df = df.copy()
    if net_c:
        df[net_c] = df[net_c].astype(float)
        df = df.sort_values(net_c, ascending=False)
    print(f"\n{'='*90}\n  板块资金流 — {sector_type}（今日，估算）\n{'='*90}")
    print(f"  【净流入 Top {top}】")
    print(f"  {'板块':<16}{'涨跌%':>8}{'主力净流入':>14}{'超大单':>14}")
    for _, r in df.head(top).iterrows():
        print(f"  {str(r.get(name_c,''))[:14]:<16}{r.get(chg_c,0):>8.2f}"
              f"{_yi(r.get(net_c,0)):>14}{_yi(r.get(xl_c,0)) if xl_c else '':>14}")
    print(f"\n  【净流出 Top {top}】")
    for _, r in df.tail(top).iloc[::-1].iterrows():
        print(f"  {str(r.get(name_c,''))[:14]:<16}{r.get(chg_c,0):>8.2f}"
              f"{_yi(r.get(net_c,0)):>14}{_yi(r.get(xl_c,0)) if xl_c else '':>14}")


_FS_ALL = ("m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,"
           "m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2")


def _em_fundflow_today(order_desc: bool, top: int):
    """用 cn_data 的多主机轮换直接取东财个股主力净流入排行。"""
    import sys as _s
    _s.path.insert(0, ROOT_DIR)
    from core.datasets.cn_data import _em_get
    po = 1 if order_desc else 0
    url = (f"https://push2.eastmoney.com/api/qt/clist/get?fid=f62&po={po}&pz=100&pn=1"
           f"&np=1&fltt=2&invt=2&fs={_FS_ALL}"
           "&fields=f12,f14,f2,f3,f62,f184,f66")
    js = _em_get(url)
    rows = []
    if js and js.get("data") and js["data"].get("diff") is not None:
        diff = js["data"]["diff"]
        items = list(diff.values()) if isinstance(diff, dict) else diff
        for it in items[:top]:
            rows.append({
                "代码": it.get("f12"), "名称": it.get("f14"),
                "涨跌%": it.get("f3"), "主力净流入": it.get("f62"),
                "主力净占比": it.get("f184"), "超大单": it.get("f66"),
            })
    return rows


def mode_stock(ak, top: int, period: str = "今日"):
    # 优先东财(clist, 多主机)；失败再退回 akshare
    inflow = _em_fundflow_today(True, top)
    outflow = _em_fundflow_today(False, top)
    if inflow or outflow:
        print(f"\n{'='*90}\n  个股主力净流入排行 — 今日（估算，东财，单位亿）\n{'='*90}")
        print(f"  {'代码':<8}{'名称':<10}{'涨跌%':>7}{'主力净流入':>13}{'超大单':>13}{'净占比%':>9}")
        print(f"  —— 净流入 Top {len(inflow)} ——")
        for r in inflow:
            print(f"  {str(r['代码']):<8}{str(r['名称'])[:8]:<10}{float(r['涨跌%']):>7.1f}"
                  f"{_yi(r['主力净流入']):>13}{_yi(r['超大单']):>13}{float(r['主力净占比']):>9.1f}")
        print(f"  —— 净流出 Top {len(outflow)} ——")
        for r in outflow:
            print(f"  {str(r['代码']):<8}{str(r['名称'])[:8]:<10}{float(r['涨跌%']):>7.1f}"
                  f"{_yi(r['主力净流入']):>13}{_yi(r['超大单']):>13}{float(r['主力净占比']):>9.1f}")
        return
    df = ak.stock_individual_fund_flow_rank(indicator=period)
    if df is None or df.empty:
        print("无个股资金流数据"); return
    net_c = _col(df, "主力净流入", prefer=period)
    xl_c = _col(df, "超大单净流入", prefer=period)
    chg_c = None
    for c in df.columns:
        if c.endswith("涨跌幅"):
            chg_c = c; break
    if not net_c:
        print("列解析失败", list(df.columns)[:6]); return
    import pandas as pd
    df = df.copy()
    for c in [net_c, xl_c, chg_c]:
        if c and c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[net_c]).sort_values(net_c, ascending=False)
    print(f"\n{'='*90}\n  个股主力净流入排行 — {period}（估算，单位亿）\n{'='*90}")
    print(f"  {'代码':<8}{'名称':<10}{'涨跌%':>7}{'主力净流入':>13}{'超大单':>13}")
    print("  " + "-" * 60)
    print(f"  —— 净流入 Top {top} ——")
    for _, r in df.head(top).iterrows():
        print(f"  {str(r['代码']):<8}{str(r['名称'])[:8]:<10}{(r.get(chg_c,0) if chg_c else 0):>7.1f}"
              f"{_yi(r[net_c]):>13}{( _yi(r[xl_c]) if xl_c else ''):>13}")
    print(f"  —— 净流出 Top {top} ——")
    for _, r in df.tail(top).iloc[::-1].iterrows():
        print(f"  {str(r['代码']):<8}{str(r['名称'])[:8]:<10}{(r.get(chg_c,0) if chg_c else 0):>7.1f}"
              f"{_yi(r[net_c]):>13}{( _yi(r[xl_c]) if xl_c else ''):>13}")


def mode_block(ak, top: int):
    try:
        df = ak.stock_dzjy_mrmx()
    except Exception as e:
        print("大宗交易取数失败:", str(e)[:80]); return
    if df is None or df.empty:
        print("今日无大宗交易数据"); return
    print(f"\n{'='*90}\n  大宗交易（含买卖方营业部席位）\n{'='*90}")
    print(f"  {'代码':<8}{'简称':<10}{'成交价':>9}{'成交额':>12}  买方营业部 / 卖方营业部")
    for _, r in df.head(top).iterrows():
        print(f"  {str(r.get('证券代码','')):<8}{str(r.get('证券简称',''))[:8]:<10}"
              f"{r.get('成交价',0):>9}{_yi(r.get('成交额',0)):>12}  "
              f"{str(r.get('买方营业部',''))[:18]} / {str(r.get('卖方营业部',''))[:18]}")


def main():
    ap = argparse.ArgumentParser(description="A股近似主力资金流向")
    ap.add_argument("--mode", choices=["all", "market", "sector", "stock", "block"], default="all")
    ap.add_argument("--sector-type", default="行业资金流", help="行业资金流/概念资金流/地域资金流")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    ak = _ak()
    print(f"\n{'#'*90}\n  {CAVEAT}\n{'#'*90}")
    if args.mode in ("all", "market"):
        mode_market(ak)
    if args.mode in ("all", "sector"):
        mode_sector(ak, args.sector_type, args.top)
    if args.mode in ("all", "stock"):
        mode_stock(ak, args.top, "今日")
    if args.mode in ("all", "block"):
        mode_block(ak, args.top)


if __name__ == "__main__":
    main()
