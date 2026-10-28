"""
Stock Data Analyzer (US / SOXX)
Fetches real-time + historical data for a US ticker, computes quantitative
indicators, and outputs structured data for LLM analysis.

Usage:
    python stock_strategy.py NVDA
    python stock_strategy.py NVDA --kline-days 180 --output data_NVDA.json
"""

import os
import json
import argparse
import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from core.datasets.us_data import (
    fetch_realtime_quote,
    fetch_daily_kline,
    fetch_financial_data,
    fmt_market_cap,
    _HAS_YFINANCE,
    _HAS_AKSHARE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

_FINANCIAL_METRICS = [
    "归母净利润",
    "营业总收入",
    "营业成本",
    "扣非净利润",
    "经营现金流量净额",
    "基本每股收益",
    "每股净资产",
    "净资产收益率(ROE)",
    "毛利率",
    "销售净利率",
    "资产负债率",
    "商誉",
]


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """精简版指标计算：只保留核心的MA/ATR/BB/量价/K线形态/位置。"""
    close = df["close"]
    volume = df["volume"]
    high = df["high"]
    low = df["low"]
    open_price = df["open"]
    prev_close = close.shift(1)

    # ---- 核心均线 ----
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()

    # ---- ATR (14-day) ----
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    # ---- Bollinger Bands (上下轨) ----
    bb_std = close.rolling(20).std()
    df["bb_upper"] = df["ma20"] + 2 * bb_std
    df["bb_lower"] = df["ma20"] - 2 * bb_std

    # ---- K线形态 ----
    df["daily_return"] = close.pct_change() * 100
    df["daily_range_pct"] = (high - low) / prev_close * 100
    df["body_pct"] = (close - open_price) / prev_close * 100
    df["upper_shadow_pct"] = (high - pd.concat([close, open_price], axis=1).max(axis=1)) / prev_close * 100
    df["lower_shadow_pct"] = (pd.concat([close, open_price], axis=1).min(axis=1) - low) / prev_close * 100

    # ---- 位置分析 ----
    for window in [20, 60]:
        df[f"high_{window}d"] = high.rolling(window).max()
        df[f"low_{window}d"] = low.rolling(window).min()
        df[f"price_pos_{window}d"] = (
            (close - df[f"low_{window}d"])
            / (df[f"high_{window}d"] - df[f"low_{window}d"]).replace(0, np.nan)
            * 100
        )

    # ---- 波动率 ----
    df["volatility_5d"] = df["daily_return"].rolling(5).std()
    df["volatility_20d"] = df["daily_return"].rolling(20).std()

    # ---- Volume ----
    df["vol_ma5"] = volume.rolling(5).mean()
    df["vol_ma20"] = volume.rolling(20).mean()

    # ---- 连续阴阳 + 缺口 ----
    df["is_up"] = (close > prev_close).astype(int)
    df["is_down"] = (close < prev_close).astype(int)
    df["gap_pct"] = (open_price - prev_close) / prev_close * 100

    return df


def _count_consecutive(series: pd.Series, idx: int, value: int) -> int:
    count = 0
    for i in range(idx, -1, -1):
        if series.iloc[i] == value:
            count += 1
        else:
            break
    return count


def get_latest_signals(df: pd.DataFrame, quote: dict) -> dict:
    """精简版信号提取：核心MA/ATR/BB/量价/K线形态/位置。"""
    latest = df.iloc[-1]
    last_idx = len(df) - 1

    price = latest["close"]

    def rnd(val, decimals=2):
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return None
        return round(float(val), decimals)

    signals = {
        "current_price": rnd(price),
        "ma20": rnd(latest["ma20"]),
        "ma60": rnd(latest["ma60"]),
        "bb_upper": rnd(latest["bb_upper"]),
        "bb_lower": rnd(latest["bb_lower"]),
        "atr14": rnd(latest["atr14"]),
        "vol_ratio": rnd(latest["volume"] / latest["vol_ma20"]) if latest["vol_ma20"] > 0 else None,
    }

    # K线形态
    signals["body_pct"] = rnd(latest["body_pct"])
    signals["upper_shadow_pct"] = rnd(latest["upper_shadow_pct"])
    signals["lower_shadow_pct"] = rnd(latest["lower_shadow_pct"])
    signals["daily_return"] = rnd(latest["daily_return"])
    signals["daily_range_pct"] = rnd(latest["daily_range_pct"])

    # 位置
    for w in [20, 60]:
        signals[f"high_{w}d"] = rnd(latest.get(f"high_{w}d"))
        signals[f"low_{w}d"] = rnd(latest.get(f"low_{w}d"))
        signals[f"price_pos_{w}d"] = rnd(latest.get(f"price_pos_{w}d"), 1)

    # 波动率
    signals["volatility_5d"] = rnd(latest["volatility_5d"])
    signals["volatility_20d"] = rnd(latest["volatility_20d"])

    # 涨跌幅
    if len(df) >= 6:
        signals["pct_5d"] = rnd((price / df.iloc[-6]["close"] - 1) * 100)
    if len(df) >= 21:
        signals["pct_20d"] = rnd((price / df.iloc[-21]["close"] - 1) * 100)

    # 连续阴/阳线
    signals["consecutive_up"] = _count_consecutive(df["is_up"], last_idx, 1)
    signals["consecutive_down"] = _count_consecutive(df["is_down"], last_idx, 1)

    # Volume
    signals["today_volume"] = int(latest["volume"])
    signals["vol_ma5"] = int(latest["vol_ma5"]) if not np.isnan(latest["vol_ma5"]) else None
    signals["vol_ma20"] = int(latest["vol_ma20"]) if not np.isnan(latest["vol_ma20"]) else None

    # 缺口
    signals["latest_gap_pct"] = rnd(latest["gap_pct"])

    # 量价形态（5日）
    recent_5 = df.tail(5)
    price_up_5 = recent_5["close"].iloc[-1] > recent_5["close"].iloc[0]
    vol_increasing_5 = recent_5["volume"].iloc[-1] > recent_5["volume"].iloc[0] * 1.2
    vol_decreasing_5 = recent_5["volume"].iloc[-1] < recent_5["volume"].iloc[0] * 0.8

    if price_up_5 and vol_increasing_5:
        signals["vol_price_pattern"] = "放量上涨"
    elif price_up_5 and vol_decreasing_5:
        signals["vol_price_pattern"] = "缩量上涨"
    elif not price_up_5 and vol_increasing_5:
        signals["vol_price_pattern"] = "放量下跌"
    elif not price_up_5 and vol_decreasing_5:
        signals["vol_price_pattern"] = "缩量下跌"
    else:
        signals["vol_price_pattern"] = "量价平稳"

    return signals


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def generate_kline_summary(df: pd.DataFrame) -> str:
    """生成最近10日详细K线，供 AI 做形态识别"""
    recent = df.tail(10)
    lines = []
    for _, row in recent.iterrows():
        change = row["close"] - row["open"]
        direction = "阳线" if change > 0 else ("阴线" if change < 0 else "十字星")
        pct = (row["close"] / row["open"] - 1) * 100
        vol_vs_ma5 = row["volume"] / row["vol_ma5"] if row["vol_ma5"] > 0 else 1
        vol_tag = "放量" if vol_vs_ma5 > 1.3 else ("缩量" if vol_vs_ma5 < 0.7 else "平量")
        lines.append(
            f"  {row['date'].strftime('%Y-%m-%d')}: "
            f"开{row['open']:.2f} 收{row['close']:.2f} "
            f"高{row['high']:.2f} 低{row['low']:.2f} "
            f"量{int(row['volume'])} {direction}({pct:+.2f}%) {vol_tag}"
        )
    return "\n".join(lines)


def generate_pattern_kline_data(df: pd.DataFrame) -> str:
    """
    生成供AI做技术形态识别的完整K线数据。
    包含：近180日收盘价序列、局部高低点（波峰波谷）、关键均线位置。
    AI 可据此判断：头肩顶/底、M顶/W底、V形反转、三角形整理、旗形、楔形、杯柄等。
    180日足以覆盖杯柄（6-20周）等中长期形态。
    """
    lines = []
    N = min(180, len(df))  # 最多180日
    df_tail = df.tail(N).copy()
    n_days = len(df_tail)

    # ---- 1. 近N日收盘价序列（用于画图判断）----
    closes = df_tail["close"].values
    lines.append(f"## 近{n_days}日收盘价序列（从早到晚）")
    lines.append("```")
    for i, (date, close) in enumerate(zip(df_tail["date"], closes)):
        marker = ""
        if i >= n_days - 10:
            marker = " ←最近10日"
        lines.append(f"  Day{i+1:>3}  {date.strftime('%Y-%m-%d')}  {close:.2f}{marker}")
    lines.append("```")

    # ---- 2. 局部波峰/波谷 ----
    from scipy.signal import argrelextrema
    import numpy as np

    close_arr = np.array(closes)
    # 根据数据量自适应 order：~5%的数据长度，最少5最多12
    peak_order = max(5, min(12, n_days // 20))
    peaks_idx = argrelextrema(close_arr, np.greater, order=peak_order)[0]
    valleys_idx = argrelextrema(close_arr, np.less, order=peak_order)[0]

    lines.append("")
    lines.append(f"## 近{n_days}日关键波峰/波谷（order={peak_order}，前后{peak_order}日极值）")
    lines.append("> AI注意：以下为程序自动检测的局部极值点，可能存在噪音。请结合价格走势人工判断有效形态。")

    all_points = []
    for idx in peaks_idx:
        all_points.append((idx, "波峰🔺", df_tail.iloc[idx]["date"], close_arr[idx],
                           int(df_tail.iloc[idx]["volume"])))
    for idx in valleys_idx:
        all_points.append((idx, "波谷🔻", df_tail.iloc[idx]["date"], close_arr[idx],
                           int(df_tail.iloc[idx]["volume"])))
    all_points.sort(key=lambda x: x[0])

    if all_points:
        lines.append("| 序号 | 日期 | 类型 | 价格 | 成交量 |")
        lines.append("|------|------|------|------|--------|")
        for i, (_, ptype, dt, price, vol) in enumerate(all_points, 1):
            lines.append(f"| {i} | {dt.strftime('%Y-%m-%d')} | {ptype} | {price:.2f} | {vol} |")
    else:
        lines.append("  (该区间未检测到明显波峰/波谷)")

    # ---- 3. 关键价格区间 ----
    lines.append("")
    lines.append(f"## 近{n_days}日关键价格区间")
    high_N = df_tail["high"].max()
    low_N = df_tail["low"].min()
    last_close = closes[-1]
    lines.append(f"- {n_days}日最高价: {high_N:.2f}")
    lines.append(f"- {n_days}日最低价: {low_N:.2f}")
    lines.append(f"- {n_days}日区间幅度: {(high_N/low_N - 1)*100:.1f}%")
    lines.append(f"- 当前价在区间位置: {(last_close - low_N)/(high_N - low_N)*100:.1f}%")

    # ---- 4. 成交量辅助 ----
    lines.append("")
    lines.append(f"## 近{n_days}日成交量概况")
    vol_arr = df_tail["volume"].values
    avg_vol = np.mean(vol_arr)
    max_vol = np.max(vol_arr)
    max_vol_date = df_tail.iloc[np.argmax(vol_arr)]["date"]
    recent_vol = vol_arr[-5:].mean()
    lines.append(f"- {n_days}日均量: {avg_vol:.0f}")
    lines.append(f"- 最大量日: {max_vol_date.strftime('%Y-%m-%d')} ({max_vol:.0f}，为均量的{max_vol/avg_vol:.1f}倍)")
    lines.append(f"- 近5日均量: {recent_vol:.0f} (vs {n_days}日均量: {(recent_vol/avg_vol-1)*100:+.1f}%)")

    # ---- 5. 分段摘要：每20日一组，便于AI快速定位 ----
    lines.append("")
    lines.append(f"## 近{n_days}日价格分段摘要（每20日一组，便于快速定位形态）")
    lines.append("| 段 | 日期范围 | 最高价 | 最低价 | 涨跌% | 均量 |")
    lines.append("|------|----------|--------|--------|-------|------|")
    seg_size = 20
    for seg_start in range(0, n_days, seg_size):
        seg_end = min(seg_start + seg_size, n_days)
        seg = df_tail.iloc[seg_start:seg_end]
        seg_high = seg["high"].max()
        seg_low = seg["low"].min()
        seg_chg = (seg["close"].iloc[-1] / seg["close"].iloc[0] - 1) * 100
        seg_vol = seg["volume"].mean()
        date_range = f"{seg['date'].iloc[0].strftime('%m-%d')}~{seg['date'].iloc[-1].strftime('%m-%d')}"
        lines.append(f"| 第{seg_start//seg_size+1}段 | {date_range} | {seg_high:.2f} | {seg_low:.2f} | {seg_chg:+.1f}% | {seg_vol:.0f} |")

    return "\n".join(lines)


def format_analysis_text(quote: dict, signals: dict, kline_summary: str,
                         financials: Optional[dict] = None,
                         pattern_data: Optional[str] = None) -> str:
    """Format all data into a structured text block for LLM analysis."""
    lines = []
    lines.append("=" * 80)
    lines.append(f"股票: {quote['name']} ({quote['code']})")
    lines.append(f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)

    lines.append("")
    lines.append("## 基本信息")
    lines.append(f"- 当前价格: ${quote['price']:.2f}")
    lines.append(f"- 今日涨跌: {quote['pct_change']:+.2f}%")
    lines.append(f"- PE-TTM: {quote['pe_ttm']:.2f}")
    lines.append(f"- PB: {quote['pb']:.2f}")
    lines.append(f"- 总市值: {fmt_market_cap(quote.get('market_cap') or quote.get('total_mv_yi', 0))}")
    lines.append(f"- 换手率: {quote['turnover']:.2f}%")

    # ---- Financial / Fundamental data ----
    if financials:
        lines.append("")
        lines.append("## 基本面财务数据（最新报告期）")
        fin_labels = {
            "归母净利润": "归母净利润",
            "营业总收入": "营业总收入",
            "营业成本": "营业成本",
            "扣非净利润": "扣非净利润",
            "经营现金流量净额": "经营现金流净额",
            "基本每股收益": "基本每股收益",
            "每股净资产": "每股净资产",
            "净资产收益率(ROE)": "ROE(%)",
            "毛利率": "毛利率(%)",
            "销售净利率": "销售净利率(%)",
            "资产负债率": "资产负债率(%)",
            "商誉": "商誉",
        }
        for metric in _FINANCIAL_METRICS:
            val = financials.get(metric)
            label = fin_labels.get(metric, metric)
            if val is not None:
                if abs(val) >= 1e8:
                    lines.append(f"- {label}: ${val/1e8:.2f}亿")
                elif abs(val) >= 1e4:
                    lines.append(f"- {label}: ${val/1e6:.2f}百万")
                else:
                    lines.append(f"- {label}: {val:.4f}")
            else:
                lines.append(f"- {label}: N/A")

        # YoY comparison
        yoy = financials.get("yoy", {})
        if yoy and yoy.get("yoy_available"):
            lines.append("")
            lines.append("## 同比变化（与去年同期对比）")
            lines.append(f"报告期: {yoy.get('latest_quarter', 'N/A')} vs {yoy.get('prev_year_quarter', 'N/A')}")
            table = yoy.get("table", [])
            for row in table:
                metric_name = row.get("指标", "")
                latest_val = row.get(yoy.get("latest_quarter", ""), None)
                prev_val = row.get(yoy.get("prev_year_quarter", ""), None)
                if latest_val is not None and prev_val is not None and prev_val != 0 and metric_name in _FINANCIAL_METRICS:
                    try:
                        chg = (float(latest_val) / float(prev_val) - 1) * 100
                        lines.append(f"- {metric_name}: 本期={latest_val:.4f}, 去年同期={prev_val:.4f}, YoY={chg:+.2f}%")
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass

        # Recent quarters table
        recent = financials.get("recent_table", {})
        if recent and recent.get("quarters"):
            lines.append("")
            lines.append("## 近四个季度财务数据对比")
            qs = recent["quarters"]
            header = "指标".ljust(14)
            for q in qs:
                header += f" | {q}"
            lines.append(header)
            lines.append("-" * len(header))
            for metric in _FINANCIAL_METRICS:
                if metric in recent:
                    row_str = metric.ljust(14)
                    for q in qs:
                        v = recent[metric].get(q)
                        if v is not None:
                            if abs(v) >= 1e8:
                                row_str += f" | {v/1e8:.2f}亿"
                            else:
                                row_str += f" | {v:.4f}"
                        else:
                            row_str += f" | N/A"
                    lines.append(row_str)

    lines.append("")
    lines.append("## 价格走势分析")
    lines.append(f"- 今日涨幅: {signals.get('daily_return', 'N/A')}%")
    lines.append(f"- 振幅: {signals.get('daily_range_pct', 'N/A')}%")
    lines.append(f"- K线实体: {signals.get('body_pct', 'N/A')}%")
    lines.append(f"- 上影线: {signals.get('upper_shadow_pct', 'N/A')}%")
    lines.append(f"- 下影线: {signals.get('lower_shadow_pct', 'N/A')}%")
    lines.append(f"- 连续上涨: {signals.get('consecutive_up', 0)}天 | 连续下跌: {signals.get('consecutive_down', 0)}天")
    lines.append(f"- 近5日涨跌: {signals.get('pct_5d', 'N/A')}% | 近20日涨跌: {signals.get('pct_20d', 'N/A')}%")
    lines.append(f"- 5日波动率: {signals.get('volatility_5d', 'N/A')}% | 20日波动率: {signals.get('volatility_20d', 'N/A')}%")
    lines.append(f"- 今日跳空: {signals.get('latest_gap_pct', 'N/A')}%")
    lines.append(f"- 近10日缺口: 向上{signals.get('recent_gap_up_count', 0)}个 | 向下{signals.get('recent_gap_down_count', 0)}个")

    lines.append("")
    lines.append("## 价格区间位置")
    lines.append(f"- 20日最高: {signals.get('high_20d', 'N/A')} | 20日最低: {signals.get('low_20d', 'N/A')}")
    lines.append(f"- 当前在20日区间位置: {signals.get('price_pos_20d', 'N/A')}%")
    lines.append(f"- 60日最高: {signals.get('high_60d', 'N/A')} | 60日最低: {signals.get('low_60d', 'N/A')}")
    lines.append(f"- 当前在60日区间位置: {signals.get('price_pos_60d', 'N/A')}%")

    lines.append("")
    lines.append("## 均线+布林带")
    lines.append(f"- MA20: {signals.get('ma20', 'N/A')} | MA60: {signals.get('ma60', 'N/A')}")
    lines.append(f"- 布林上轨: {signals.get('bb_upper', 'N/A')} | 布林下轨: {signals.get('bb_lower', 'N/A')}")
    lines.append(f"- ATR(14): {signals.get('atr14', 'N/A')}")

    lines.append("")
    lines.append("## 量价关系")
    lines.append(f"- 今日成交量: {signals.get('today_volume', 'N/A')}")
    lines.append(f"- 5日均量: {signals.get('vol_ma5', 'N/A')} | 20日均量: {signals.get('vol_ma20', 'N/A')}")
    lines.append(f"- 量比(相对20日): {signals.get('vol_ratio', 'N/A')}")
    lines.append(f"- 近5日量价形态: {signals.get('vol_price_pattern', 'N/A')}")

    lines.append("")
    lines.append("## 近期K线走势（最近10个交易日）")
    lines.append(kline_summary)

    # ---- 形态识别数据（供AI判断W底/M顶/头肩等）----
    if pattern_data:
        lines.append("")
        lines.append(pattern_data)

    lines.append("")
    lines.append("=" * 80)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="个股数据分析 - 输出结构化数据供LLM分析")
    parser.add_argument("code", help="美股代码，如 NVDA / TSM / AMAT")
    parser.add_argument("--kline-days", type=int, default=250, help="拉取K线天数（默认250，覆盖约1年）")
    parser.add_argument("--output", help="保存数据到JSON文件")
    args = parser.parse_args()

    # Fetch data
    log.info("Fetching quote for %s ...", args.code)
    quote = fetch_realtime_quote(args.code)
    log.info("%s %s - $%.2f (%+.2f%%)", quote["code"], quote["name"], quote["price"], quote["pct_change"])

    log.info("Fetching daily K-line (%d days) ...", args.kline_days)
    df = fetch_daily_kline(args.code, args.kline_days)
    log.info("Got %d trading days of data", len(df))

    # Compute indicators
    df = compute_indicators(df)
    signals = get_latest_signals(df, quote)

    # Generate K-line summary
    kline_summary = generate_kline_summary(df)

    # Generate pattern recognition data (for AI to identify W/M/head-shoulders etc.)
    pattern_data = generate_pattern_kline_data(df)

    # Fetch financial data
    log.info("Fetching financial report data ...")
    financials = fetch_financial_data(quote["code"])
    if financials:
        log.info("Financial data loaded for %s", quote["code"])
    else:
        log.warning("No financial data available for %s", quote["code"])

    # Format and print analysis text
    analysis_text = format_analysis_text(quote, signals, kline_summary, financials, pattern_data)
    print(analysis_text)

    # Save to JSON if requested
    if args.output:
        output = {
            "code": quote["code"],
            "name": quote["name"],
            "timestamp": datetime.now().isoformat(),
            "quote": quote,
            "signals": signals,
            "kline_summary": kline_summary,
            "financials": financials,
            "analysis_text": analysis_text,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        log.info("Data saved to %s", args.output)


if __name__ == "__main__":
    main()
