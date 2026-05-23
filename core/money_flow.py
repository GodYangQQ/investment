#!/usr/bin/env python3
"""
A股资金流向分析模块
==================
提供个股/全市场的主力资金、超大单、大单、中单、小单的流入/流出/净流入统计。

数据源：akshare (stock_individual_fund_flow)
字段说明：
  - 主力净流入 = 超大单净流入 + 大单净流入
  - 净额单位：元
  - 净占比单位：%

支持统计维度：
  1. 单只股票每日资金流向明细
  2. 单只股票近N日累计净流入
  3. 全市场资金净流入排名
  4. 板块资金流向汇总

用法：
    # 单只股票资金流向
    python money_flow.py 600519

    # 全市场排名 Top50
    python money_flow.py --top 50

    # 近5日累计排名
    python money_flow.py --top 50 --days 5

    # 指定板块
    python money_flow.py --top 30 --market 主板
"""

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

try:
    import akshare as ak
    _HAS_AKSHARE = True
except ImportError:
    _HAS_AKSHARE = False

# 确保可以 import 同目录下的模块
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

from market_filter import get_market, ALL_MARKETS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ============================================================================
# 配置
# ============================================================================

DEFAULT_TOP_N = 50
DEFAULT_WORKERS = 8
DEFAULT_DAYS = 1  # 默认统计最近1天

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")

# akshare 市场代码映射
_MARKET_MAP = {
    "600": "sh", "601": "sh", "603": "sh", "605": "sh",
    "000": "sz", "001": "sz", "002": "sz", "003": "sz",
    "300": "sz", "301": "sz",
    "688": "sh",
}

# ============================================================================
# 1. 个股资金流向获取
# ============================================================================

def _get_market_code(code: str) -> str:
    """根据股票代码推断 akshare 所需的 market 参数"""
    code = str(code).strip()
    for prefix, market in _MARKET_MAP.items():
        if code.startswith(prefix):
            return market
    # 北交所等
    if code.startswith(("4", "8")):
        return "bj"
    return "sh"  # 默认沪市


def get_stock_fund_flow(code: str, days: int = 10) -> pd.DataFrame:
    """
    获取单只股票近N日资金流向数据。

    Args:
        code: 股票代码，如 "600519"
        days: 获取最近多少天的数据（最多约120天）

    Returns:
        DataFrame，包含日期、收盘价、涨跌幅、各类型资金净流入额和占比
    """
    if not _HAS_AKSHARE:
        raise ImportError("需要安装 akshare: pip install akshare")

    market = _get_market_code(code)
    try:
        df = ak.stock_individual_fund_flow(stock=code, market=market)
    except Exception as e:
        log.warning("获取 %s 资金流向失败: %s", code, e)
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # 标准化列名
    col_map = {
        "日期": "date",
        "收盘价": "close",
        "涨跌幅": "pct_change",
        "主力净流入-净额": "main_net",
        "主力净流入-净占比": "main_net_pct",
        "超大单净流入-净额": "super_large_net",
        "超大单净流入-净占比": "super_large_net_pct",
        "大单净流入-净额": "large_net",
        "大单净流入-净占比": "large_net_pct",
        "中单净流入-净额": "medium_net",
        "中单净流入-净占比": "medium_net_pct",
        "小单净流入-净额": "small_net",
        "小单净流入-净占比": "small_net_pct",
    }
    df = df.rename(columns=col_map)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date", ascending=False)

    # 只保留最近 days 天
    if days > 0:
        df = df.head(days)

    df["code"] = code
    return df


def get_stock_fund_flow_summary(code: str, days: int = 1) -> dict:
    """
    获取单只股票近N日资金流向汇总。

    Returns:
        dict: {
            code, name, latest_date, close, pct_change,
            main_net (累计主力净流入), main_net_pct (加权净占比),
            super_large_net, large_net, medium_net, small_net,
            flow_score (资金流向评分: 0-100),
            consecutive_inflow (连续净流入天数),
            consecutive_outflow (连续净流出天数),
        }
    """
    df = get_stock_fund_flow(code, days=max(days, 10))  # 多取几天用于计算连续

    if df.empty:
        return {"code": code, "error": "无数据"}

    # 最近 days 天的数据
    recent = df.head(days).copy()

    result = {
        "code": code,
        "latest_date": str(df.iloc[0]["date"].date()),
        "close": float(df.iloc[0]["close"]) if pd.notna(df.iloc[0]["close"]) else None,
        "pct_change": float(df.iloc[0]["pct_change"]) if pd.notna(df.iloc[0]["pct_change"]) else None,
    }

    # 累计净流入（近N日求和）
    for col in ["main_net", "super_large_net", "large_net", "medium_net", "small_net"]:
        if col in recent.columns:
            total = recent[col].sum()
            result[f"{col}_cum"] = float(total) if pd.notna(total) else 0.0
        else:
            result[f"{col}_cum"] = 0.0

    # 加权净占比（按成交额加权，近似用逐日简单平均）
    for col in ["main_net_pct", "super_large_net_pct", "large_net_pct", "medium_net_pct", "small_net_pct"]:
        if col in recent.columns:
            avg = recent[col].mean()
            result[f"{col}_avg"] = round(float(avg), 2) if pd.notna(avg) else 0.0
        else:
            result[f"{col}_avg"] = 0.0

    # 连续净流入/流出天数
    result["consecutive_inflow"] = _count_consecutive_flow(df, "main_net", direction="in")
    result["consecutive_outflow"] = _count_consecutive_flow(df, "main_net", direction="out")

    # 流入流出分开统计
    for col_prefix, label in [("main", "主力"), ("super_large", "超大单"), ("large", "大单"),
                               ("medium", "中单"), ("small", "小单")]:
        net_col = f"{col_prefix}_net"
        if net_col in recent.columns:
            inflow = recent[recent[net_col] > 0][net_col].sum()
            outflow = recent[recent[net_col] < 0][net_col].sum()
            result[f"{col_prefix}_inflow"] = float(inflow) if pd.notna(inflow) else 0.0
            result[f"{col_prefix}_outflow"] = float(outflow) if pd.notna(outflow) else 0.0

    # 资金流向评分 (0-100)
    result["flow_score"] = _calc_flow_score(result, days)

    return result


def _count_consecutive_flow(df: pd.DataFrame, col: str, direction: str = "in") -> int:
    """计算连续净流入/流出天数"""
    if col not in df.columns or df.empty:
        return 0
    count = 0
    for _, row in df.iterrows():
        val = row[col]
        if pd.isna(val):
            break
        if direction == "in" and val > 0:
            count += 1
        elif direction == "out" and val < 0:
            count += 1
        else:
            break
    return count


def _calc_flow_score(summary: dict, days: int) -> float:
    """
    资金流向综合评分 (0-100)

    考虑因素：
    - 主力净占比：正值得分高
    - 主力净流入额：绝对值大得分高（考虑流通市值）
    - 连续净流入天数：持续性强得分高
    - 超大单占比：机构行为信号
    """
    score = 50.0  # 基准分

    # 1) 主力净占比 (±20分)
    main_pct = summary.get("main_net_pct_avg", 0)
    if main_pct > 10:
        score += 20
    elif main_pct > 5:
        score += 15
    elif main_pct > 2:
        score += 10
    elif main_pct > 0:
        score += 5
    elif main_pct < -10:
        score -= 20
    elif main_pct < -5:
        score -= 15
    elif main_pct < -2:
        score -= 10
    elif main_pct < 0:
        score -= 5

    # 2) 连续流入天数 (+15分)
    consecutive = summary.get("consecutive_inflow", 0)
    if consecutive >= 5:
        score += 15
    elif consecutive >= 3:
        score += 10
    elif consecutive >= 1:
        score += 5
    else:
        # 连续流出扣分
        out_days = summary.get("consecutive_outflow", 0)
        if out_days >= 5:
            score -= 15
        elif out_days >= 3:
            score -= 10

    # 3) 超大单占比 (+15分，机构行为)
    super_pct = summary.get("super_large_net_pct_avg", 0)
    if super_pct > 5:
        score += 15
    elif super_pct > 2:
        score += 10
    elif super_pct > 0:
        score += 5
    elif super_pct < -5:
        score -= 10

    return max(0, min(100, round(score, 1)))


# ============================================================================
# 2. 全市场资金流向排名
# ============================================================================

def _get_all_a_stock_codes() -> list[str]:
    """从新浪获取全部A股代码列表"""
    import requests as req
    log.info("获取全部A股代码...")
    try:
        count_url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount?node=hs_a"
        count = int(req.get(count_url, timeout=10).text.strip('"'))
    except Exception:
        log.warning("获取股票总数失败，使用默认5000")
        count = 5000

    codes = []
    for page in range(1, count // 80 + 2):
        url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        params = {"page": page, "num": 80, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "page"}
        try:
            r = req.get(url, params=params, timeout=15)
            data = r.json()
            if not data:
                break
            codes.extend([item["code"] for item in data])
        except Exception:
            break
        time.sleep(0.08)

    log.info("共获取 %d 只A股", len(codes))
    return codes


def rank_all_by_money_flow(
    top_n: int = DEFAULT_TOP_N,
    days: int = DEFAULT_DAYS,
    workers: int = DEFAULT_WORKERS,
    market_filter: Optional[str] = None,
    output_csv: Optional[str] = None,
) -> list[dict]:
    """
    全市场资金流向排名。

    Args:
        top_n: 返回前N名
        days: 统计最近N天
        workers: 并发线程数
        market_filter: 板块过滤，如 "主板"/"创业板"/None=全部
        output_csv: 输出CSV路径，None则不保存

    Returns:
        排名列表 [{"rank":1, "code":"600519", ...}, ...]
    """
    all_codes = _get_all_a_stock_codes()
    if not all_codes:
        log.error("无法获取股票列表")
        return []

    # 板块过滤
    if market_filter:
        before = len(all_codes)
        all_codes = [c for c in all_codes if get_market(c) == market_filter]
        log.info("板块过滤 '%s': %d -> %d 只", market_filter, before, len(all_codes))

    log.info("开始统计资金流向 (days=%d, workers=%d)...", days, workers)

    results = []
    completed = 0
    lock = __import__("threading").Lock()

    def process_one(code):
        try:
            return get_stock_fund_flow_summary(code, days=days)
        except Exception as e:
            return {"code": code, "error": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_one, c): c for c in all_codes}
        for future in as_completed(futures):
            try:
                result = future.result(timeout=30)
                if result and "error" not in result:
                    results.append(result)
            except Exception:
                pass
            with lock:
                completed += 1
                if completed % 200 == 0:
                    log.info("  进度: %d/%d (%.1f%%)", completed, len(all_codes),
                             completed / len(all_codes) * 100)

    log.info("完成! 有效数据: %d 只", len(results))

    # 按主力净流入排序
    results.sort(key=lambda x: x.get("main_net_cum", 0), reverse=True)

    # 标注排名
    for i, r in enumerate(results[:top_n]):
        r["rank"] = i + 1

    top_results = results[:top_n]

    # 输出CSV
    if output_csv:
        _save_flow_csv(top_results, output_csv, days)
    else:
        default_csv = os.path.join(OUTPUT_DIR, f"money_flow_top{top_n}.csv")
        _save_flow_csv(top_results, default_csv, days)

    return top_results


def _save_flow_csv(results: list[dict], filepath: str, days: int):
    """保存资金流向排名CSV"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    rows = []
    for r in results:
        rows.append({
            "排名": r.get("rank", ""),
            "代码": r["code"],
            "日期": r.get("latest_date", ""),
            "收盘价": r.get("close", ""),
            "涨跌幅%": r.get("pct_change", ""),
            f"主力净流入({days}日累计)": _fmt_amount(r.get("main_net_cum", 0)),
            "主力净占比%": r.get("main_net_pct_avg", 0),
            f"主力流入({days}日)": _fmt_amount(r.get("main_inflow", 0)),
            f"主力流出({days}日)": _fmt_amount(r.get("main_outflow", 0)),
            "超大单净流入": _fmt_amount(r.get("super_large_net_cum", 0)),
            "超大单净占比%": r.get("super_large_net_pct_avg", 0),
            "大单净流入": _fmt_amount(r.get("large_net_cum", 0)),
            "大单净占比%": r.get("large_net_pct_avg", 0),
            "中单净流入": _fmt_amount(r.get("medium_net_cum", 0)),
            "小单净流入": _fmt_amount(r.get("small_net_cum", 0)),
            "连续净流入天数": r.get("consecutive_inflow", 0),
            "连续净流出天数": r.get("consecutive_outflow", 0),
            "资金评分": r.get("flow_score", 0),
        })

    df = pd.DataFrame(rows)
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    log.info("结果已保存: %s", filepath)


def _fmt_amount(val: float) -> str:
    """格式化金额（元->亿）"""
    if abs(val) >= 1e8:
        return f"{val/1e8:.2f}亿"
    elif abs(val) >= 1e4:
        return f"{val/1e4:.2f}万"
    else:
        return f"{val:.0f}元"


# ============================================================================
# 3. 板块资金流向汇总
# ============================================================================

def summarize_by_market(results: list[dict]) -> dict:
    """按板块汇总资金流向"""
    summary = {}
    for r in results:
        code = r.get("code", "")
        market = get_market(code) or "其他"
        if market not in summary:
            summary[market] = {
                "count": 0,
                "main_net_cum": 0.0,
                "main_inflow": 0.0,
                "main_outflow": 0.0,
                "inflow_stocks": 0,  # 净流入个股数
                "outflow_stocks": 0,  # 净流出个股数
                "top_inflow_stock": None,
                "top_inflow_amount": -float("inf"),
            }
        s = summary[market]
        s["count"] += 1
        main_net = r.get("main_net_cum", 0)
        s["main_net_cum"] += main_net
        s["main_inflow"] += r.get("main_inflow", 0)
        s["main_outflow"] += r.get("main_outflow", 0)
        if main_net > 0:
            s["inflow_stocks"] += 1
        else:
            s["outflow_stocks"] += 1
        if main_net > s["top_inflow_amount"]:
            s["top_inflow_amount"] = main_net
            s["top_inflow_stock"] = code

    return summary


# ============================================================================
# 4. 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="A股资金流向统计 - 主力/超大单/大单/中单/小单 流入流出分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 模式选择
    parser.add_argument("code", nargs="?", default=None,
                        help="股票代码（如 600519），不填则进行全市场排名")
    parser.add_argument("--top", type=int, default=None,
                        help="全市场排名Top N（默认50）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="统计最近N天（默认1，即当日）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="并发线程数（默认8）")
    parser.add_argument("--market", type=str, default=None,
                        help="板块过滤: 主板/创业板/科创板/北交所")
    parser.add_argument("--output", type=str, default=None,
                        help="输出CSV文件路径")
    parser.add_argument("--summary", action="store_true",
                        help="按板块汇总资金流向")

    args = parser.parse_args()

    if not _HAS_AKSHARE:
        log.error("需要安装 akshare: pip install akshare")
        sys.exit(1)

    # 模式1: 单只股票
    if args.code and not args.top:
        code = args.code.strip()
        summary = get_stock_fund_flow_summary(code, days=args.days)

        if "error" in summary:
            log.error("获取失败: %s", summary["error"])
            return

        print("\n" + "=" * 70)
        print(f"  {code} 资金流向分析 (近{args.days}日)")
        print("=" * 70)
        print(f"  最新日期: {summary['latest_date']}")
        print(f"  收盘价:   {summary.get('close', 'N/A')}")
        print(f"  涨跌幅:   {summary.get('pct_change', 'N/A')}%")
        print(f"  资金评分: {summary.get('flow_score', 'N/A')} (0-100)")
        print("-" * 70)
        print(f"  {'类型':<10} {'净流入':>16} {'净占比':>10} {'流入':>16} {'流出':>16}")
        print("-" * 70)

        for col, label in [("main", "主力"), ("super_large", "超大单"), ("large", "大单"),
                           ("medium", "中单"), ("small", "小单")]:
            net = summary.get(f"{col}_net_cum", 0)
            pct = summary.get(f"{col}_net_pct_avg", 0)
            inflow = summary.get(f"{col}_inflow", 0)
            outflow = summary.get(f"{col}_outflow", 0)
            print(f"  {label:<10} {_fmt_amount(net):>16} {pct:>9.2f}% {_fmt_amount(inflow):>16} {_fmt_amount(outflow):>16}")

        print("-" * 70)
        print(f"  连续净流入: {summary.get('consecutive_inflow', 0)} 天")
        print(f"  连续净流出: {summary.get('consecutive_outflow', 0)} 天")
        print("=" * 70)

        # 详细每日数据
        print("\n  近10日明细:")
        df = get_stock_fund_flow(code, days=10)
        if not df.empty:
            detail_cols = ["date", "close", "pct_change", "main_net", "main_net_pct"]
            available = [c for c in detail_cols if c in df.columns]
            print(df[available].to_string(index=False))

        return

    # 模式2: 全市场排名
    top_n = args.top or (DEFAULT_TOP_N if not args.code else DEFAULT_TOP_N)

    results = rank_all_by_money_flow(
        top_n=top_n,
        days=args.days,
        workers=args.workers,
        market_filter=args.market,
        output_csv=args.output,
    )

    if not results:
        log.error("没有获取到有效数据")
        return

    # 打印排名
    print("\n" + "=" * 90)
    title = f"A股资金流向排名 Top{len(results)}"
    if args.market:
        title += f" ({args.market})"
    if args.days > 1:
        title += f" [近{args.days}日累计]"
    print(f"  {title}")
    print("=" * 90)
    print(f"  {'排名':<5} {'代码':<8} {'主力净流入':>14} {'净占比':>8} {'评分':>6} {'连续流入':>8} {'超大单净流入':>14}")
    print("-" * 90)

    for r in results:
        code = r["code"]
        main_net = _fmt_amount(r.get("main_net_cum", 0))
        main_pct = r.get("main_net_pct_avg", 0)
        score = r.get("flow_score", 0)
        consec = r.get("consecutive_inflow", 0)
        super_net = _fmt_amount(r.get("super_large_net_cum", 0))
        rank = r.get("rank", "")
        print(f"  {rank:<5} {code:<8} {main_net:>14} {main_pct:>7.2f}% {score:>5.0f} {consec:>8}天 {super_net:>14}")

    print("=" * 90)

    # 板块汇总
    if args.summary:
        print("\n  板块资金流向汇总:")
        print("-" * 60)
        summary = summarize_by_market(results)
        print(f"  {'板块':<8} {'个股数':>6} {'主力净流入':>14} {'净流入股数':>8}")
        print("-" * 60)
        for market, s in sorted(summary.items()):
            print(f"  {market:<8} {s['count']:>6} {_fmt_amount(s['main_net_cum']):>14} {s['inflow_stocks']:>8}")
        print("-" * 60)


if __name__ == "__main__":
    main()
