#!/usr/bin/env python3
"""
A股短线炒作 pipeline（CLI）
==========================
世界/国内大事件（全球权威源）→ 炒作逻辑链 → 题材模块 → 模块前3龙头

用法:
    python apps/cli/cn_pick.py                          # 全量：模块 + 每模块前3
    python apps/cli/cn_pick.py --top-modules 12 --top-stocks 3
    python apps/cli/cn_pick.py --category plague        # 只看鼠疫/烈性传染病
    python apps/cli/cn_pick.py --module 抗生素           # 直接查某模块
    python apps/cli/cn_pick.py --no-global              # 仅用 Google News 兜底
    python apps/cli/cn_pick.py --timespan 30d --output output/cn_pick.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from core.strategies.cn_speculation import run_pipeline, load_cn_chains, rank_module_stocks  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")


def _fmt_pub(dt) -> str:
    try:
        return dt.strftime("%m-%d")
    except Exception:
        return "??"


def run(args):
    # 直接查单模块
    if args.module:
        chains = load_cn_chains()
        target = None
        for cat, spec in chains.items():
            for mod in spec["modules"]:
                if args.module in mod["name"]:
                    target = (cat, spec, mod)
                    break
            if target:
                break
        if not target:
            print(f"未找到模块 '{args.module}'")
            return []
        cat, spec, mod = target
        stocks = rank_module_stocks(mod, top=args.top_stocks, expand=args.expand)
        print(f"\n=== 模块: {mod['name']}  [{spec['name_cn']}] ===")
        print(f"逻辑: {mod.get('logic','')}")
        for s in stocks:
            print(f"  {s['code']} {s['name']}  {s['pct']:+.1f}%  炒作分{s['score']}  连板{s['consec_limit']}  量比{round(s['vol_ratio'] or 0,2)}")
        return stocks

    # 完整 pipeline
    res = run_pipeline(per_query=args.per_query, top_modules=args.top_modules,
                       top_stocks=args.top_stocks, only_category=args.category,
                       use_global=not args.no_global, timespan=args.timespan)
    events = res["events"]
    modules = res["modules"]

    print("\n" + "=" * 88)
    print(f"  A股短线炒作 pipeline  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}"
          f"  |  全球权威事件命中 {len(events)} 条")
    print("=" * 88)

    # 事件概览（权威优先）
    print("\n【全球大事件（权威源优先）】")
    for ev in events[:12]:
        tag = {"official": "官方", "tier1": "一线", "other": "媒体"}.get(ev.get("authority"), "媒体")
        print(f"  [{tag}] {_fmt_pub(ev.get('published'))} {ev['category_cn']:<12} {ev['title'][:60]}")

    # 模块 + 前3
    print("\n【炒作模块 → 前3龙头】")
    out_rows = []
    for i, m in enumerate(modules, 1):
        print(f"\n{i:>2}. {m['name']}  [{m['category_cn']}]  模块分 {m['module_score']}"
              f"  (事件×{m['event_hits']} 权威+{m.get('authority_bonus',0)})")
        print(f"    逻辑: {m['logic']}")
        for j, s in enumerate(m["top_stocks"], 1):
            print(f"    {j}. {s['code']} {s['name']:<8} {s['pct']:+6.1f}%  "
                  f"炒作{s['score']:>6}  连板{s['consec_limit']}  量比{round(s['vol_ratio'] or 0,2)}")
            out_rows.append({
                "模块": m["name"], "事件类别": m["category_cn"], "模块分": m["module_score"],
                "排名": j, "代码": s["code"], "名称": s["name"],
                "涨跌%": s["pct"], "炒作分": s["score"], "连板": s["consec_limit"],
                "量比": round(s["vol_ratio"] or 0, 2),
            })

    # CSV
    out = args.output or os.path.join(OUTPUT_DIR, f"cn_pick_{datetime.now().strftime('%Y%m%d')}.csv")
    if out_rows:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(out_rows)
        print(f"\n  已保存: {out}")
    print("\n  提示：方向为题材炒作逻辑，非投资建议；短线只看情绪/量价，注意追高风险。")
    return modules


def main():
    p = argparse.ArgumentParser(description="A股短线炒作：大事件→模块→前3龙头")
    p.add_argument("--top-modules", type=int, default=10)
    p.add_argument("--top-stocks", type=int, default=3)
    p.add_argument("--category", default=None, help="只保留某事件类别（如 plague/pandemic_outbreak/earthquake）")
    p.add_argument("--module", default=None, help="直接查单个模块（如 抗生素/生物疫苗）")
    p.add_argument("--per-query", type=int, default=5)
    p.add_argument("--timespan", default="14d", help="GDELT 时间窗，如 7d/14d/30d")
    p.add_argument("--no-global", action="store_true", help="不用全球权威层，仅 Google 兜底")
    p.add_argument("--expand", action="store_true", help="用东财概念成分扩充候选池")
    p.add_argument("--output", default=None)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
