#!/usr/bin/env python3
"""
SOXX 前20 量化打分排名
=====================
遍历 iShares SOXX 前 20 持仓，用 quant_score 多因子模型逐只打分，按总分倒排。

用法:
    python apps/cli/rank.py
    python apps/cli/rank.py --top 10
    python apps/cli/rank.py --no-fundamental-filter
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import pandas as pd

from core.strategies.stock_strategy import fetch_daily_kline, compute_indicators, get_latest_signals
from core.scoring.quant_score import calc_total_score, calc_momentum_factors
from core.filters.market_filter import get_market, ALL_MARKETS
from core.filters.fundamental_filter import apply_fundamental_filter
from core.datasets.universe import get_soxx_universe
from core.datasets.us_data import fetch_batch_quotes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
DATA_DIR = os.path.join(ROOT_DIR, "data")
DEFAULT_TOP_N = 20
DEFAULT_WORKERS = 8
DEFAULT_OUTPUT_CSV = os.path.join(OUTPUT_DIR, "quant_top20.csv")
INTERMEDIATE_CSV = os.path.join(DATA_DIR, "soxx_score_intermediate.csv")


def score_one_stock(code: str, name: str, quote: dict | None) -> dict | None:
    try:
        df = fetch_daily_kline(code, days=300)
        if df is None or len(df) < 60:
            return None
        df = compute_indicators(df)
        signals = get_latest_signals(df, quote or {})
        score_result = calc_total_score(signals)
        total = score_result["total"]
        momentum = calc_momentum_factors(df, code)
        factors = score_result["factors"]
        record = {
            "代码": code,
            "名称": name,
            "细分": get_market(code) or "未知",
            "现价": signals.get("current_price") or (quote or {}).get("price", 0),
            "总得分": total,
            "可买度": score_result["buyability"],
            "趋势+位置": factors.get("趋势+位置", {}).get("score"),
            "量价+PriceAction": factors.get("量价+PriceAction", {}).get("score"),
            "波动率": factors.get("波动率", {}).get("score"),
            "斐波那契/结构": factors.get("斐波那契/结构", {}).get("score"),
            "PE_TTM": (quote or {}).get("pe_ttm", 0),
            "PB": (quote or {}).get("pb", 0),
            "涨跌幅": (quote or {}).get("pct_change", 0),
            "20日动量": momentum.get("twenty_day_pct"),
            "3月动量": momentum.get("three_month_pct"),
            "1年动量": momentum.get("one_year_pct"),
            "动量一致性": momentum.get("momentum_consistency"),
            "RSI": signals.get("rsi14"),
        }
        return record
    except Exception as e:
        log.warning("打分失败 %s %s: %s", code, name, e)
        return None


def rank_all_stocks(
    top_n: int = DEFAULT_TOP_N,
    workers: int = DEFAULT_WORKERS,
    resume: bool = False,
    output_csv: str = DEFAULT_OUTPUT_CSV,
    intermediate_csv: str = INTERMEDIATE_CSV,
    exclude_markets: list[str] | None = None,
    keep_markets: list[str] | None = None,
    use_fundamental_filter: bool = True,
    deep_check_top: int = 20,
) -> pd.DataFrame:
    start_time = time.time()
    universe = get_soxx_universe(top_n=20, live=True)
    if keep_markets:
        universe = [s for s in universe if get_market(s["code"]) in keep_markets]
    if exclude_markets:
        universe = [s for s in universe if get_market(s["code"]) not in exclude_markets]
    log.info("SOXX 池: %d 只", len(universe))

    quotes = fetch_batch_quotes([s["code"] for s in universe])
    names = {s["code"]: s["name"] for s in universe}

    records = []
    lock = Lock()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for s in universe:
            code = s["code"]
            q = quotes.get(code)
            if q and not q.get("name"):
                q["name"] = names.get(code, code)
            futs[ex.submit(score_one_stock, code, names.get(code, code), q)] = code
        for fut in as_completed(futs):
            rec = fut.result()
            if rec:
                with lock:
                    records.append(rec)

    if not records:
        log.error("无有效打分结果")
        return pd.DataFrame()

    all_results = pd.DataFrame(records).sort_values("总得分", ascending=False).reset_index(drop=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    all_results.to_csv(intermediate_csv, index=False, encoding="utf-8-sig")

    if use_fundamental_filter:
        log.info("基本面过滤 (deep_check_top=%d)...", deep_check_top)
        all_results = apply_fundamental_filter(all_results, deep_check_top=deep_check_top)
        if "基本面通过" in all_results.columns:
            before = len(all_results)
            all_results = all_results[all_results["基本面通过"] == True].copy()
            log.info("基本面过滤后: %d → %d", before, len(all_results))

    top_df = all_results.head(top_n).copy()
    top_df.insert(0, "排名", range(1, len(top_df) + 1))
    os.makedirs(os.path.dirname(output_csv) or ".", exist_ok=True)
    top_df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    log.info("Top %d 已保存: %s", top_n, output_csv)

    print("\n" + "=" * 80)
    print(f"SOXX 量化打分 Top {top_n}")
    print("=" * 80)
    display_cols = ["排名", "代码", "名称", "细分", "现价", "总得分", "可买度", "PE_TTM", "PB"]
    if use_fundamental_filter:
        display_cols += [c for c in ["ROE", "毛利率", "仓位乘数", "基本面原因"] if c in top_df.columns]
    avail = [c for c in display_cols if c in top_df.columns]
    print(top_df[avail].to_string(index=False))
    print(f"\n耗时 {time.time() - start_time:.1f}s  有效 {len(all_results)} 只")
    return top_df


def main():
    parser = argparse.ArgumentParser(description="SOXX 前20 量化打分排名")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--resume", action="store_true", default=False)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--intermediate", type=str, default=INTERMEDIATE_CSV)
    parser.add_argument("--exclude-markets", type=str, default="",
                        help=f"排除细分，可选: {','.join(ALL_MARKETS)}")
    parser.add_argument("--keep-markets", type=str, default=None,
                        help=f"仅保留细分，可选: {','.join(ALL_MARKETS)}")
    parser.add_argument("--no-fundamental-filter", action="store_true")
    parser.add_argument("--deep-check-top", type=int, default=20)
    args = parser.parse_args()

    exclude = [m.strip() for m in args.exclude_markets.split(",") if m.strip()] or None
    keep = [m.strip() for m in args.keep_markets.split(",")] if args.keep_markets else None
    rank_all_stocks(
        top_n=args.top, workers=args.workers, resume=args.resume,
        output_csv=args.output, intermediate_csv=args.intermediate,
        exclude_markets=exclude, keep_markets=keep,
        use_fundamental_filter=not args.no_fundamental_filter,
        deep_check_top=args.deep_check_top,
    )


if __name__ == "__main__":
    main()
