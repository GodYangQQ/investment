#!/usr/bin/env python3
"""
牛散 / 十大流通股东 追踪
========================
说明：牛散（知名个人投资者）会出现在季报【十大流通股东】里，可追踪其持仓变化，
     但数据来自季报，**延迟 1-3 个月**；没有官方"散户收益排行榜"。

用法:
    python apps/cli/niusan_track.py --stock 600812
    python apps/cli/niusan_track.py --stock 000001 --top 10
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

# 常见"牛散"名单（启发式，非官方）
NIUSAN = {
    "林园", "葛卫东", "赵建平", "赵强", "徐开东", "李志强", "陈庆桃", "吴鸣霄",
    "王孝安", "孙惠刚", "章建平", "刘少鸾", "何雪萍", "高雅萍", "张寿清", "王富济",
    "冯柳", "段永平", "应莹", "章建平", "顾青", "周信钢", "孙伟", "李松强",
    "钟格", "钟坚", "黄木顺", "林广茂", "许磊", "王卫东",
}


def recent_quarters(n: int = 6) -> list[str]:
    """最近 n 个季报期（YYYYMMDD）。"""
    qs, y, today = [], date.today().year, date.today()
    ends = [(y, "1231"), (y, "0930"), (y, "0630"), (y, "0331"),
            (y - 1, "1231"), (y - 1, "0930"), (y - 1, "0630"), (y - 1, "0331")]
    for yy, mmdd in ends:
        d = f"{yy}{mmdd}"
        if date(int(d[:4]), int(d[4:6]), int(d[6:])) <= today:
            qs.append(d)
        if len(qs) >= n:
            break
    return qs


def market_prefix(code: str) -> str:
    return "sh" if str(code).startswith(("6", "9")) else ("bj" if str(code).startswith(("4", "8")) else "sz")


def fetch_top10(code: str, quarters: list[str], retries: int = 3):
    import akshare as ak
    sym = market_prefix(code) + str(code)
    for q in quarters:
        for _ in range(retries):
            try:
                df = ak.stock_gdfx_free_top_10_em(symbol=sym, date=q)
                if df is not None and not df.empty:
                    return q, df
            except Exception:
                time.sleep(1)
    return None, None


def main():
    ap = argparse.ArgumentParser(description="牛散/十大流通股东追踪")
    ap.add_argument("--stock", required=True, help="股票代码，如 600812")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    quarters = recent_quarters(6)
    print(f"查询 {args.stock} 十大流通股东（尝试期数: {', '.join(quarters)}）...")
    q, df = fetch_top10(args.stock, quarters)
    if df is None:
        print("获取失败：数据源(emweb.eastmoney)当前不可达或无数据。请稍后重试。")
        return

    print(f"\n{'='*88}\n  {args.stock} 十大流通股东  @ {q[:4]}-{q[4:6]}-{q[6:]}\n{'='*88}")
    print(f"  {'名次':<5}{'股东名称':<26}{'性质':<8}{'持股(万股)':>12}{'占流通%':>9}{'增减':>12}")
    print("  " + "-" * 84)
    for _, r in df.head(args.top).iterrows():
        name = str(r.get("股东名称", ""))
        nature = str(r.get("股东性质", ""))
        shares = r.get("持股数", 0)
        try:
            shares_w = float(shares) / 1e4
        except (TypeError, ValueError):
            shares_w = 0
        tag = ""
        if any(n and n in name for n in NIUSAN):
            tag = " ⭐牛散"
        elif nature in ("个人", "自然人") or ("个人" in nature):
            tag = " (自然人)"
        print(f"  {str(r.get('名次','')):<5}{name[:24]:<26}{nature[:6]:<8}{shares_w:>12.1f}"
              f"{float(r.get('占总流通股本持股比例',0) or 0):>9.2f}{str(r.get('增减','')):>12}{tag}")
    print("\n  说明：⭐=在收录的牛散名单中；季报数据，延迟 1-3 个月；仅供研究。")
    print("  ⚠️ 无官方'散户收益排行榜'；个人账户收益不公开。")


if __name__ == "__main__":
    main()
