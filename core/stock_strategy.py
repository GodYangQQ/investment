"""
Stock Data Analyzer
Fetches real-time + historical data for a single stock, computes comprehensive
quantitative indicators (price + volume-price relationships), and outputs structured
data for LLM analysis via opencode.

Usage:
    python stock_strategy.py 600519
    python stock_strategy.py 600519 --kline-days 180 --output data_600519.json
"""

import os
import re
import json
import argparse
import logging
from datetime import datetime
from typing import Optional

import requests
import numpy as np
import pandas as pd

try:
    import akshare as ak

    _HAS_AKSHARE = True
except ImportError:
    _HAS_AKSHARE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _normalize_code(code: str) -> tuple[str, str]:
    code = code.strip().upper()
    if code.startswith("SH") or code.startswith("SZ"):
        prefix = code[:2].lower()
        pure = code[2:]
    else:
        pure = code
        prefix = "sh" if pure.startswith("6") else "sz"
    return pure, prefix


def fetch_realtime_quote(code: str) -> dict:
    pure, prefix = _normalize_code(code)
    query = f"{prefix}{pure}"
    r = requests.get(f"https://qt.gtimg.cn/q={query}", timeout=10)
    r.encoding = "gbk"
    match = re.search(r'="(.+?)"', r.text)
    if not match:
        raise ValueError(f"Failed to fetch quote for {code}")

    fields = match.group(1).split("~")
    if len(fields) < 54:
        raise ValueError(f"Invalid quote data for {code}")

    def sf(v: str, default: float = 0.0) -> float:
        try:
            return float(v) if v else default
        except (ValueError, TypeError):
            return default

    return {
        "code": pure,
        "name": fields[1],
        "price": sf(fields[3]),
        "prev_close": sf(fields[4]),
        "open": sf(fields[5]),
        "volume": sf(fields[6]),
        "buy_volume": sf(fields[7]),
        "sell_volume": sf(fields[8]),
        "pct_change": sf(fields[32]),
        "high": sf(fields[33]),
        "low": sf(fields[34]),
        "pe_ttm": sf(fields[39]),
        "turnover": sf(fields[38]),
        "total_mv_yi": sf(fields[44]),
        "pb": sf(fields[53]),
        "振幅": sf(fields[43]),
    }


def fetch_daily_kline(code: str, days: int = 120) -> pd.DataFrame:
    pure, prefix = _normalize_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{pure},day,,,{days},qfq"
    r = requests.get(url, timeout=15)
    data = r.json()

    key = f"{prefix}{pure}"
    klines = data.get("data", {}).get(key, {}).get("qfqday", [])
    if not klines:
        klines = data.get("data", {}).get(key, {}).get("day", [])

    if not klines:
        raise ValueError(f"No K-line data for {code}")

    rows = []
    for k in klines:
        if len(k) >= 6:
            rows.append({
                "date": k[0],
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]),
            })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ---------------------------------------------------------------------------
# Financial data (fundamental analysis)
# ---------------------------------------------------------------------------

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


def _quarter_yoy(df_fin: pd.DataFrame) -> dict:
    """Compute YoY growth for the latest available quarter."""
    cols = df_fin.columns
    date_cols = [c for c in cols if str(c).isdigit() and len(str(c)) == 8]
    date_cols.sort(reverse=True)

    if len(date_cols) < 5:
        return {}

    latest = date_cols[0]
    try:
        prev_year = f"{int(latest[:4]) - 1}{latest[4:]}"
    except (ValueError, IndexError):
        return {}

    if prev_year not in date_cols:
        return {"yoy_available": False, "note": f"无去年同期({prev_year})数据"}

    result = {"yoy_available": True, "latest_quarter": latest, "prev_year_quarter": prev_year}
    recent_qs = date_cols[:5]
    # Reset index to get 指标 back as a column
    yoy_df = df_fin.reset_index()[["指标"] + recent_qs].copy()
    result["recent_quarters"] = recent_qs
    result["table"] = yoy_df.to_dict("records")
    return result


_EM_FIN_CACHE: Optional[dict] = None  # module-level cache for East Money data

def _fetch_financial_via_ths(code: str) -> Optional[dict]:
    """Fetch financial data from THS (stock_financial_abstract_new_ths).
    
    旧版 stock_financial_abstract (Sina) 已在 akshare 1.18+ 失效，
    改用同花顺新版接口。
    """
    try:
        df = ak.stock_financial_abstract_new_ths(symbol=code, indicator="按报告期")
    except Exception as e:
        log.debug("THS financial API failed for %s: %s", code, e)
        return None

    if df.empty:
        return None

    # 取最新报告期
    latest_date = df["report_date"].max()
    latest = df[df["report_date"] == latest_date].set_index("metric_name")

    def _get(metric: str) -> Optional[float]:
        if metric in latest.index:
            val = latest.loc[metric, "value"]
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
        return None

    # 映射新接口 metric_name → 旧字段名
    fundamentals = {
        "归母净利润": _get("parent_holder_net_profit"),
        "营业总收入": _get("operating_income_total"),
        "扣非净利润": _get("index_deduct_holder_net_profit"),
        "基本每股收益": _get("basic_eps"),
        "每股净资产": _get("calc_per_net_assets"),
        "净资产收益率(ROE)": _get("index_weighted_avg_roe"),
        "毛利率": _get("sale_gross_margin"),
        "销售净利率": _get("sale_net_interest_ratio"),
        "资产负债率": _get("assets_debt_ratio"),
    }

    # 经营现金流 = 每股经营现金流 × 总股本
    eps_cf = _get("index_per_operating_cash_flow_net")
    np_val = _get("parent_holder_net_profit")
    eps_val = _get("basic_eps")
    if eps_cf is not None and np_val and np_val > 0 and eps_val and eps_val > 0:
        total_shares = np_val / eps_val
        fundamentals["经营现金流量净额"] = eps_cf * total_shares

    # YoY growth
    fundamentals["营业总收入同比"] = _get("calculate_operating_income_total_yoy_growth_ratio")
    fundamentals["净利润同比增长率"] = _get("calculate_parent_holder_net_profit_yoy_growth_ratio")

    # source
    fundamentals["source"] = "ths"

    return fundamentals


def _ensure_em_fin_cache() -> None:
    """Populate module-level cache from East Money (one batch fetch)."""
    global _EM_FIN_CACHE
    if _EM_FIN_CACHE is not None:
        return
    try:
        log.info("Fetching financial data from East Money (stock_yjbb_em 20251231)...")
        df = ak.stock_yjbb_em(date="20251231")
        _EM_FIN_CACHE = {}
        for _, row in df.iterrows():
            c = str(row.get("股票代码", "")).strip()
            _EM_FIN_CACHE[c] = {
                "归母净利润": float(row.get("净利润-净利润", 0) or 0),
                "营业总收入": float(row.get("营业总收入-营业总收入", 0) or 0),
                "营业总收入同比": float(row.get("营业总收入-同比增长", 0) or 0),
                "毛利率": float(row.get("销售毛利率", 0) or 0),
                "净资产收益率(ROE)": float(row.get("净资产收益率", 0) or 0),
                "基本每股收益": float(row.get("每股收益", 0) or 0),
                "每股净资产": float(row.get("每股净资产", 0) or 0),
                "经营现金流量净额": float(row.get("每股经营现金流量", 0) or 0) * 100000000,
                "_name": str(row.get("股票简称", "")),
                "_industry": str(row.get("所处行业", "")),
            }
        log.info("East Money cache loaded: %d stocks", len(_EM_FIN_CACHE))
    except Exception as e:
        log.warning("East Money batch fetch failed: %s", e)


def fetch_financial_data(code: str) -> Optional[dict]:
    """Fetch financial report data via THS (同花顺) API."""
    if not _HAS_AKSHARE:
        log.warning("akshare not installed, skipping financial data")
        return None

    # Try THS first (richer data)
    result = _fetch_financial_via_ths(code)
    if result is not None:
        return result

    # Fallback to East Money cache
    _ensure_em_fin_cache()
    if _EM_FIN_CACHE is None or code not in _EM_FIN_CACHE:
        log.warning("No financial data for %s from either source", code)
        return None

    entry = _EM_FIN_CACHE[code]
    return {
        "归母净利润": entry.get("归母净利润"),
        "营业总收入": entry.get("营业总收入"),
        "毛利率": entry.get("毛利率"),
        "净资产收益率(ROE)": entry.get("净资产收益率(ROE)"),
        "基本每股收益": entry.get("基本每股收益"),
        "每股净资产": entry.get("每股净资产"),
        "经营现金流量净额": entry.get("经营现金流量净额"),
        "营业总收入同比": entry.get("营业总收入同比"),
        "净利润": entry.get("归母净利润"),
        "source": "eastmoney",
    }


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    volume = df["volume"]
    high = df["high"]
    low = df["low"]
    open_price = df["open"]

    # Moving averages
    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = 2 * (df["dif"] - df["dea"])

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - 100 / (1 + rs)

    # Bollinger Bands
    df["bb_mid"] = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std

    # Volume MA
    df["vol_ma5"] = volume.rolling(5).mean()
    df["vol_ma20"] = volume.rolling(20).mean()

    # ATR (14-day)
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    # Price analysis
    df["daily_return"] = close.pct_change() * 100
    df["daily_range_pct"] = (high - low) / prev_close * 100
    df["body_pct"] = (close - open_price) / prev_close * 100
    df["upper_shadow_pct"] = (high - pd.concat([close, open_price], axis=1).max(axis=1)) / prev_close * 100
    df["lower_shadow_pct"] = (pd.concat([close, open_price], axis=1).min(axis=1) - low) / prev_close * 100

    # Price position in recent ranges
    for window in [20, 60]:
        df[f"high_{window}d"] = high.rolling(window).max()
        df[f"low_{window}d"] = low.rolling(window).min()
        df[f"price_pos_{window}d"] = (
            (close - df[f"low_{window}d"])
            / (df[f"high_{window}d"] - df[f"low_{window}d"]).replace(0, np.nan)
            * 100
        )

    # Price volatility
    df["volatility_5d"] = df["daily_return"].rolling(5).std()
    df["volatility_20d"] = df["daily_return"].rolling(20).std()

    # Consecutive up/down days
    df["is_up"] = (close > close.shift(1)).astype(int)
    df["is_down"] = (close < close.shift(1)).astype(int)

    # Gap analysis
    df["gap_pct"] = (open_price - prev_close) / prev_close * 100
    df["has_gap_up"] = df["gap_pct"] > 0.5
    df["has_gap_down"] = df["gap_pct"] < -0.5

    # Volume-price relationship
    df["vol_up"] = volume.where(close > close.shift(1), 0)
    df["vol_down"] = volume.where(close < close.shift(1), 0)
    df["vol_up_ma5"] = df["vol_up"].rolling(5).mean()
    df["vol_down_ma5"] = df["vol_down"].rolling(5).mean()
    df["vol_up_ma10"] = df["vol_up"].rolling(10).mean()
    df["vol_down_ma10"] = df["vol_down"].rolling(10).mean()

    # Volume trend
    df["vol_trend_5d"] = volume.rolling(5).mean().pct_change(5) * 100
    df["vol_trend_10d"] = volume.rolling(10).mean().pct_change(10) * 100

    # Volume-price correlation
    df["price_vol_corr_10d"] = close.rolling(10).corr(volume)
    df["price_vol_corr_20d"] = close.rolling(20).corr(volume)

    # OBV
    obv = [0]
    for i in range(1, len(df)):
        if close.iloc[i] > close.iloc[i - 1]:
            obv.append(obv[-1] + volume.iloc[i])
        elif close.iloc[i] < close.iloc[i - 1]:
            obv.append(obv[-1] - volume.iloc[i])
        else:
            obv.append(obv[-1])
    df["obv"] = obv
    df["obv_ma10"] = df["obv"].rolling(10).mean()
    df["obv_ma20"] = df["obv"].rolling(20).mean()

    # VWAP approximation
    df["typical_price"] = (high + low + close) / 3
    df["tp_vol"] = df["typical_price"] * volume
    df["vwap_5d"] = df["tp_vol"].rolling(5).sum() / volume.rolling(5).sum()
    df["vwap_20d"] = df["tp_vol"].rolling(20).sum() / volume.rolling(20).sum()

    # Chaikin Money Flow
    mf_multiplier = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    mf_volume = mf_multiplier * volume
    df["cmf_20d"] = mf_volume.rolling(20).sum() / volume.rolling(20).sum()

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
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    last_idx = len(df) - 1

    price = latest["close"]

    def rnd(val, decimals=2):
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return None
        return round(float(val), decimals)

    signals = {
        "current_price": rnd(price),
        "ma5": rnd(latest["ma5"]),
        "ma10": rnd(latest["ma10"]),
        "ma20": rnd(latest["ma20"]),
        "ma60": rnd(latest["ma60"]),
        "dif": rnd(latest["dif"], 4),
        "dea": rnd(latest["dea"], 4),
        "macd_hist": rnd(latest["macd_hist"], 4),
        "rsi14": rnd(latest["rsi14"]),
        "bb_upper": rnd(latest["bb_upper"]),
        "bb_mid": rnd(latest["bb_mid"]),
        "bb_lower": rnd(latest["bb_lower"]),
        "atr14": rnd(latest["atr14"]),
        "vol_ratio": rnd(latest["volume"] / latest["vol_ma20"]) if latest["vol_ma20"] > 0 else None,
    }

    # Trend
    signals["price_vs_ma5"] = "上方" if price > (signals["ma5"] or 0) else "下方"
    signals["price_vs_ma20"] = "上方" if price > (signals["ma20"] or 0) else "下方"
    signals["ma5_vs_ma20"] = "金叉" if (signals["ma5"] or 0) > (signals["ma20"] or 0) else "死叉"
    signals["macd_cross"] = "金叉" if latest["dif"] > latest["dea"] and prev["dif"] <= prev["dea"] else (
        "死叉" if latest["dif"] < latest["dea"] and prev["dif"] >= prev["dea"] else "无交叉"
    )
    signals["macd_trend"] = "多头" if latest["dif"] > latest["dea"] else "空头"

    # RSI zone
    rsi = signals["rsi14"]
    if rsi is not None:
        if rsi > 80:
            signals["rsi_zone"] = "超买"
        elif rsi > 60:
            signals["rsi_zone"] = "强势"
        elif rsi > 40:
            signals["rsi_zone"] = "中性"
        elif rsi > 20:
            signals["rsi_zone"] = "弱势"
        else:
            signals["rsi_zone"] = "超卖"

    # Bollinger
    bb_upper = signals["bb_upper"]
    bb_lower = signals["bb_lower"]
    if bb_upper and bb_lower and (bb_upper - bb_lower) > 0:
        bb_pct = (price - bb_lower) / (bb_upper - bb_lower) * 100
        signals["bb_position"] = rnd(bb_pct, 1)
        if bb_pct > 90:
            signals["bb_zone"] = "上轨附近"
        elif bb_pct < 10:
            signals["bb_zone"] = "下轨附近"
        else:
            signals["bb_zone"] = "中轨区域"

    # Performance
    if len(df) >= 6:
        signals["pct_5d"] = rnd((price / df.iloc[-6]["close"] - 1) * 100)
    if len(df) >= 21:
        signals["pct_20d"] = rnd((price / df.iloc[-21]["close"] - 1) * 100)

    # Price signals
    signals["daily_return"] = rnd(latest["daily_return"])
    signals["daily_range_pct"] = rnd(latest["daily_range_pct"])
    signals["body_pct"] = rnd(latest["body_pct"])
    signals["upper_shadow_pct"] = rnd(latest["upper_shadow_pct"])
    signals["lower_shadow_pct"] = rnd(latest["lower_shadow_pct"])

    for w in [20, 60]:
        signals[f"price_pos_{w}d"] = rnd(latest.get(f"price_pos_{w}d"), 1)

    signals["volatility_5d"] = rnd(latest["volatility_5d"])
    signals["volatility_20d"] = rnd(latest["volatility_20d"])
    signals["consecutive_up"] = _count_consecutive(df["is_up"], last_idx, 1)
    signals["consecutive_down"] = _count_consecutive(df["is_down"], last_idx, 1)

    recent_gaps = df.tail(10)
    signals["recent_gap_up_count"] = int(recent_gaps["has_gap_up"].sum())
    signals["recent_gap_down_count"] = int(recent_gaps["has_gap_down"].sum())
    signals["latest_gap_pct"] = rnd(latest["gap_pct"])

    signals["high_20d"] = rnd(latest["high_20d"])
    signals["low_20d"] = rnd(latest["low_20d"])
    signals["high_60d"] = rnd(latest["high_60d"])
    signals["low_60d"] = rnd(latest["low_60d"])

    # Volume-price signals
    signals["today_volume"] = int(latest["volume"])
    signals["vol_ma5"] = int(latest["vol_ma5"]) if not np.isnan(latest["vol_ma5"]) else None
    signals["vol_ma20"] = int(latest["vol_ma20"]) if not np.isnan(latest["vol_ma20"]) else None
    signals["vol_trend_5d"] = rnd(latest["vol_trend_5d"], 1)
    signals["vol_trend_10d"] = rnd(latest["vol_trend_10d"], 1)

    vol_up_5 = latest["vol_up_ma5"]
    vol_down_5 = latest["vol_down_ma5"]
    signals["vol_up_down_ratio_5d"] = rnd(vol_up_5 / vol_down_5) if vol_down_5 > 0 else None

    vol_up_10 = latest["vol_up_ma10"]
    vol_down_10 = latest["vol_down_ma10"]
    signals["vol_up_down_ratio_10d"] = rnd(vol_up_10 / vol_down_10) if vol_down_10 > 0 else None

    signals["price_vol_corr_10d"] = rnd(latest["price_vol_corr_10d"], 3)
    signals["price_vol_corr_20d"] = rnd(latest["price_vol_corr_20d"], 3)

    signals["obv_trend"] = None
    if not np.isnan(latest["obv_ma10"]) and not np.isnan(latest["obv_ma20"]):
        signals["obv_trend"] = "多头" if latest["obv_ma10"] > latest["obv_ma20"] else "空头"

    signals["price_vs_vwap_5d"] = None
    signals["price_vs_vwap_20d"] = None
    if not np.isnan(latest["vwap_5d"]):
        signals["price_vs_vwap_5d"] = "上方" if price > latest["vwap_5d"] else "下方"
    if not np.isnan(latest["vwap_20d"]):
        signals["price_vs_vwap_20d"] = "上方" if price > latest["vwap_20d"] else "下方"

    signals["cmf_20d"] = rnd(latest["cmf_20d"], 4)
    if signals["cmf_20d"] is not None:
        signals["cmf_signal"] = "买入压力" if signals["cmf_20d"] > 0.05 else (
            "卖出压力" if signals["cmf_20d"] < -0.05 else "中性"
        )
    else:
        signals["cmf_signal"] = None

    # Volume-price pattern
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

    # Divergence
    corr10 = signals["price_vol_corr_10d"]
    if corr10 is not None:
        if corr10 < -0.5 and price > (signals["ma20"] or 0):
            signals["divergence"] = "顶背离预警(价升量降)"
        elif corr10 > 0.5 and price < (signals["ma20"] or 0):
            signals["divergence"] = "底背离信号(价降量升)"
        else:
            signals["divergence"] = "无明显背离"
    else:
        signals["divergence"] = None

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
    lines.append(f"- 当前价格: {quote['price']:.2f} 元")
    lines.append(f"- 今日涨跌: {quote['pct_change']:+.2f}%")
    lines.append(f"- PE-TTM: {quote['pe_ttm']:.2f}")
    lines.append(f"- PB: {quote['pb']:.2f}")
    lines.append(f"- 总市值: {quote['total_mv_yi']:.1f} 亿元")
    lines.append(f"- 换手率: {quote['turnover']:.2f}%")

    # ---- Financial / Fundamental data ----
    if financials:
        lines.append("")
        lines.append("## 基本面财务数据（最新报告期）")
        fin_labels = {
            "归母净利润": "归母净利润(元)",
            "营业总收入": "营业总收入(元)",
            "营业成本": "营业成本(元)",
            "扣非净利润": "扣非净利润(元)",
            "经营现金流量净额": "经营现金流净额(元)",
            "基本每股收益": "基本每股收益(元)",
            "每股净资产": "每股净资产(元)",
            "净资产收益率(ROE)": "ROE(%)",
            "毛利率": "毛利率(%)",
            "销售净利率": "销售净利率(%)",
            "资产负债率": "资产负债率(%)",
            "商誉": "商誉(元)",
        }
        for metric in _FINANCIAL_METRICS:
            val = financials.get(metric)
            label = fin_labels.get(metric, metric)
            if val is not None:
                if abs(val) >= 1e8:
                    lines.append(f"- {label}: {val/1e8:.2f}亿")
                elif abs(val) >= 1e4:
                    lines.append(f"- {label}: {val/1e4:.2f}万")
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
    lines.append("## 均线系统")
    lines.append(f"- MA5: {signals.get('ma5', 'N/A')} | MA10: {signals.get('ma10', 'N/A')} | MA20: {signals.get('ma20', 'N/A')} | MA60: {signals.get('ma60', 'N/A')}")
    lines.append(f"- 价格位置: MA5{signals.get('price_vs_ma5', 'N/A')} | MA20{signals.get('price_vs_ma20', 'N/A')}")
    lines.append(f"- MA5 vs MA20: {signals.get('ma5_vs_ma20', 'N/A')}")

    lines.append("")
    lines.append("## 量价关系（重点）")
    lines.append(f"- 今日成交量: {signals.get('today_volume', 'N/A')}")
    lines.append(f"- 5日均量: {signals.get('vol_ma5', 'N/A')} | 20日均量: {signals.get('vol_ma20', 'N/A')}")
    lines.append(f"- 量比(相对20日): {signals.get('vol_ratio', 'N/A')}")
    lines.append(f"- 5日量能趋势: {signals.get('vol_trend_5d', 'N/A')}% | 10日量能趋势: {signals.get('vol_trend_10d', 'N/A')}%")
    lines.append(f"- 上涨日均量/下跌日均量(5日): {signals.get('vol_up_down_ratio_5d', 'N/A')}")
    lines.append(f"- 上涨日均量/下跌日均量(10日): {signals.get('vol_up_down_ratio_10d', 'N/A')}")
    lines.append(f"- 量价相关系数(10日): {signals.get('price_vol_corr_10d', 'N/A')}")
    lines.append(f"- 量价相关系数(20日): {signals.get('price_vol_corr_20d', 'N/A')}")
    lines.append(f"- 近5日量价形态: {signals.get('vol_price_pattern', 'N/A')}")
    lines.append(f"- 量价背离: {signals.get('divergence', 'N/A')}")

    lines.append("")
    lines.append("## 资金流向指标")
    lines.append(f"- OBV趋势: {signals.get('obv_trend', 'N/A')}")
    lines.append(f"- VWAP对比: 价格在5日VWAP{signals.get('price_vs_vwap_5d', 'N/A')} | 在20日VWAP{signals.get('price_vs_vwap_20d', 'N/A')}")
    lines.append(f"- Chaikin资金流(20日): {signals.get('cmf_20d', 'N/A')} | 信号: {signals.get('cmf_signal', 'N/A')}")

    lines.append("")
    lines.append("## MACD & RSI & 布林带")
    lines.append(f"- MACD: DIF={signals.get('dif', 'N/A')} | DEA={signals.get('dea', 'N/A')} | 柱状={signals.get('macd_hist', 'N/A')}")
    lines.append(f"- MACD趋势: {signals.get('macd_trend', 'N/A')} | 交叉: {signals.get('macd_cross', 'N/A')}")
    lines.append(f"- RSI(14): {signals.get('rsi14', 'N/A')} ({signals.get('rsi_zone', 'N/A')})")
    lines.append(f"- 布林带: 上轨={signals.get('bb_upper', 'N/A')} | 中轨={signals.get('bb_mid', 'N/A')} | 下轨={signals.get('bb_lower', 'N/A')}")
    lines.append(f"- 布林带位置: {signals.get('bb_position', 'N/A')}% ({signals.get('bb_zone', 'N/A')})")
    lines.append(f"- ATR(14): {signals.get('atr14', 'N/A')}")

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
    parser.add_argument("code", help="股票代码，如 600519 或 sh600519")
    parser.add_argument("--kline-days", type=int, default=250, help="拉取K线天数（默认250，覆盖约1年）")
    parser.add_argument("--output", help="保存数据到JSON文件")
    args = parser.parse_args()

    # Fetch data
    log.info("Fetching real-time quote for %s ...", args.code)
    quote = fetch_realtime_quote(args.code)
    log.info("%s %s - 当前价格: %.2f (%+.2f%%)", quote["code"], quote["name"], quote["price"], quote["pct_change"])

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
