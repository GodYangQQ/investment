#!/usr/bin/env python3
"""
A股实时资金流向监控（盘中）
==========================
东方财富盘中资金流接口，分钟级刷新（非Level2，仍是"大单推算"估算）。

用法:
    python run/moneyflow_live.py                 # 每 15 秒刷新
    python run/moneyflow_live.py --interval 5
    python run/moneyflow_live.py --once          # 只打一次（可接 cron/定时）
    python run/moneyflow_live.py --top 12
    python run/moneyflow_live.py --save          # 每次刷新追加存 txt

退出：Ctrl+C
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime

RUN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RUN_DIR)
sys.path.insert(0, os.path.join(RUN_DIR, "..", "core"))

from cn_data import _em_get  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT_DIR = os.path.dirname(RUN_DIR)
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")

_FS_ALL = ("m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,"
           "m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2")


def _yi(v):
    try:
        return f"{float(v)/1e8:+.2f}"
    except (TypeError, ValueError):
        return "?"


def _items(js):
    if js and js.get("data") and js["data"].get("diff") is not None:
        diff = js["data"]["diff"]
        return list(diff.values()) if isinstance(diff, dict) else diff
    return []


def top_stocks(desc: bool, n: int) -> list[dict]:
    po = 1 if desc else 0
    url = (f"https://push2.eastmoney.com/api/qt/clist/get?fid=f62&po={po}&pz={n}&pn=1"
           f"&np=1&fltt=2&invt=2&fs={_FS_ALL}&fields=f12,f14,f3,f62,f184")
    out = []
    for it in _items(_em_get(url))[:n]:
        out.append({"code": it.get("f12"), "name": it.get("f14"),
                    "pct": it.get("f3"), "net": it.get("f62"), "pct_net": it.get("f184")})
    return out


def top_sectors(desc: bool, n: int, kind: str = "行业") -> list[dict]:
    po = 1 if desc else 0
    t = "2" if kind == "行业" else "3"
    url = (f"https://push2.eastmoney.com/api/qt/clist/get?fid=f62&po={po}&pz={n}&pn=1"
           f"&np=1&fltt=2&invt=2&fs=m:90+t:{t}&fields=f12,f14,f3,f62")
    out = []
    for it in _items(_em_get(url))[:n]:
        out.append({"code": it.get("f12"), "name": it.get("f14"),
                    "pct": it.get("f3"), "net": it.get("f62")})
    return out


def render(top: int, kind: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("=" * 78)
    lines.append(f"  A股实时资金流向  |  {now}  （东财，估算）")
    lines.append("=" * 78)
    inflow = top_stocks(True, top)
    outflow = top_stocks(False, top)
    lines.append("\n【个股主力净流入 亿】")
    for r in inflow:
        lines.append(f"  {str(r['name'])[:8]:<10}{r['code']:<8}{float(r['pct']):>7.1f}%  {_yi(r['net']):>9}亿")
    lines.append("\n【个股主力净流出 亿】")
    for r in outflow:
        lines.append(f"  {str(r['name'])[:8]:<10}{r['code']:<8}{float(r['pct']):>7.1f}%  {_yi(r['net']):>9}亿")
    si = top_sectors(True, 6, kind)
    so = top_sectors(False, 6, kind)
    lines.append(f"\n【{kind}板块 净流入 亿】")
    lines.append("  " + " | ".join(f"{str(r['name'])[:8]} {_yi(r['net'])}" for r in si))
    lines.append(f"【{kind}板块 净流出 亿】")
    lines.append("  " + " | ".join(f"{str(r['name'])[:8]} {_yi(r['net'])}" for r in so))
    lines.append("\n  ⚠️ 盘中'大单推算'估算，非真实账户；Ctrl+C 退出。")
    print("\n".join(lines))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="A股实时资金流向")
    ap.add_argument("--interval", type=int, default=15, help="刷新秒数")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--kind", choices=["行业", "概念"], default="行业")
    ap.add_argument("--once", action="store_true", help="只输出一次")
    ap.add_argument("--save", action="store_true", help="追加保存到 output/moneyflow_live_YYYYMMDD.txt")
    args = ap.parse_args()

    if args.once:
        txt = render(args.top, args.kind)
        if args.save:
            _append(txt)
        return

    try:
        while True:
            os.system("clear")
            txt = render(args.top, args.kind)
            if args.save:
                _append(txt)
            time.sleep(max(3, args.interval))
    except KeyboardInterrupt:
        print("\n已退出实时监控。")


def _append(txt: str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"moneyflow_live_{datetime.now().strftime('%Y%m%d')}.txt")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(txt + "\n\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
