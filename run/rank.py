#!/usr/bin/env python3
"""
A股全市场量化打分排名脚本
=======================
遍历全部 A 股（~5000只），用 quant_score 的多因子模型逐只打分，
按总分倒排，输出 Top N。

核心流程：
  1. 从新浪获取全部A股代码列表
  2. 多线程并发获取日K线 + 计算技术指标 + 打分
  3. 每只股票打分完实时追加到中间CSV（断点续跑）
  4. 最终排序输出 Top N

时间预估（5000只）：
  - 串行：约 60-150 分钟
  - 10线程并发：约 8-20 分钟（受限于 akshare 频率限制）

用法：
    python rank_all_stocks.py                    # 默认 Top100，10线程
    python rank_all_stocks.py --top 50           # Top50
    python rank_all_stocks.py --workers 20       # 20线程（激进）
    python rank_all_stocks.py --resume           # 断点续跑（跳过已完成）
    python rank_all_stocks.py --output top200.csv  # 自定义输出文件
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

# 确保可以 import core/ 下的模块
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

import numpy as np
import pandas as pd
import requests

from stock_strategy import (
    fetch_daily_kline,
    compute_indicators,
    get_latest_signals,
    _HAS_AKSHARE,
)

from quant_score import (
    calc_total_score,
    calc_momentum_factors,
    calc_rsi_slope_curvature,
)

from market_filter import (
    get_market,
    filter_by_market,
    filter_codes_by_market,
    ALL_MARKETS,
)

from fundamental_filter import (
    quick_pe_filter,
    apply_fundamental_filter,
    DEFAULT_FUNDAMENTAL_RULES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ============================================================================
# 配置
# ============================================================================

DEFAULT_TOP_N = 100
DEFAULT_WORKERS = 10
BATCH_SAVE_INTERVAL = 50  # 每50只刷新一次中间文件（用于断点续跑）
REQUEST_DELAY = 0.15       # 每个请求之间的最小间隔（秒），避免被封

# 路径：相对于项目根目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
DATA_DIR = os.path.join(ROOT_DIR, "data")
INTERMEDIATE_CSV = os.path.join(DATA_DIR, "all_stocks_score_intermediate.csv")
DEFAULT_OUTPUT_CSV = os.path.join(OUTPUT_DIR, "quant_top100.csv")
DEFAULT_EXCLUDE_MARKETS = "创业板,科创板"  # 默认排除创业板+科创板


# ============================================================================
# 1. 获取全部A股代码
# ============================================================================

def get_all_a_stock_codes() -> list[dict]:
    """从新浪获取全部A股代码列表。返回 [{"code":"600519","name":"贵州茅台"}, ...]"""
    log.info("正在从新浪获取全部A股代码列表...")

    # 方法1：新浪分页接口
    count_url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
        "/Market_Center.getHQNodeStockCount?node=hs_a"
    )
    try:
        count = int(requests.get(count_url, timeout=10).text.strip('"'))
    except Exception:
        log.warning("新浪获取总数失败，尝试用 akshare")
        return _get_codes_from_akshare()

    log.info("A股总数: %d", count)

    codes = []
    page_size = 80
    total_pages = count // page_size + 2

    for page in range(1, total_pages + 1):
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
            "/Market_Center.getHQNodeData"
        )
        params = {
            "page": page, "num": page_size, "sort": "symbol",
            "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "page",
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
            if not data:
                break
            for item in data:
                codes.append({"code": item["code"], "name": item.get("name", "")})
        except Exception as e:
            log.warning("第%d页获取失败: %s", page, e)
            break
        time.sleep(0.05)

    log.info("共获取 %d 只A股", len(codes))
    return codes


def _get_codes_from_akshare() -> list[dict]:
    """备用方案：用 akshare 获取股票列表"""
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        return [
            {"code": row["code"], "name": row["name"]}
            for _, row in df.iterrows()
        ]
    except Exception as e:
        log.error("akshare获取股票列表失败: %s", e)
        return []


# ============================================================================
# 2. 获取实时行情（批量，用于PE/PB等补充信息）
# ============================================================================

def fetch_batch_realtime_quotes(codes: list[str]) -> dict[str, dict]:
    """
    使用腾讯批量接口获取实时行情。
    返回 {code: {price, pe_ttm, pb, pct_change, name, ...}}
    """
    log.info("正在批量获取 %d 只股票的实时行情...", len(codes))
    quotes = {}

    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        # 腾讯格式: sh600519,sz000001
        symbols = []
        for c in batch:
            prefix = "sh" if c.startswith(("6", "9")) else "sz"
            symbols.append(f"{prefix}{c}")

        url = (
            "http://qt.gtimg.cn/q="
            + ",".join(symbols)
        )
        try:
            resp = requests.get(url, timeout=10)
            resp.encoding = "gbk"
            for line in resp.text.strip().split("\n"):
                if "~" not in line:
                    continue
                # 提取 v_xxx="..." 中的内容
                parts = line.split('"')
                if len(parts) < 2:
                    continue
                fields = parts[1].split("~")
                if len(fields) < 50:
                    continue

                code = fields[2]
                try:
                    price = float(fields[3]) if fields[3] else 0
                    pe = float(fields[39]) if fields[39] else 0
                    pb = float(fields[46]) if fields[46] else 0
                    pct = float(fields[32]) if fields[32] else 0
                    name = fields[1]
                except (ValueError, IndexError):
                    continue

                quotes[code] = {
                    "code": code,
                    "name": name,
                    "price": price,
                    "pe_ttm": pe,
                    "pb": pb,
                    "pct_change": pct,
                }
        except Exception as e:
            log.warning("批量行情第%d批失败: %s", i // batch_size, e)

        time.sleep(0.1)

    log.info("成功获取 %d 只股票的实时行情", len(quotes))
    return quotes


# ============================================================================
# 3. 单只股票打分（核心逻辑）
# ============================================================================

def score_one_stock(
    code: str,
    name: str,
    quote: dict | None,
) -> dict | None:
    """
    对单只股票进行完整打分。
    返回包含总分和各因子得分的 dict，失败返回 None。
    """
    try:
        # 3a. 获取日K线
        df = fetch_daily_kline(code, days=300)
        if df is None or len(df) < 60:
            return None

        # 3b. 计算技术指标
        df = compute_indicators(df)
        signals = get_latest_signals(df, quote or {})

        # 3c. 技术面多因子打分
        score_result = calc_total_score(signals)
        total = score_result["total"]

        # 3d. 动量因子
        momentum = calc_momentum_factors(df, code)

        # 3e. RSI斜率曲率
        closes = list(df["close"].values)
        rsi_curve = calc_rsi_slope_curvature(closes)

        # 3f. 汇总
        record = {
            "代码": code,
            "名称": name,
            "板块": get_market(code) or "未知",
            "现价": signals["current_price"],
            "总得分": total,
            "可买度": score_result["buyability"],
            "趋势因子": score_result["factors"]["趋势因子"]["score"],
            "位置因子": score_result["factors"]["位置因子"]["score"],
            "量价因子": score_result["factors"]["量价因子"]["score"],
            "RSI因子": score_result["factors"]["RSI因子"]["score"],
            "波动率因子": score_result["factors"]["波动率因子"]["score"],
            "附加项": score_result["factors"]["附加项"]["score"],
            "PE_TTM": quote.get("pe_ttm", 0) if quote else 0,
            "PB": quote.get("pb", 0) if quote else 0,
            "涨跌幅": quote.get("pct_change", 0) if quote else 0,
            "20日动量": momentum.get("twenty_day_pct"),
            "3月动量": momentum.get("three_month_pct"),
            "1年动量": momentum.get("one_year_pct"),
            "动量一致性": momentum.get("momentum_consistency"),
            "RSI当前": rsi_curve.get("rsi_current"),
            "RSI斜率5日": rsi_curve.get("rsi_slope_5d"),
            "RSI方向": rsi_curve.get("rsi_arrow"),
        }
        return record

    except Exception as e:
        log.debug("股票 %s(%s) 打分异常: %s", code, name, str(e)[:80])
        return None


# ============================================================================
# 4. 主流程：并发遍历 + 断点续跑
# ============================================================================

def load_completed_codes(csv_path: str) -> set:
    """加载已完成打分的股票代码，用于断点续跑"""
    completed = set()
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path, dtype={"代码": str})
            completed = set(df["代码"].tolist())
            log.info("已加载 %d 只已完成股票（断点续跑）", len(completed))
        except Exception:
            pass
    return completed


def save_results(records: list[dict], csv_path: str, mode: str = "a"):
    """保存结果到CSV"""
    if not records:
        return
    df = pd.DataFrame(records)
    write_header = (mode == "w") or not os.path.exists(csv_path)
    df.to_csv(csv_path, mode=mode, index=False, header=write_header, encoding="utf-8-sig")


def rank_all_stocks(
    top_n: int = DEFAULT_TOP_N,
    workers: int = DEFAULT_WORKERS,
    resume: bool = True,
    output_csv: str = None,
    intermediate_csv: str = None,
    exclude_markets: list[str] | None = None,
    keep_markets: list[str] | None = None,
    use_fundamental_filter: bool = True,
    deep_check_top: int = 200,
):
    """
    主入口：遍历全部A股，多线程打分，输出Top N。

    Args:
        exclude_markets: 排除的板块列表，如 ["创业板", "科创板"]。
                         默认排除创业板+科创板。设为 [] 或 None 则不过滤。
        keep_markets: 只保留的板块列表，如 ["主板"]。与 exclude 二选一。
    """
    if output_csv is None:
        output_csv = DEFAULT_OUTPUT_CSV
    if intermediate_csv is None:
        intermediate_csv = INTERMEDIATE_CSV

    start_time = time.time()

    # 4a. 获取股票列表
    all_stocks = get_all_a_stock_codes()
    if not all_stocks:
        log.error("无法获取股票列表，退出")
        return

    # 4a2. 板块过滤
    if exclude_markets:
        all_stocks = filter_by_market(all_stocks, exclude=exclude_markets)
        log.info("排除板块 %s 后剩余 %d 只股票", exclude_markets, len(all_stocks))
    elif keep_markets:
        all_stocks = filter_by_market(all_stocks, keep=keep_markets)
        log.info("仅保留板块 %s，共 %d 只股票", keep_markets, len(all_stocks))
    else:
        # 不过滤，仅附加板块信息
        all_stocks = filter_by_market(all_stocks)
        log.info("不排除任何板块，共 %d 只股票", len(all_stocks))

    # 4b. 断点续跑：跳过已完成的
    completed = load_completed_codes(intermediate_csv) if resume else set()
    pending = [s for s in all_stocks if s["code"] not in completed]
    log.info("总股票: %d, 已完成: %d, 待处理: %d",
             len(all_stocks), len(completed), len(pending))

    if not pending:
        log.info("全部已完成，直接排序输出")
    else:
        # 4c. 批量获取实时行情
        pending_codes = [s["code"] for s in pending]
        quotes = fetch_batch_realtime_quotes(pending_codes)
        log.info("实时行情覆盖率: %d/%d", len(quotes), len(pending))

        # 4d. 并发打分
        write_lock = Lock()
        buffer: list[dict] = []
        total_processed = 0
        total_success = 0

        def process_and_buffer(stock: dict) -> dict | None:
            code = stock["code"]
            name = stock["name"]
            quote = quotes.get(code)
            return score_one_stock(code, name, quote)

        log.info("开始并发打分（%d线程）...", workers)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_and_buffer, stock): stock
                for stock in pending
            }

            for future in as_completed(futures):
                stock = futures[future]
                total_processed += 1
                try:
                    record = future.result()
                except Exception as e:
                    log.debug("%s(%s) 线程异常: %s", stock["code"], stock["name"], e)
                    record = None

                if record is not None:
                    total_success += 1
                    with write_lock:
                        buffer.append(record)

                # 定期刷盘
                if total_processed % BATCH_SAVE_INTERVAL == 0:
                    with write_lock:
                        if buffer:
                            save_results(buffer, intermediate_csv)
                            buffer.clear()

                # 进度显示
                if total_processed % 100 == 0 or total_processed == len(pending):
                    elapsed = time.time() - start_time
                    rate = total_processed / elapsed if elapsed > 0 else 0
                    eta = (len(pending) - total_processed) / rate if rate > 0 else 0
                    log.info(
                        "进度: %d/%d (%.1f%%), 成功: %d, 速率: %.1f只/秒, ETA: %.0f秒",
                        total_processed, len(pending),
                        total_processed / len(pending) * 100,
                        total_success, rate, eta,
                    )

        # 刷盘剩余
        with write_lock:
            if buffer:
                save_results(buffer, intermediate_csv)
                buffer.clear()

        log.info("打分完成: 成功 %d/%d", total_success, total_processed)

    # 4e. 读取全部结果，排序输出
    log.info("读取全部结果并排序...")
    all_results = pd.read_csv(intermediate_csv, dtype={"代码": str})

    # 按总得分降序
    all_results = all_results.sort_values("总得分", ascending=False)
    all_results = all_results.reset_index(drop=True)

    # 4f. 基本面过滤
    if use_fundamental_filter:
        log.info("=" * 60)
        log.info("开始基本面过滤...")
        all_results = apply_fundamental_filter(
            all_results,
            deep_check_top=deep_check_top,
        )
        # 对未深度检查的股票，标记为 "⏭ 未深度检查"
        mask_unchecked = all_results["基本面原因"].isna()
        all_results.loc[mask_unchecked, "基本面原因"] = "⏭ 未深度检查(排名靠后)"
        all_results.loc[mask_unchecked, "基本面通过"] = True
        all_results.loc[mask_unchecked, "仓位乘数"] = 1.0

        # 只保留通过的
        before = len(all_results)
        all_results = all_results[all_results["基本面通过"] == True].copy()
        log.info("基本面过滤后: %d → %d 只", before, len(all_results))
        log.info("=" * 60)

    # Top N
    top_df = all_results.head(top_n).copy()
    top_df.insert(0, "排名", range(1, len(top_df) + 1))

    # 输出
    top_df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    log.info("Top %d 已保存到: %s", top_n, output_csv)

    # 打印摘要
    print("\n" + "=" * 80)
    print(f"🏆 A股量化打分 Top {top_n}")
    print("=" * 80)
    display_cols = ["排名", "代码", "名称", "现价", "总得分", "可买度", "PE_TTM", "PB"]
    if use_fundamental_filter:
        display_cols += ["ROE", "毛利率", "仓位乘数", "基本面原因"]
    avail_cols = [c for c in display_cols if c in top_df.columns]
    print(top_df[avail_cols].to_string(index=False))

    elapsed_total = time.time() - start_time
    log.info("总耗时: %.0f秒 (%.1f分钟)", elapsed_total, elapsed_total / 60)

    # 统计分布
    print(f"\n📊 全部股票评分分布（共{len(all_results)}只）:")
    bins = [0, 40, 55, 68, 78, 101]
    labels = ["🔴 0-39(回避)", "🟠 40-54(偏弱)", "🟡 55-67(关注)", "🟢 68-77(推荐)", "🟢🟢 78-100(强烈推荐)"]
    all_results["分数段"] = pd.cut(all_results["总得分"], bins=bins, labels=labels, right=False)
    dist = all_results["分数段"].value_counts().sort_index()
    for label, cnt in dist.items():
        pct = cnt / len(all_results) * 100
        print(f"  {label}: {cnt}只 ({pct:.1f}%)")

    return top_df


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="A股全市场量化打分排名",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python rank_all_stocks.py                        # Top100, 10线程
  python rank_all_stocks.py --top 50 --workers 15  # Top50, 15线程
  python rank_all_stocks.py --resume               # 断点续跑
  python rank_all_stocks.py --no-resume            # 从头开始（忽略中间文件）
        """,
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                        help=f"输出Top N (默认: {DEFAULT_TOP_N})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"并发线程数 (默认: {DEFAULT_WORKERS})")
    parser.add_argument("--resume", action="store_true", default=True,
                        help="断点续跑 (默认开启)")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        help="从头开始，忽略中间文件")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT_CSV,
                        help=f"输出CSV文件路径 (默认: {DEFAULT_OUTPUT_CSV})")
    parser.add_argument("--intermediate", type=str, default=INTERMEDIATE_CSV,
                        help="中间结果CSV (默认: all_stocks_score_intermediate.csv)")
    parser.add_argument(
        "--exclude-markets", type=str,
        default=DEFAULT_EXCLUDE_MARKETS,
        help=f"排除的板块（逗号分隔），可选: {','.join(ALL_MARKETS)}。默认排除创业板+科创板。设为空字符串则不过滤。"
    )
    parser.add_argument(
        "--keep-markets", type=str, default=None,
        help=f"仅保留的板块（逗号分隔），可选: {','.join(ALL_MARKETS)}。与 --exclude-markets 二选一。"
    )
    parser.add_argument(
        "--no-fundamental-filter", action="store_true",
        help="禁用基本面过滤（默认开启，排除亏损/PE离谱/ROE低/现金流差）"
    )
    parser.add_argument(
        "--deep-check-top", type=int, default=200,
        help="深度基本面检查的Top N数量 (默认: 200，仅对技术分最高的前N只查ROE/毛利率/现金流)"
    )

    args = parser.parse_args()

    # 解析板块过滤参数
    exclude_markets = None
    keep_markets = None
    if args.keep_markets:
        keep_markets = [m.strip() for m in args.keep_markets.split(",")]
    elif args.exclude_markets:
        exclude_markets = [m.strip() for m in args.exclude_markets.split(",")]

    rank_all_stocks(
        top_n=args.top,
        workers=args.workers,
        resume=args.resume,
        output_csv=args.output,
        intermediate_csv=args.intermediate,
        exclude_markets=exclude_markets,
        keep_markets=keep_markets,
        use_fundamental_filter=not args.no_fundamental_filter,
        deep_check_top=args.deep_check_top,
    )


if __name__ == "__main__":
    main()
