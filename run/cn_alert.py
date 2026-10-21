#!/usr/bin/env python3
"""
A股炒作预警（提醒）
==================
只输出"值得盯"的标的：量比/涨幅/连板 达阈值的活跃票，按炒作分排序。

用法:
    python run/cn_alert.py                    # 默认阈值
    python run/cn_alert.py --min-vr 2.0 --min-pct 3
    python run/cn_alert.py --category plague  # 只看鼠疫链
    python run/cn_alert.py --timespan 3d
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

from cn_speculation import run_pipeline  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")


def _tag(pct, vr, consec):
    if consec >= 1 or pct >= 9.5:
        return "🔥 强势/连板"
    if vr and vr >= 2.5:
        return "⚡ 放量启动"
    if pct >= 5:
        return "📈 异动"
    return "✅ 观察"


def run(args):
    res = run_pipeline(per_query=args.per_query, top_modules=args.top_modules,
                       top_stocks=5, only_category=args.category,
                       use_global=not args.no_global, timespan=args.timespan)
    modules = res["modules"]

    alerts = {}
    for m in modules:
        for s in m["top_stocks"]:
            code = s["code"]
            vr = s.get("vol_ratio") or 0
            pct = s.get("pct") or 0
            consec = s.get("consec_limit") or 0
            hit = (vr >= args.min_vr) or (pct >= args.min_pct) or (consec >= 1)
            if not hit:
                continue
            if code not in alerts or s["score"] > alerts[code]["炒作分"]:
                alerts[code] = {
                    "模块": m["name"], "事件": m["category_cn"], "代码": code,
                    "名称": s.get("name") or "", "涨跌%": pct,
                    "量比": round(vr, 2), "连板": consec, "炒作分": s["score"],
                    "信号": _tag(pct, vr, consec), "逻辑": m.get("logic", ""),
                }

    rows = sorted(alerts.values(), key=lambda x: x["炒作分"], reverse=True)

    print("\n" + "=" * 90)
    print(f"  🔔 A股炒作预警  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}"
          f"  |  阈值 量比≥{args.min_vr} 或 涨幅≥{args.min_pct}% 或 连板")
    print("=" * 90)
    if not rows:
        print("  当前无达到阈值的活跃标的（市场偏冷）。")
        return rows

    print(f"\n  {'信号':<10}{'代码':<8}{'名称':<10}{'涨跌%':>7}{'量比':>6}{'连板':>5}"
          f"{'炒作分':>7}  模块/逻辑")
    print("  " + "-" * 86)
    for r in rows:
        print(f"  {r['信号']:<10}{r['代码']:<8}{r['名称']:<10}{r['涨跌%']:>6.1f}%"
              f"{r['量比']:>6}{r['连板']:>5}{r['炒作分']:>7}  {r['模块']}（{r['事件']}）")

    out = args.output or os.path.join(OUTPUT_DIR, f"cn_alert_{datetime.now().strftime('%Y%m%d')}.csv")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"\n  已保存: {out}")
    print("  提示：题材炒作非投资建议；🔥别追高位板，⚡看启动，跌破5日线止损。")
    return rows


def main():
    p = argparse.ArgumentParser(description="A股炒作预警（活跃标的提醒）")
    p.add_argument("--min-vr", type=float, default=1.8, help="量比阈值")
    p.add_argument("--min-pct", type=float, default=3.0, help="涨幅阈值%")
    p.add_argument("--top-modules", type=int, default=12)
    p.add_argument("--category", default=None)
    p.add_argument("--per-query", type=int, default=5)
    p.add_argument("--timespan", default="3d")
    p.add_argument("--no-global", action="store_true")
    p.add_argument("--output", default=None)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
