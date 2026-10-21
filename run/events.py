#!/usr/bin/env python3
"""
世界大事件 → 逻辑链 → 候选标的 （报告入口）
==========================================
扫描当前全球重大事件（灾害/疫情/地缘/政策/AI），按知识库逻辑链映射到美股标的。

用法:
    python run/events.py                      # 全量扫描 + 候选 Top 20
    python run/events.py --top 30
    python run/events.py --category war_geopolitics
    python run/events.py --quotes             # 给候选补实时行情（较慢）
    python run/events.py --output output/events.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

from world_events import (  # noqa: E402
    load_chains, scan_events, map_to_targets, summarize_categories,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")


def _fmt_when(dt) -> str:
    if not dt:
        return "?"
    try:
        return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def run(top: int = 20, per_query: int = 8, include_zh: bool = True,
        min_mag: float = 5.0, category: str = None,
        enrich_quotes: bool = False, output: str = None):
    chains = load_chains()
    log.info("扫描全球事件中（%d 类 / %s）...", len(chains), "含中文" if include_zh else "仅英文")
    events = scan_events(chains=chains, per_query=per_query,
                         include_zh=include_zh, include_quakes=True)
    events = [e for e in events if (e.get("magnitude") is None or e["magnitude"] >= min_mag)]
    if category:
        events = [e for e in events if e["category"] == category]

    print("\n" + "=" * 84)
    print(f"  全球大事件扫描  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  命中 {len(events)} 条")
    print("=" * 84)

    if not events:
        print("  未扫描到相关事件。可放宽 --min-mag 或检查网络。")
        return []

    # 1. 事件分类概览
    print("\n【事件热度】")
    for c in summarize_categories(events, chains):
        latest = _fmt_when(c.get("latest"))
        print(f"  {c['name_cn']:<18} x{c['count']:<3} 最新 {latest}")
        for s in c["samples"][:1]:
            print(f"       · {s[:88]}")

    # 2. 候选标的
    targets = map_to_targets(events, chains)[:top]
    quote_map = {}
    if enrich_quotes and targets:
        try:
            from us_data import fetch_batch_quotes
            quote_map = fetch_batch_quotes([t["ticker"] for t in targets])
        except Exception as e:
            log.warning("行情补充失败: %s", e)

    print("\n【候选标的（逻辑链映射，非买入建议）】")
    header = f"  {'代码':<6}{'方向':<4}{'分':>5}  逻辑 / 关联事件"
    print(header)
    print("  " + "-" * 80)
    for t in targets:
        d = "多" if t["direction"] == "long" else "空"
        px = ""
        if quote_map.get(t["ticker"]):
            q = quote_map[t["ticker"]]
            px = f"  ${q.get('price', 0):.2f}({q.get('pct_change', 0):+.1f}%)"
        why = "；".join(t["reasons"][:2])
        print(f"  {t['ticker']:<6}{d:<4}{t['score']:>5.1f}  {why}{px}")

    print("\n  说明：方向仅为逻辑推演（多=受益/空=受损），须结合行情与基本面二次验证。")

    # 3. CSV
    if output is None:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        output = os.path.join(OUTPUT_DIR, f"events_{datetime.now().strftime('%Y%m%d')}.csv")
    try:
        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
        with open(output, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["代码", "方向", "分数", "关联类别", "逻辑", "现价", "涨跌%"])
            for t in targets:
                q = quote_map.get(t["ticker"], {})
                w.writerow([
                    t["ticker"], "多" if t["direction"] == "long" else "空",
                    t["score"], "/".join(t["categories"]),
                    "；".join(t["reasons"]),
                    q.get("price", ""), q.get("pct_change", ""),
                ])
        print(f"\n  候选已保存: {output}")
    except Exception as e:
        log.warning("保存CSV失败: %s", e)

    return targets


def main():
    parser = argparse.ArgumentParser(description="世界大事件 → 逻辑链 → 候选标的")
    parser.add_argument("--top", type=int, default=20, help="候选标的数量")
    parser.add_argument("--per-query", type=int, default=8, help="每个关键词抓取新闻数")
    parser.add_argument("--no-zh", action="store_true", help="不抓中文新闻")
    parser.add_argument("--min-mag", type=float, default=5.0, help="地震最低震级")
    parser.add_argument("--category", default=None,
                        help="只看某类事件（earthquake/hurricane_typhoon/war_geopolitics 等）")
    parser.add_argument("--quotes", action="store_true", help="给候选补实时行情")
    parser.add_argument("--output", default=None, help="输出CSV路径")
    args = parser.parse_args()

    run(top=args.top, per_query=args.per_query, include_zh=not args.no_zh,
        min_mag=args.min_mag, category=args.category,
        enrich_quotes=args.quotes, output=args.output)


if __name__ == "__main__":
    main()
