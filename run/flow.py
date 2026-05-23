#!/usr/bin/env python3
"""
每日资金流向统计脚本（可定时运行）
===============================
每天收盘后自动统计全市场资金流向，输出：
  1. 全市场资金净流入 Top N 排名
  2. 全市场资金净流出 Top N 排名（警惕风险）
  3. 连续N日净流入/流出个股
  4. 板块资金流向汇总

用法：
    python daily_money_flow.py                     # 默认Top50，统计当日
    python daily_money_flow.py --top 100 --days 3   # 近3日累计，Top100
    python daily_money_flow.py --market 主板         # 只看主板

输出文件（output/目录）：
    money_flow_top{N}_{date}.csv        - 净流入排名
    money_flow_bottom{N}_{date}.csv     - 净流出排名（资金逃离预警）
    money_flow_consecutive_{date}.csv   - 连续净流入/流出个股
    money_flow_market_summary_{date}.csv - 板块汇总
"""

import argparse
import logging
import os
import sys
from datetime import datetime

import pandas as pd

# 确保可以 import core/ 下的模块
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

from money_flow import (
    rank_all_by_money_flow,
    summarize_by_market,
    _fmt_amount,
    _get_all_a_stock_codes,
    get_stock_fund_flow_summary,
    DEFAULT_TOP_N,
    DEFAULT_DAYS,
    DEFAULT_WORKERS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")


def run_daily_report(
    top_n: int = DEFAULT_TOP_N,
    days: int = DEFAULT_DAYS,
    workers: int = DEFAULT_WORKERS,
    market: str = None,
):
    """运行每日资金流向完整报告"""
    today = datetime.now().strftime("%Y%m%d")
    log.info("=" * 60)
    log.info("每日资金流向统计报告 - %s", today)
    log.info("参数: top=%d, days=%d, market=%s", top_n, days, market or "全部")
    log.info("=" * 60)

    # ------------------------------------------------------------------
    # 1. 全市场排名
    # ------------------------------------------------------------------
    log.info("[1/4] 全市场资金流向排名...")
    all_results = rank_all_by_money_flow(
        top_n=max(top_n * 3, 500),  # 多取一些用于后续分析
        days=days,
        workers=workers,
        market_filter=market,
        output_csv=None,
    )

    if not all_results:
        log.error("没有获取到有效数据，退出")
        return

    # 所有结果（不截断）
    sorted_by_net = sorted(all_results, key=lambda x: x.get("main_net_cum", 0), reverse=True)

    # ------------------------------------------------------------------
    # 2. 保存 Top N 净流入
    # ------------------------------------------------------------------
    log.info("[2/4] 保存净流入排名...")
    top_inflow = sorted_by_net[:top_n]
    for i, r in enumerate(top_inflow):
        r["rank"] = i + 1
    _save_flow_csv(top_inflow, os.path.join(OUTPUT_DIR, f"money_flow_top{top_n}_{today}.csv"), days)
    log.info("  净流入 Top%d 已保存", len(top_inflow))

    # ------------------------------------------------------------------
    # 3. 保存 Top N 净流出（资金逃离预警）
    # ------------------------------------------------------------------
    log.info("[3/4] 保存净流出排名（资金逃离预警）...")
    sorted_by_outflow = sorted(all_results, key=lambda x: x.get("main_net_cum", 0))
    top_outflow = sorted_by_outflow[:top_n]
    for i, r in enumerate(top_outflow):
        r["rank"] = i + 1
    _save_flow_csv(top_outflow, os.path.join(OUTPUT_DIR, f"money_flow_bottom{top_n}_{today}.csv"), days)
    log.info("  净流出 Top%d 已保存", len(top_outflow))

    # ------------------------------------------------------------------
    # 4. 连续净流入/流出个股
    # ------------------------------------------------------------------
    log.info("[4/4] 筛选连续资金异动个股...")
    consecutive_records = []

    # 连续净流入 >= 3天
    inflow_stocks = [r for r in all_results if r.get("consecutive_inflow", 0) >= 3]
    inflow_stocks.sort(key=lambda x: x.get("consecutive_inflow", 0), reverse=True)
    for r in inflow_stocks[:top_n]:
        r["type"] = "连续净流入"
        consecutive_records.append(r)

    # 连续净流出 >= 3天
    outflow_stocks = [r for r in all_results if r.get("consecutive_outflow", 0) >= 3]
    outflow_stocks.sort(key=lambda x: x.get("consecutive_outflow", 0), reverse=True)
    for r in outflow_stocks[:top_n]:
        r["type"] = "连续净流出"
        consecutive_records.append(r)

    if consecutive_records:
        _save_consecutive_csv(consecutive_records,
                              os.path.join(OUTPUT_DIR, f"money_flow_consecutive_{today}.csv"), days)
        log.info("  连续异动个股: 流入%d只, 流出%d只", len(inflow_stocks[:top_n]), len(outflow_stocks[:top_n]))

    # ------------------------------------------------------------------
    # 5. 板块汇总
    # ------------------------------------------------------------------
    market_summary = summarize_by_market(all_results)
    _save_market_summary_csv(market_summary,
                             os.path.join(OUTPUT_DIR, f"money_flow_market_summary_{today}.csv"))
    log.info("  板块汇总已保存")

    # ------------------------------------------------------------------
    # 6. 打印摘要
    # ------------------------------------------------------------------
    _print_report_summary(top_inflow, top_outflow, market_summary, today, days)

    log.info("=" * 60)
    log.info("每日资金流向统计完成!")
    log.info("=" * 60)


def _save_flow_csv(results: list[dict], filepath: str, days: int):
    """保存资金流向CSV"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    rows = []
    for r in results:
        rows.append({
            "排名": r.get("rank", ""),
            "代码": r["code"],
            "日期": r.get("latest_date", ""),
            "收盘价": r.get("close", ""),
            "涨跌幅%": r.get("pct_change", ""),
            f"主力净流入({days}日)": _fmt_amount(r.get("main_net_cum", 0)),
            "主力净占比%": r.get("main_net_pct_avg", 0),
            f"主力流入({days}日)": _fmt_amount(r.get("main_inflow", 0)),
            f"主力流出({days}日)": _fmt_amount(r.get("main_outflow", 0)),
            "超大单净流入": _fmt_amount(r.get("super_large_net_cum", 0)),
            "超大单净占比%": r.get("super_large_net_pct_avg", 0),
            "大单净流入": _fmt_amount(r.get("large_net_cum", 0)),
            "大单净占比%": r.get("large_net_pct_avg", 0),
            "中单净流入": _fmt_amount(r.get("medium_net_cum", 0)),
            "中单净占比%": r.get("medium_net_pct_avg", 0),
            "小单净流入": _fmt_amount(r.get("small_net_cum", 0)),
            "小单净占比%": r.get("small_net_pct_avg", 0),
            "超大单流入": _fmt_amount(r.get("super_large_inflow", 0)),
            "超大单流出": _fmt_amount(r.get("super_large_outflow", 0)),
            "大单流入": _fmt_amount(r.get("large_inflow", 0)),
            "大单流出": _fmt_amount(r.get("large_outflow", 0)),
            "连续净流入天数": r.get("consecutive_inflow", 0),
            "连续净流出天数": r.get("consecutive_outflow", 0),
            "资金评分": r.get("flow_score", 0),
        })
    df = pd.DataFrame(rows)
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    log.info("  已保存: %s (%d条)", filepath, len(rows))


def _save_consecutive_csv(records: list[dict], filepath: str, days: int):
    """保存连续异动CSV"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    rows = []
    for r in records:
        rows.append({
            "类型": r.get("type", ""),
            "代码": r["code"],
            "日期": r.get("latest_date", ""),
            "收盘价": r.get("close", ""),
            "涨跌幅%": r.get("pct_change", ""),
            f"主力净流入({days}日)": _fmt_amount(r.get("main_net_cum", 0)),
            "主力净占比%": r.get("main_net_pct_avg", 0),
            "连续流入天数": r.get("consecutive_inflow", 0),
            "连续流出天数": r.get("consecutive_outflow", 0),
            "超大单净占比%": r.get("super_large_net_pct_avg", 0),
            "资金评分": r.get("flow_score", 0),
        })
    df = pd.DataFrame(rows)
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    log.info("  已保存: %s (%d条)", filepath, len(rows))


def _save_market_summary_csv(summary: dict, filepath: str):
    """保存板块汇总CSV"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    rows = []
    for market, s in sorted(summary.items()):
        rows.append({
            "板块": market,
            "个股数": s["count"],
            "主力净流入(net)": _fmt_amount(s["main_net_cum"]),
            "主力流入总额": _fmt_amount(s["main_inflow"]),
            "主力流出总额": _fmt_amount(s["main_outflow"]),
            "净流入个股数": s["inflow_stocks"],
            "净流出个股数": s["outflow_stocks"],
            "净流入占比": f"{s['inflow_stocks']/s['count']*100:.1f}%" if s["count"] > 0 else "0%",
        })
    df = pd.DataFrame(rows)
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    log.info("  已保存: %s", filepath)


def _print_report_summary(top_inflow, top_outflow, market_summary, date_str, days):
    """打印报告摘要"""
    print("\n" + "=" * 80)
    print(f"  📊 每日资金流向统计报告 - {date_str}")
    print("=" * 80)

    # 净流入前5
    print(f"\n  🔥 主力净流入 Top5 (近{days}日):")
    print(f"  {'排名':<5} {'代码':<8} {'主力净流入':>14} {'净占比':>8} {'评分':>6}")
    print("  " + "-" * 50)
    for r in top_inflow[:5]:
        print(f"  {r['rank']:<5} {r['code']:<8} {_fmt_amount(r.get('main_net_cum', 0)):>14} "
              f"{r.get('main_net_pct_avg', 0):>7.2f}% {r.get('flow_score', 0):>5.0f}")

    # 净流出前5
    print(f"\n  ⚠️  主力净流出 Top5 (近{days}日) - 资金逃离预警:")
    print(f"  {'排名':<5} {'代码':<8} {'主力净流出':>14} {'净占比':>8} {'评分':>6}")
    print("  " + "-" * 50)
    for r in top_outflow[:5]:
        print(f"  {r['rank']:<5} {r['code']:<8} {_fmt_amount(r.get('main_net_cum', 0)):>14} "
              f"{r.get('main_net_pct_avg', 0):>7.2f}% {r.get('flow_score', 0):>5.0f}")

    # 板块汇总
    print(f"\n  📈 板块资金流向汇总:")
    print(f"  {'板块':<8} {'个股数':>6} {'主力净流入':>14} {'净流入占比':>10}")
    print("  " + "-" * 50)
    for market, s in sorted(market_summary.items()):
        pct = f"{s['inflow_stocks']/s['count']*100:.1f}%" if s["count"] > 0 else "0%"
        print(f"  {market:<8} {s['count']:>6} {_fmt_amount(s['main_net_cum']):>14} {pct:>10}")

    print("\n" + "=" * 80)


# ============================================================================
# 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="每日资金流向完整统计报告",
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                        help="排名Top N（默认50）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="统计最近N天（默认1）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="并发线程数（默认8）")
    parser.add_argument("--market", type=str, default=None,
                        help="板块过滤: 主板/创业板/科创板/北交所")

    args = parser.parse_args()

    run_daily_report(
        top_n=args.top,
        days=args.days,
        workers=args.workers,
        market=args.market,
    )


if __name__ == "__main__":
    main()
