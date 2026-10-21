#!/usr/bin/env python3
"""
美股资金流向（dollar volume 代理）
================================
美股没有 A 股的主力/超大单逐笔数据。用日线成交额 × 涨跌方向作为净流入代理：

  signed_flow = volume * close * sign(close - open)

字段名保留 main_net / super_large 等，方便 run/flow.py 少改。
  - main_net: 当日 signed dollar volume
  - super_large_net: 放量日（量比>1.5）的 signed flow
  - large_net: 量比 1.0-1.5
  - medium/small: 缩量日

用法:
    python core/money_flow.py NVDA
    python core/money_flow.py --top 20 --days 5
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from market_filter import get_market, ALL_MARKETS
from stock_strategy import fetch_daily_kline
from universe import get_soxx_tickers, get_soxx_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DEFAULT_TOP_N = 20
DEFAULT_WORKERS = 8
DEFAULT_DAYS = 1
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")


def get_stock_fund_flow(code: str, days: int = 10) -> pd.DataFrame:
    """近 N 日资金流向明细（dollar volume 代理）。"""
    try:
        df = fetch_daily_kline(code, days=max(days + 40, 60))
    except Exception as e:
        log.warning("获取 %s K线失败: %s", code, e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["pct_change"] = df["close"].pct_change() * 100
    dollar = df["close"] * df["volume"]
    direction = np.sign(df["close"] - df["open"]).replace(0, 0)
    signed = dollar * direction
    vol_ma20 = df["volume"].rolling(20).mean()
    vol_ratio = df["volume"] / vol_ma20.replace(0, np.nan)

    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"]),
        "close": df["close"],
        "pct_change": df["pct_change"],
        "main_net": signed,
        "main_net_pct": (signed / dollar.replace(0, np.nan) * 100).fillna(0),
        "super_large_net": np.where(vol_ratio > 1.5, signed, 0.0),
        "super_large_net_pct": np.where(vol_ratio > 1.5, signed / dollar.replace(0, np.nan) * 100, 0.0),
        "large_net": np.where((vol_ratio > 1.0) & (vol_ratio <= 1.5), signed, 0.0),
        "large_net_pct": np.where((vol_ratio > 1.0) & (vol_ratio <= 1.5), signed / dollar.replace(0, np.nan) * 100, 0.0),
        "medium_net": np.where((vol_ratio > 0.7) & (vol_ratio <= 1.0), signed, 0.0),
        "medium_net_pct": 0.0,
        "small_net": np.where(vol_ratio <= 0.7, signed, 0.0),
        "small_net_pct": 0.0,
        "dollar_volume": dollar,
        "vol_ratio": vol_ratio,
        "code": code,
    })
    out = out.sort_values("date", ascending=False)
    if days > 0:
        out = out.head(days)
    return out.reset_index(drop=True)


def get_stock_fund_flow_summary(code: str, days: int = 1) -> dict:
    df = get_stock_fund_flow(code, days=max(days, 10))
    if df.empty:
        return {"code": code, "error": "无数据"}

    recent = df.head(days).copy()
    result = {
        "code": code,
        "latest_date": str(pd.to_datetime(df.iloc[0]["date"]).date()),
        "close": float(df.iloc[0]["close"]) if pd.notna(df.iloc[0]["close"]) else None,
        "pct_change": float(df.iloc[0]["pct_change"]) if pd.notna(df.iloc[0]["pct_change"]) else None,
    }
    for col in ["main_net", "super_large_net", "large_net", "medium_net", "small_net"]:
        result[f"{col}_cum"] = float(recent[col].sum()) if col in recent.columns else 0.0
    for col in ["main_net_pct", "super_large_net_pct", "large_net_pct", "medium_net_pct", "small_net_pct"]:
        result[f"{col}_avg"] = round(float(recent[col].mean()), 2) if col in recent.columns else 0.0

    result["consecutive_inflow"] = _count_consecutive_flow(df, "main_net", "in")
    result["consecutive_outflow"] = _count_consecutive_flow(df, "main_net", "out")

    for col_prefix in ["main", "super_large", "large", "medium", "small"]:
        net_col = f"{col_prefix}_net"
        if net_col in recent.columns:
            result[f"{col_prefix}_inflow"] = float(recent[recent[net_col] > 0][net_col].sum())
            result[f"{col_prefix}_outflow"] = float(recent[recent[net_col] < 0][net_col].sum())
        else:
            result[f"{col_prefix}_inflow"] = 0.0
            result[f"{col_prefix}_outflow"] = 0.0

    result["flow_score"] = _calc_flow_score(result, days)
    return result


def _count_consecutive_flow(df: pd.DataFrame, col: str, direction: str = "in") -> int:
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
    score = 50.0
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

    consecutive = summary.get("consecutive_inflow", 0)
    if consecutive >= 5:
        score += 15
    elif consecutive >= 3:
        score += 10
    elif consecutive >= 1:
        score += 5
    else:
        out_days = summary.get("consecutive_outflow", 0)
        if out_days >= 5:
            score -= 15
        elif out_days >= 3:
            score -= 10

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


def rank_all_by_money_flow(
    top_n: int = DEFAULT_TOP_N,
    days: int = DEFAULT_DAYS,
    workers: int = DEFAULT_WORKERS,
    market_filter: Optional[str] = None,
    output_csv: Optional[str] = None,
) -> list[dict]:
    universe = get_soxx_universe(top_n=20, live=True)
    all_codes = [s["code"] for s in universe]
    names = {s["code"]: s["name"] for s in universe}
    if market_filter:
        all_codes = [c for c in all_codes if get_market(c) == market_filter]

    log.info("SOXX 资金流向统计: %d 只, days=%d", len(all_codes), days)
    results = []

    def process_one(code):
        try:
            r = get_stock_fund_flow_summary(code, days=days)
            r["name"] = names.get(code, code)
            return r
        except Exception as e:
            return {"code": code, "error": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_one, c): c for c in all_codes}
        for future in as_completed(futures):
            try:
                result = future.result(timeout=60)
                if result and "error" not in result:
                    results.append(result)
            except Exception:
                pass

    results.sort(key=lambda x: x.get("main_net_cum", 0), reverse=True)
    for i, r in enumerate(results[:top_n]):
        r["rank"] = i + 1
    top_results = results[:top_n]

    if output_csv:
        _save_flow_csv(top_results, output_csv, days)
    else:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        _save_flow_csv(top_results, os.path.join(OUTPUT_DIR, f"money_flow_top{top_n}.csv"), days)
    return top_results


def _save_flow_csv(results: list[dict], filepath: str, days: int):
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    rows = []
    for r in results:
        rows.append({
            "排名": r.get("rank", ""),
            "代码": r["code"],
            "名称": r.get("name", ""),
            "日期": r.get("latest_date", ""),
            "收盘价": r.get("close", ""),
            "涨跌幅%": r.get("pct_change", ""),
            f"净流入({days}日累计)": _fmt_amount(r.get("main_net_cum", 0)),
            "净占比%": r.get("main_net_pct_avg", 0),
            "放量净流入": _fmt_amount(r.get("super_large_net_cum", 0)),
            "连续净流入天数": r.get("consecutive_inflow", 0),
            "连续净流出天数": r.get("consecutive_outflow", 0),
            "资金评分": r.get("flow_score", 0),
        })
    pd.DataFrame(rows).to_csv(filepath, index=False, encoding="utf-8-sig")
    log.info("结果已保存: %s", filepath)


def _fmt_amount(val: float) -> str:
    val = float(val or 0)
    if abs(val) >= 1e9:
        return f"{val/1e9:.2f}十亿"
    if abs(val) >= 1e6:
        return f"{val/1e6:.2f}百万"
    if abs(val) >= 1e3:
        return f"{val/1e3:.2f}千"
    return f"{val:.0f}美元"


def summarize_by_market(results: list[dict]) -> dict:
    summary = {}
    for r in results:
        code = r.get("code", "")
        market = get_market(code) or "其他"
        if market not in summary:
            summary[market] = {
                "count": 0, "main_net_cum": 0.0, "main_inflow": 0.0, "main_outflow": 0.0,
                "inflow_stocks": 0, "outflow_stocks": 0,
                "top_inflow_stock": None, "top_inflow_amount": -float("inf"),
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


def main():
    parser = argparse.ArgumentParser(description="SOXX 资金流向（dollar volume 代理）")
    parser.add_argument("code", nargs="?", default=None, help="美股代码，如 NVDA")
    parser.add_argument("--top", type=int, default=None, help="排名 Top N")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--market", type=str, default=None, help=f"细分过滤: {','.join(ALL_MARKETS)}")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    if args.code and not args.top:
        summary = get_stock_fund_flow_summary(args.code.strip(), days=args.days)
        if "error" in summary:
            log.error("获取失败: %s", summary["error"])
            return
        print("\n" + "=" * 70)
        print(f"  {args.code} 资金流向 (近{args.days}日, dollar volume 代理)")
        print("=" * 70)
        print(f"  日期: {summary['latest_date']}  收盘: ${summary.get('close')}  涨跌: {summary.get('pct_change')}%")
        print(f"  资金评分: {summary.get('flow_score')}")
        print(f"  净流入: {_fmt_amount(summary.get('main_net_cum', 0))}")
        print(f"  连续流入/流出: {summary.get('consecutive_inflow')} / {summary.get('consecutive_outflow')} 天")
        print("=" * 70)
        return

    results = rank_all_by_money_flow(
        top_n=args.top or DEFAULT_TOP_N,
        days=args.days, workers=args.workers,
        market_filter=args.market, output_csv=args.output,
    )
    print("\n" + "=" * 80)
    print(f"  SOXX 资金流向 Top{len(results)}")
    print("=" * 80)
    for r in results:
        print(f"  {r.get('rank',''):<4} {r['code']:<6} {_fmt_amount(r.get('main_net_cum',0)):>14}  分{r.get('flow_score',0):.0f}")
    print("=" * 80)


if __name__ == "__main__":
    main()
