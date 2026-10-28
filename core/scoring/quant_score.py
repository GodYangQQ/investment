#!/usr/bin/env python3
"""
量化多因子打分引擎（零未来函数）
基于 stock_strategy.py 已计算的技术指标，输出0-100的量化评分 + 成本修正

用法：
    # 单只股票打分（无成本）
    python quant_score.py NVDA

    # 单只股票打分（带成本+持仓）
    python quant_score.py NVDA --cost 95.50 --shares 500 --buy-date 2026-05-10

    # 批量打分（从CSV读取自选池）
    python quant_score.py --pool pool.csv

    # 输出JSON
    python quant_score.py NVDA --cost 95.50 --output score.json

CSV格式（pool.csv）：
    代码,名称,层级,成本价,持仓量,买入日期
    NVDA,NVIDIA,核心,120.00,50,2026-05-15
    AMD,AMD,观察,,,        （无持仓可不填成本/量/日期）
"""

import argparse
import json
import logging
import sys
import os
from datetime import datetime, date

# 确保可以 import 同目录下的 core/ 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

# 复用 stock_strategy.py 的数据获取和指标计算
from core.strategies.stock_strategy import (
    fetch_realtime_quote,
    fetch_daily_kline,
    compute_indicators,
    get_latest_signals,
)
from core.datasets.us_data import fetch_financial_data, fetch_index_quote, fmt_market_cap, _HAS_YFINANCE
from core.datasets.universe import GPU_AI_CORE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 量化多因子打分（完全机器计算，零LLM参与）
# 精简版：趋势+位置 35分 | 量价+Price Action 30分 | 波动率 15分 | 斐波那契/结构 20分
# ═══════════════════════════════════════════════════════════════════════════

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

def score_trend_position(signals: dict) -> tuple[float, str]:
    """趋势+位置因子（满分35）。MA20/MA60的价距 + 60日高回撤 + 20日低支撑。"""
    price = signals.get("current_price", 0)
    ma20 = signals.get("ma20") or 0
    ma60 = signals.get("ma60") or 0
    high_60d = signals.get("high_60d")
    low_20d = signals.get("low_20d")

    if not ma20 or not ma60 or price <= 0:
        return 10.0, "数据不全"

    detail_parts = []
    total = 0.0

    # ---- A. 均线位置 (0-15) ----
    gap20 = (price / ma20 - 1) * 100
    gap60 = (price / ma60 - 1) * 100

    # price vs MA20 (0-8)：价在MA20上方3-15%最佳
    if -3 <= gap20 <= 3:
        ma20_score = 8.0 - abs(gap20) / 3 * 3.0
    elif gap20 > 3:
        ma20_score = max(2.0, 5.0 - (gap20 - 3) / 12 * 3.0)
    else:
        ma20_score = max(1.0, 5.0 + gap20 / 5 * 4.0)

    # price vs MA60 (0-7)：价在MA60上方最好，但不过分远离
    if gap60 > 15:
        ma60_score = 7.0
    elif gap60 > 5:
        ma60_score = 5.0 + (gap60 - 5) / 10 * 2.0
    elif gap60 > -3:
        ma60_score = 3.0 + (gap60 + 3) / 8 * 2.0
    elif gap60 > -10:
        ma60_score = 1.0 + (gap60 + 10) / 7 * 2.0
    else:
        ma60_score = 0.0

    total += ma20_score + ma60_score
    detail_parts.append(f"价距MA20={gap20:.1f}%, 距MA60={gap60:.1f}%")

    # ---- B. 位置评估 (0-20) ----
    if high_60d and high_60d > 0:
        drawdown = (high_60d - price) / high_60d * 100
        if drawdown <= 3:
            dd_score = 4.0
        elif drawdown <= 8:
            dd_score = 4.0 + (drawdown - 3) / 5 * 11.0  # 3~8%: 4→15
        elif drawdown <= 20:
            dd_score = 15.0 + (drawdown - 8) / 12 * 5.0  # 8~20%: 15→20（最佳）
        elif drawdown <= 30:
            dd_score = 20.0 - (drawdown - 20) / 10 * 12.0
        elif drawdown <= 45:
            dd_score = 8.0 - (drawdown - 30) / 15 * 5.0
        else:
            dd_score = max(0.0, 3.0 - (drawdown - 45) / 10 * 3.0)
        total += dd_score
        detail_parts.append(f"距60日高回撤{drawdown:.1f}%")

        # 距20日低补充分
        if low_20d and low_20d > 0:
            above_low = (price / low_20d - 1) * 100
            if 5 <= above_low <= 25:
                low_bonus = 2.0 - abs(above_low - 15) / 10 * 1.5
            elif above_low < 5:
                low_bonus = 1.0 + above_low / 5 * 1.0
            else:
                low_bonus = max(0.0, 0.5 - (above_low - 25) / 20)
            total += low_bonus
            detail_parts.append(f"距20日低{above_low:.1f}%")

    total = round(_clamp(total, 0, 35), 1)
    return total, "; ".join(detail_parts)


def score_volume_price_action(signals: dict) -> tuple[float, str]:
    """量价+Price Action因子（满分30）。量比 + K线形态 + 量能方向 + 量价形态。"""
    price = signals.get("current_price", 0)
    vol_ma5 = signals.get("vol_ma5") or 0
    vol_ma20 = signals.get("vol_ma20") or 0
    body_pct = signals.get("body_pct") or 0
    upper_shadow = signals.get("upper_shadow_pct") or 0
    lower_shadow = signals.get("lower_shadow_pct") or 0
    daily_return = signals.get("daily_return") or 0
    pattern = signals.get("vol_price_pattern", "")
    cons_up = signals.get("consecutive_up", 0)
    cons_down = signals.get("consecutive_down", 0)

    total = 0.0
    parts = []

    # ---- 1) 量比 (0-12) ----
    if vol_ma20 > 0:
        vol_ratio = vol_ma5 / vol_ma20
        if 1.0 <= vol_ratio <= 2.5:
            vr = 6.0 + (vol_ratio - 1.0) / 1.5 * 6.0  # 1~2.5: 6→12
        elif 0.7 <= vol_ratio < 1.0:
            vr = 2.0 + (vol_ratio - 0.7) / 0.3 * 4.0  # 0.7~1: 2→6
        elif vol_ratio > 2.5:
            vr = max(3.0, 12.0 - (vol_ratio - 2.5) / 3.0 * 9.0)
        else:
            vr = max(0.0, vol_ratio / 0.7 * 2.0)
        parts.append(f"量比{vol_ratio:.2f}")
    else:
        vr = 5.0
    total += vr

    # ---- 2) K线形态 (0-10) ----
    # 实体方向 + 影线质量
    candle_score = 5.0

    # 阳线加分：小阳+2，中阳+3，大阳谨慎+1
    if body_pct > 0 and daily_return > 0:
        if 0 < body_pct <= 3:
            candle_score += 2.0
        elif 3 < body_pct <= 7:
            candle_score += 3.0
        elif body_pct > 7:
            candle_score += 1.0  # 暴涨追高风险，不如中阳
    elif daily_return < 0:
        if body_pct > 5:
            candle_score -= 3.0  # 大阴线
        elif body_pct > 2:
            candle_score -= 1.5  # 中阴线

    # 下影线长=买盘支撑 (0~+3)
    if lower_shadow and lower_shadow > 0 and body_pct > 0:
        shadow_ratio = lower_shadow / max(abs(body_pct), 0.01)
        if shadow_ratio > 2:
            candle_score += 3.0  # 长下影+阳线=强支撑
            parts.append("长下影线(支撑)")
        elif shadow_ratio > 1:
            candle_score += 1.5

    # 上影线长=抛压 (-2~0)
    if upper_shadow and upper_shadow > 0:
        if upper_shadow > 3 and daily_return > 0:
            candle_score -= 2.0  # 阳线上影过长=上方抛压
            parts.append("长上影线(抛压)")
        elif upper_shadow > 3:
            candle_score -= 1.0

    candle_score = _clamp(candle_score, 0, 10)
    total += candle_score

    # 描述K线形态
    if body_pct > 0:
        direction = "阳"
    elif body_pct < 0:
        direction = "阴"
    else:
        direction = "十字"
    parts.append(f"{direction}线{abs(body_pct):.1f}%")

    # ---- 3) 量价形态 (0-5) ----
    pattern_scores = {
        "放量上涨": 5.0, "缩量上涨": 2.0,
        "放量下跌": 0.0, "缩量下跌": 3.0,
        "量价平稳": 2.5,
    }
    tp = pattern_scores.get(pattern, 2.5)
    total += tp
    if pattern:
        parts.append(pattern)

    # ---- 4) 结构印象 (0-3) ----
    # 连阴后止跌 +2，连阳追高 -1
    if cons_down >= 3:
        total += min(cons_down, 5) * 0.4
        parts.append(f"连阴{cons_down}天(止跌)")
    elif cons_up >= 4:
        total -= min(cons_up - 3, 3) * 0.35
        parts.append(f"连阳{cons_up}天(追高)")

    total = round(_clamp(total, 0, 30), 1)
    return total, " | ".join(parts)


def score_volatility(signals: dict) -> tuple[float, str]:
    """波动率因子（满分15）。ATR(14)/价格 + 波动率环境。"""
    atr = signals.get("atr14")
    price = signals.get("current_price", 0)
    vol5 = signals.get("volatility_5d")

    if atr is None or atr <= 0 or price <= 0:
        return 7.5, "ATR数据不足"

    atr_pct = (atr / price) * 100

    if atr_pct <= 1:
        atr_score = 3.0 + atr_pct / 1.0 * 4.0
    elif atr_pct <= 2:
        atr_score = 7.0 + (atr_pct - 1) / 1.0 * 6.0  # 1~2: 7→13
    elif atr_pct <= 5:
        atr_score = 13.0 + (atr_pct - 2) / 3.0 * 2.0  # 2~5: 最佳
    elif atr_pct <= 8:
        atr_score = 15.0 - (atr_pct - 5) / 3.0 * 7.0
    elif atr_pct <= 15:
        atr_score = 8.0 - (atr_pct - 8) / 7.0 * 5.0
    else:
        atr_score = max(0.0, 3.0 - (atr_pct - 15) / 20 * 3.0)

    vol5_adj = 0.0
    if vol5 is not None and vol5 > 0:
        if 1.0 <= vol5 <= 3.0:
            vol5_adj = 1.0
        elif vol5 > 5.0:
            vol5_adj = -1.0

    total = _clamp(atr_score + vol5_adj, 0, 15)
    total = round(total, 1)
    return total, f"ATR/价格={atr_pct:.2f}%"


def score_fib_structure(signals: dict, df: pd.DataFrame | None = None) -> tuple[float, str]:
    """斐波那契+市场结构因子（满分20）。回调位 + 波峰波谷结构 + 布林极端。"""
    price = signals.get("current_price", 0)
    if not price:
        return 5.0, "价格缺失"

    total = 0.0
    parts = []

    # ---- 1) 斐波那契回调评估 (0-8) ----
    high_60d = signals.get("high_60d")
    low_60d = signals.get("low_60d")

    if high_60d and low_60d and high_60d > low_60d:
        swing_range = high_60d - low_60d
        # 0.382 / 0.5 / 0.618 / 0.786
        fib_levels = {
            0.382: high_60d - swing_range * 0.382,
            0.5: high_60d - swing_range * 0.5,
            0.618: high_60d - swing_range * 0.618,
            0.786: high_60d - swing_range * 0.786,
        }

        # 找到价格最接近的斐波那契位
        closest_level = None
        closest_dist = float("inf")
        for lvl_ratio, lvl_price in fib_levels.items():
            dist = abs(price - lvl_price) / price * 100
            if dist < closest_dist:
                closest_dist = dist
                closest_level = lvl_ratio

        if closest_level is not None:
            if closest_dist <= 2:
                # 在重要斐波那契位附近
                fib_score = 7.0
                parts.append(f"价格在{closest_level:.0%}回调位附近(距{closest_dist:.0f}%)")
            elif closest_dist <= 5:
                fib_score = 5.0
                parts.append(f"接近{closest_level:.0%}回调位(距{closest_dist:.0f}%)")
            elif closest_dist <= 10:
                fib_score = 3.0
                parts.append(f"偏向{closest_level:.0%}回调位")
            else:
                fib_score = 1.0
            total += fib_score
    else:
        total += 2.0  # 无足够数据，给基础分

    # ---- 2) 市场结构 (0-7) ----
    if df is not None and len(df) >= 40:
        from scipy.signal import argrelextrema
        closes = df["close"].values
        n = len(closes)

        # 检测最近40日的波峰波谷
        order = max(5, min(10, n // 8))
        peaks = argrelextrema(closes[-40:], np.greater, order=order)[0]
        valleys = argrelextrema(closes[-40:], np.less, order=order)[0]

        struct_score = 3.5  # 基础分

        if len(peaks) >= 2 and len(valleys) >= 2:
            recent_peaks_vals = [closes[n - 40 + p] for p in peaks[-3:]] if len(peaks) >= 3 else [closes[n - 40 + p] for p in peaks]
            recent_valleys_vals = [closes[n - 40 + v] for v in valleys[-3:]] if len(valleys) >= 3 else [closes[n - 40 + v] for v in valleys]

            # Higher High / Higher Low = 上升结构
            if len(recent_peaks_vals) >= 2 and recent_peaks_vals[-1] > recent_peaks_vals[-2]:
                struct_score += 2.0
                parts.append("更高的高点(HH)")
            elif len(recent_peaks_vals) >= 2:
                struct_score -= 1.5
                parts.append("更低的高点(LH)")

            if len(recent_valleys_vals) >= 2 and recent_valleys_vals[-1] > recent_valleys_vals[-2]:
                struct_score += 1.5
                parts.append("更高的低点(HL)")
            elif len(recent_valleys_vals) >= 2:
                struct_score -= 2.0
                parts.append("更低的低点(LL)")

        struct_score = _clamp(struct_score, 0, 7)
        total += struct_score
    else:
        total += 3.0  # 无结构数据，给基础分

    # ---- 3) 布林极端位 (0-5) ----
    bb_upper = signals.get("bb_upper")
    bb_lower = signals.get("bb_lower")
    if bb_upper and bb_lower and bb_upper > bb_lower:
        bb_pos = (price - bb_lower) / (bb_upper - bb_lower) * 100
        if bb_pos < 15:
            # 下轨附近=潜在支撑=反弹概率高
            total += 4.0
            parts.append(f"布林下轨({bb_pos:.0f}%)")
        elif bb_pos < 30:
            total += 2.0
            parts.append(f"布林中下轨({bb_pos:.0f}%)")
        elif bb_pos > 85:
            total -= 1.0
            parts.append(f"布林上轨({bb_pos:.0f}%)-追高")
    else:
        total += 1.0

    if not parts:
        parts.append("数据不足")

    total = round(_clamp(total, 0, 20), 1)
    return total, " | ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# 动量因子（需更长K线，替代AI手工计算）
# ═══════════════════════════════════════════════════════════════════════════

def calc_momentum_factors(df: pd.DataFrame, code: str = "") -> dict:
    """
    计算量化动量因子（基于历史K线数据，零AI介入）。
    需要至少250日K线数据以获得1年动量。
    """
    closes = df["close"].values
    n = len(closes)

    result = {
        "data_available": False,
        "one_year_pct": None,
        "three_month_pct": None,
        "twenty_day_pct": None,
        "momentum_consistency": 0,
        "long_short_ratio": None,
        "momentum_total": 0,
        "momentum_grade": "数据不足",
    }

    if n < 21:
        return result

    latest = closes[-1]

    # 20日涨幅
    if n >= 21:
        p20 = (latest / closes[-21] - 1) * 100
        result["twenty_day_pct"] = round(p20, 2)

    # 3月涨幅（约63个交易日）
    if n >= 64:
        p3m = (latest / closes[-64] - 1) * 100
        result["three_month_pct"] = round(p3m, 2)

    # 1年涨幅（约250个交易日）
    if n >= 251:
        p1y = (latest / closes[-251] - 1) * 100
        result["one_year_pct"] = round(p1y, 2)

    # 动量一致性（三个周期全正=3分）
    consistency = 0
    for pct in [result["one_year_pct"], result["three_month_pct"], result["twenty_day_pct"]]:
        if pct is not None and pct > 0:
            consistency += 1
    result["momentum_consistency"] = consistency

    # 长动比（1年涨幅÷3月涨幅）
    if result["one_year_pct"] is not None and result["three_month_pct"] is not None:
        if result["three_month_pct"] > 0:
            lsr = result["one_year_pct"] / result["three_month_pct"]
            result["long_short_ratio"] = round(lsr, 2)
        elif result["three_month_pct"] < 0 and result["one_year_pct"] > 0:
            result["long_short_ratio"] = 999  # 长期涨短期跌，高质量回调

    # GPU/AI 核心（SOXX 内部分层，避免全池都加分）
    is_ai_chain = str(code).upper() in GPU_AI_CORE

    # 动量总分（满分8=一致性3+AI链2+长动比2+20日趋势1）
    score = 0
    score += consistency  # 0-3
    score += 2 if is_ai_chain else 0  # 0-2
    if result["long_short_ratio"] is not None:
        if result["long_short_ratio"] > 5:
            score += 2
        elif result["long_short_ratio"] >= 3:
            score += 1
    if result["twenty_day_pct"] is not None and result["twenty_day_pct"] > 0:
        score += 1

    result["is_ai_chain"] = is_ai_chain
    result["momentum_total"] = score

    if score >= 6:
        result["momentum_grade"] = f"强动量({score}/8)"
    elif score >= 4:
        result["momentum_grade"] = f"中等动量({score}/8)"
    else:
        result["momentum_grade"] = f"弱动量({score}/8)"

    result["data_available"] = True
    return result


def calc_fundamental_auto_score(quote: dict, financials: dict | None) -> dict:
    """
    基本面自动初评（1-5分），基于可获取的财务数据。
    仅供AI参考修正，不替代AI最终判断。
    """
    result = {
        "score": None,
        "data_available": False,
        "breakdown": [],
    }

    if financials is None:
        result["breakdown"].append("无财务数据")
        return result

    score = 0
    items = []

    # ROE
    roe = financials.get("净资产收益率(ROE)")
    if roe is not None:
        if roe > 20:
            score += 1.0
            items.append(f"ROE={roe:.1f}%(>20%) +1")
        elif roe > 10:
            score += 0.5
            items.append(f"ROE={roe:.1f}%(>10%) +0.5")
        elif roe < 5:
            score -= 0.5
            items.append(f"ROE={roe:.1f}%(<5%) -0.5")

    # 毛利率
    gross = financials.get("毛利率")
    if gross is not None:
        if gross > 40:
            score += 0.5
            items.append(f"毛利率={gross:.1f}%(>40%) +0.5")
        elif gross < 10:
            score -= 1.0
            items.append(f"毛利率={gross:.1f}%(<10%) -1")
        elif gross < 0:
            score = min(score, 0)
            items.append(f"毛利率为负→≤1分")

    # PE
    pe = quote.get("pe_ttm", 0)
    if pe > 0:
        if pe < 15:
            score += 0.5
            items.append(f"PE={pe:.1f}(<15) +0.5")
        elif pe > 80:
            score -= 0.5
            items.append(f"PE={pe:.1f}(>80) -0.5")

    # PEG = PE / 净利增速
    net_profit_yoy = financials.get("净利润同比增长率") or financials.get("归母净利润同比增长率")
    if pe > 0 and net_profit_yoy is not None and net_profit_yoy > 0:
        peg = pe / net_profit_yoy
        if peg < 0.5:
            score += 0.5
            items.append(f"PEG={peg:.2f}(<0.5,极度低估) +0.5")
        elif peg < 1.0:
            score += 0.3
            items.append(f"PEG={peg:.2f}(<1,低估) +0.3")
        elif peg > 3.0:
            score -= 0.5
            items.append(f"PEG={peg:.2f}(>3,严重高估) -0.5")
        elif peg > 2.0:
            score -= 0.3
            items.append(f"PEG={peg:.2f}(>2,高估) -0.3")

    # 现金流/净利比
    cashflow = financials.get("经营现金流量净额")
    profit = financials.get("归母净利润")
    if cashflow is not None and profit is not None and profit > 0:
        cf_ratio = cashflow / profit
        if cf_ratio > 1.0:
            score += 0.5
            items.append(f"现金流/净利={cf_ratio:.2f}(>1) +0.5")
        elif cf_ratio < 0.3:
            score -= 0.5
            items.append(f"现金流/净利={cf_ratio:.2f}(<0.3) -0.5")

    # 资产负债率
    debt = financials.get("资产负债率")
    if debt is not None:
        if debt > 70:
            score -= 0.5
            items.append(f"负债率={debt:.1f}%(>70%) -0.5")

    # 映射到1-5分
    result["score"] = max(1, min(5, round(score + 3)))  # 基准3分
    result["data_available"] = True
    result["breakdown"] = items
    return result


def calc_stock_grade(fundamental_star: int | None, concept_star: int | None) -> dict:
    """
    根据基本面⭐和概念⭐判定标的等级（A+/A/B）。
    fundamental_star 和 concept_star 优先取用户标注值，fallback到自动评分。
    """
    if fundamental_star is None or concept_star is None:
        return {"grade": "未知", "drawdown_line": 8, "label": "数据不足→默认A级(-8%)"}

    if fundamental_star >= 4 and concept_star >= 4:
        return {"grade": "A+", "drawdown_line": 10, "label": "A+级：基本面≥4+概念≥4，回撤止盈-10%"}
    elif fundamental_star >= 4 and concept_star <= 3:
        return {"grade": "A", "drawdown_line": 8, "label": "A级：基本面≥4+概念≤3，回撤止盈-8%"}
    else:
        return {"grade": "B", "drawdown_line": 6, "label": "B级：基本面≤3，回撤止盈-6%"}


# ═══════════════════════════════════════════════════════════════════════════
# 汇总 & 成本修正
# ═══════════════════════════════════════════════════════════════════════════

def calc_total_score(signals: dict, df: pd.DataFrame | None = None) -> dict:
    """汇总4因子得分，返回结构化的打分结果。"""
    s_tp, r_tp = score_trend_position(signals)
    s_vpa, r_vpa = score_volume_price_action(signals)
    s_vol, r_vol = score_volatility(signals)
    s_fib, r_fib = score_fib_structure(signals, df)

    total = s_tp + s_vpa + s_vol + s_fib
    total = round(max(0.0, min(100.0, total)), 1)

    if total >= 78:
        buyability = "[强烈推荐]"
    elif total >= 68:
        buyability = "[推荐]"
    elif total >= 55:
        buyability = "[关注]"
    elif total >= 40:
        buyability = "[偏弱]"
    else:
        buyability = "[回避]"

    return {
        "factors": {
            "趋势+位置": {"score": s_tp, "max": 35, "reason": r_tp},
            "量价+PriceAction": {"score": s_vpa, "max": 30, "reason": r_vpa},
            "波动率": {"score": s_vol, "max": 15, "reason": r_vol},
            "斐波那契/结构": {"score": s_fib, "max": 20, "reason": r_fib},
        },
        "total": total,
        "max": 100,
        "buyability": buyability,
    }


def calc_position_advice(total: float, tier: str) -> tuple[float, str]:
    """根据总分和层级，给出仓位建议（百分比）。"""
    if tier == "核心":
        if total >= 78:
            return 15.0, "满仓(15%)"
        elif total >= 68:
            return 7.0, "半仓(7%)"
        elif total >= 55:
            return 0.0, "观察不买"
        elif total >= 40:
            return 0.0, "持有者可继续持有，不建议新买"
        else:
            return -1.0, "建议卖出"
    else:  # 观察池
        if total >= 78:
            return 7.0, "半仓(7%)"
        elif total >= 68:
            return 3.0, "轻仓(3%)"
        elif total >= 40:
            return 0.0, "不买/持有者考虑卖出"
        else:
            return -1.0, "建议卖出"


def apply_cost_correction(
    signals: dict,
    total: float,
    base_pct: float,
    cost_price: float | None,
    shares: int | None,
    buy_date_str: str | None,
) -> dict:
    """持仓成本修正因子。无成本价则原样返回。"""
    if cost_price is None or cost_price <= 0:
        return {
            "enabled": False,
            "note": "未提供成本价，跳过成本修正",
            "final_pct": base_pct,
            "final_operation": _op_name(base_pct, 0),
        }

    today = date.today()
    price = signals.get("current_price", 0)
    if price <= 0:
        return {"enabled": False, "note": "现价无效", "final_pct": base_pct, "final_operation": "数据异常"}

    # 5B.1 盈亏状态
    pnl_pct = (price - cost_price) / cost_price * 100
    if pnl_pct > 20:
        pnl_label = "大幅浮盈++"
    elif pnl_pct >= 10:
        pnl_label = "中等浮盈+"
    elif pnl_pct >= 3:
        pnl_label = "小幅浮盈"
    elif pnl_pct >= -3:
        pnl_label = "成本附近"
    elif pnl_pct >= -8:
        pnl_label = "小幅浮亏"
    elif pnl_pct >= -15:
        pnl_label = "中等浮亏"
    else:
        pnl_label = "大幅浮亏--"

    # 5B.2 持仓时间
    hold_days = 0
    if buy_date_str:
        try:
            buy_date = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
            hold_days = (today - buy_date).days
        except ValueError:
            pass

    if hold_days < 5:
        time_factor = 0.3
        time_label = f"<5天，系数×0.3"
    elif hold_days < 20:
        time_factor = 0.7
        time_label = f"{hold_days}天(5-20)，系数×0.7"
    elif hold_days < 60:
        time_factor = 1.0
        time_label = f"{hold_days}天(20-60)，正常"
    else:
        time_factor = 1.0
        time_label = f"{hold_days}天(>60)，长期持仓，止盈宽容+3%"

    # 5B.3 成本均线比
    ma20 = signals.get("ma20") or 0
    ma60 = signals.get("ma60") or 0
    cost_ma_adj = 0
    cost_ma_reasons = []

    if ma20 > 0:
        if cost_price < ma20 and price > cost_price:
            cost_ma_adj += 5
            cost_ma_reasons.append(f"成本{cost_price:.2f}<MA20({ma20:.2f})且浮盈→+5%")
        elif cost_price > ma20 and price < cost_price:
            cost_ma_adj -= 5
            cost_ma_reasons.append(f"成本{cost_price:.2f}>MA20({ma20:.2f})且被套→-5%")

    if ma60 > 0:
        if cost_price < ma60:
            cost_ma_reasons.append(f"成本<MA60({ma60:.2f})，买入位置极低→止盈线上移3%")
        elif cost_price > ma60 * 1.3:
            cost_ma_reasons.append(f"成本>MA60×1.3({ma60*1.3:.2f})，高位买入→止盈线下移3%")

    # 5B.4 加仓/减仓决策矩阵
    corrected_pct = base_pct * time_factor + cost_ma_adj
    corrected_pct = max(-1, min(30, corrected_pct))

    if total >= 75:
        if pnl_pct >= 0:
            multiplier = 1.5 if hold_days < 20 else 1.3
            final_pct = base_pct * multiplier
            op = f"[加仓] 加仓（原{base_pct:.0f}% → {final_pct:.0f}%）"
        else:
            multiplier = 1.2 if hold_days < 20 else 1.5
            final_pct = base_pct * multiplier
            op = f"[加仓] 加仓摊薄（原{base_pct:.0f}% → {final_pct:.0f}%）"
    elif total >= 65:
        if pnl_pct >= 0:
            final_pct = base_pct * (1.1 if hold_days >= 20 else 1.0)
            op = f"[持有] {'轻仓加仓' if hold_days >= 20 else '持有'}（{final_pct:.0f}%）"
        else:
            final_pct = base_pct
            op = f"[持有] 持有观察（{final_pct:.0f}%）"
    elif total >= 50:
        if pnl_pct >= 0:
            final_pct = base_pct * (0.7 if hold_days >= 20 else 1.0)
            op = f"[减仓] {'减仓30%' if hold_days >= 20 else '持有，上移止损至成本价'}（{final_pct:.0f}%）" if base_pct > 0 else "观望"
        else:
            final_pct = base_pct * 0.5
            op = f"[减仓] 减仓50%（弱势+被套）→ {final_pct:.0f}%"
    elif total >= 35:
        if base_pct > 0:
            final_pct = base_pct * (0.3 if hold_days < 20 else 0)
            op = f"[清仓] {'减仓70%' if hold_days < 20 else '清仓'}"
        else:
            final_pct = 0
            op = "不买"
    else:
        final_pct = -1
        op = "[清仓] 清仓"

    final_pct = max(-1, min(30, final_pct))

    return {
        "enabled": True,
        "cost_price": cost_price,
        "current_price": price,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_label": pnl_label,
        "hold_days": hold_days,
        "time_factor": time_factor,
        "time_label": time_label,
        "cost_ma_adj": cost_ma_adj,
        "cost_ma_reasons": cost_ma_reasons,
        "base_pct": base_pct,
        "corrected_pct": round(corrected_pct, 1),
        "final_pct": round(final_pct, 1),
        "final_operation": op,
    }


def _op_name(pct: float, fallback: float) -> str:
    if pct > 0:
        return f"买入({pct:.0f}%)"
    elif pct < 0:
        return "卖出"
    else:
        return "观望"


# ═══════════════════════════════════════════════════════════════════════════
# 格式化输出
# ═══════════════════════════════════════════════════════════════════════════

def format_score_output(
    code: str,
    name: str,
    quote: dict,
    signals: dict,
    score_result: dict,
    cost_result: dict,
    tier: str = "核心",
    momentum: dict | None = None,
    fundamental_auto: dict | None = None,
    grade: dict | None = None,
    supply_demand: dict | None = None,
) -> str:
    """格式化为 agent 可直接消费的结构化文本。"""
    lines = []
    lines.append(f"## 量化多因子评分 — {name}({code})")
    lines.append(f"> 计算时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据截止: T-1日 | 零未来函数")
    lines.append("")

    # 基本信息
    lines.append("### 基本信息")
    lines.append(f"- 现价: {quote['price']:.2f} | 今日涨跌: {quote['pct_change']:+.2f}%")
    lines.append(f"- PE(TTM): {quote['pe_ttm']:.2f} | PB: {quote['pb']:.2f} | 市值: {fmt_market_cap(quote.get('market_cap') or quote.get('total_mv_yi', 0))}")
    lines.append(f"- 量比: {signals.get('vol_ratio', 'N/A')} | K线: {'阳' if signals.get('body_pct',0)>0 else '阴'}{abs(signals.get('body_pct',0)):.1f}% | 层级: {tier}")
    if grade:
        lines.append(f"- **标的等级**: {grade['grade']} | {grade['label']}")
    lines.append("")

    # 量化动量因子（脚本计算）
    if momentum and momentum.get("data_available"):
        lines.append("### 量化动量因子（脚本计算，满分8）")
        lines.append("| 因子 | 公式 | 满分 | 得分 | 依据 |")
        lines.append("|------|------|------|------|------|")
        m = momentum
        cons = m.get("momentum_consistency", 0)
        lines.append(f"| 动量一致性 | (1y%>0)+(3m%>0)+(20d%>0) | 3 | {cons} | 1y:{m.get('one_year_pct','N/A')}% 3m:{m.get('three_month_pct','N/A')}% 20d:{m.get('twenty_day_pct','N/A')}% |")
        ai_pts = 2 if m.get("is_ai_chain") else 0
        lines.append(f"| 产业链归属 | GPU/AI核心(NVDA/AMD/AVGO/TSM等) | 2 | {ai_pts} | {'是' if m.get('is_ai_chain') else '否(可由CSV标注覆盖)'} |")
        lsr = m.get("long_short_ratio")
        lsr_str = f"{lsr:.2f}" if lsr and lsr != 999 else ("∞(长期涨短期跌)" if lsr == 999 else "N/A")
        lsr_pts = 2 if (lsr and lsr > 5) else (1 if (lsr and lsr >= 3) else 0)
        lines.append(f"| 长动比 | 1年涨幅÷3月涨幅 | 2 | {lsr_pts} | {lsr_str} |")
        d20_pts = 1 if (m.get("twenty_day_pct") is not None and m["twenty_day_pct"] > 0) else 0
        lines.append(f"| 20日趋势 | 20日%>0为1 | 1 | {d20_pts} | {m.get('twenty_day_pct','N/A')}% |")
        lines.append(f"| **动量总分** | | **8** | **{m.get('momentum_total',0)}** | {m.get('momentum_grade','')} |")
        lines.append("")

    # 因子评分表
    lines.append("### 多因子技术评分（4因子）")
    lines.append("| 因子 | 满分 | 得分 | 依据 |")
    lines.append("|------|------|------|------|")
    for fname, fdata in score_result["factors"].items():
        lines.append(f"| {fname} | {fdata['max']} | {fdata['score']} | {fdata['reason']} |")
    lines.append(f"| **总分** | **100** | **{score_result['total']}** | |")
    lines.append("")
    lines.append(f"**可买度**: {score_result['buyability']}")
    lines.append("")

    # 基本面自动初评（脚本计算，供AI修正）
    if fundamental_auto and fundamental_auto.get("data_available"):
        lines.append("### 基本面自动初评（脚本计算，AI可修正）")
        lines.append(f"- 自动评分: *{fundamental_auto['score']}/5")
        if fundamental_auto.get("breakdown"):
            for item in fundamental_auto["breakdown"]:
                lines.append(f"  - {item}")
        lines.append(f"- [!] 此评分为机器初评，不含概念/行业地位判断，AI 需结合财报细节修正")
        lines.append("")

    # 成本修正（如有）
    if cost_result["enabled"]:
        lines.append("### 持仓成本修正")
        lines.append("| 指标 | 值 | 说明 |")
        lines.append("|------|-----|------|")
        lines.append(f"| 成本价 | ${cost_result['cost_price']:.2f} | 用户提供 |")
        lines.append(f"| 盈亏比 | {cost_result['pnl_pct']:+.2f}% | {cost_result['pnl_label']} |")
        lines.append(f"| 持仓天数 | {cost_result['hold_days']} 天 | {cost_result['time_label']} |")
        if cost_result['cost_ma_reasons']:
            for r in cost_result['cost_ma_reasons']:
                lines.append(f"| 成本均线比 | | {r} |")
        lines.append(f"| 原始建议仓位 | {cost_result['base_pct']:.0f}% | 纯量化打分结果 |")
        lines.append(f"| **最终建议操作** | | **{cost_result['final_operation']}** |")
        lines.append("")
    else:
        base_pct, base_label = calc_position_advice(score_result["total"], tier)
        lines.append(f"**建议仓位**: {base_label}")
        lines.append("")

    # 供需评估
    if supply_demand and supply_demand.get("star", 0) > 0:
        lines.append("### 供需评估（脚本自动计算，6项硬指标）")
        sd = supply_demand
        star = sd.get("star", 0)
        star_str = "*" * star + "-" * (5 - star)
        lines.append(f"- 供需评级: {star_str} ({star}/5) | 总供需分: {sd.get('total_score', 0):.0f}/100")
        lines.append(f"- 需求端: {sd.get('demand_score', 0):.0f}/50 | 供给端: {sd.get('supply_score', 0):.0f}/50")
        lines.append(f"- {sd.get('summary', '')}")
        details = sd.get("details", {})
        if details:
            for key, d in details.items():
                lines.append(f"  - {key}: {d['score']}/{d['max']} {d['label']} ({d.get('detail', '')})")
        lines.append("")

    # 必填提醒
    lines.append("---")
    lines.append("> [!] **AI 必须继续输出以下内容（脚本不计算，需AI判断）：**")
    lines.append("> 1. **最优买入点**（突破买入价 / 触底买入价 + 触发条件）")
    lines.append("> 2. **止盈止损表**（具体价格，按标的等级分档）")
    lines.append("> 3. **概念*(1-5)评级** 和 **最终基本面*(1-5)**（可在自动初评基础上修正）")
    lines.append("> 4. **关键价位**（阻力位/支撑位）")
    lines.append("> 5. **操作理由+风险提示**")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 大盘风控
# ═══════════════════════════════════════════════════════════════════════════

def fetch_index_risk(symbol: str = "SOXX") -> dict:
    """获取半导体ETF状态，返回仓位上限。默认 SOXX。"""
    try:
        return fetch_index_quote(symbol)
    except Exception as e:
        return {"error": f"{symbol} 行情获取失败: {e}"}


def format_index_output(risk: dict) -> str:
    if "error" in risk:
        return f"[!] 大盘风控: {risk['error']}"
    lines = [
        "## 大盘风控",
        f"- {risk.get('symbol', 'SOXX')}: {risk['idx_price']:.2f} ({risk['idx_pct']:+.2f}%)",
        f"- T-1日收盘: {risk.get('last_close', 'N/A')}",
        f"- MA60: {risk.get('ma60', 'N/A')} | MA200: {risk.get('ma200', 'N/A')}",
        f"- 状态: {risk['status']}",
        f"- **总仓位上限: {risk['position_cap']}%** | 最大持仓: {risk['max_stocks']}",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def score_single_stock(
    code: str,
    name: str = "",
    tier: str = "核心",
    cost_price: float | None = None,
    shares: int | None = None,
    buy_date: str | None = None,
    kline_days: int = 120,
    fundamental_star: int | None = None,
    concept_star: int | None = None,
) -> dict:
    """对单只股票进行完整打分，返回结构化结果。"""

    # 获取数据
    quote = fetch_realtime_quote(code)
    if not name:
        name = quote.get("name", "")

    # 拉更长K线用于动量计算（至少250日）
    momentum_days = max(kline_days, 260)
    df = fetch_daily_kline(code, days=momentum_days)
    df = compute_indicators(df)
    signals = get_latest_signals(df, quote)

    # 量化打分（传入df用于斐波那契/结构计算）
    score_result = calc_total_score(signals, df)

    # 仓位建议
    base_pct, base_label = calc_position_advice(score_result["total"], tier)

    # 成本修正
    cost_result = apply_cost_correction(signals, score_result["total"], base_pct, cost_price, shares, buy_date)

    # == 动量因子 ==
    momentum = calc_momentum_factors(df, code)

    # == 基本面自动初评 ==
    try:
        financials = fetch_financial_data(code)
    except Exception:
        financials = None
    fundamental_auto = calc_fundamental_auto_score(quote, financials)
    if financials:
        fundamental_auto["roe"] = round(financials["净资产收益率(ROE)"], 1) if financials.get("净资产收益率(ROE)") else None
        fundamental_auto["gross_margin"] = round(financials["毛利率"], 1) if financials.get("毛利率") else None
        fundamental_auto["net_margin"] = round(financials["销售净利率"], 1) if financials.get("销售净利率") else None
        fundamental_auto["revenue_yoy"] = round(financials["营业总收入同比"], 1) if financials.get("营业总收入同比") else None
        fundamental_auto["net_profit_yoy"] = round(financials["净利润同比增长率"], 1) if financials.get("净利润同比增长率") else None
        fundamental_auto["debt_ratio"] = round(financials["资产负债率"], 1) if financials.get("资产负债率") else None
        rev = financials.get("营业总收入") or 0
        fundamental_auto["cf_ratio"] = round(financials.get("经营现金流量净额", 0) / rev, 3) if rev > 0 and financials.get("经营现金流量净额") else None

    # == 新增：标的等级 ==
    f_star = fundamental_star if fundamental_star is not None else fundamental_auto.get("score")
    c_star = concept_star  # 概念⭐只能人工标注，不自动计算
    grade_info = calc_stock_grade(f_star, c_star)

    # == 新增：供需评估 ==
    supply_demand = None
    try:
        from core.strategies.supply_demand import fetch_supply_demand_data, calc_supply_demand, merge_pe_into_peg
        sd_data = fetch_supply_demand_data(code)
        supply_demand = calc_supply_demand(sd_data)
        if sd_data.get("quarters") and sd_data["quarters"][0].get("profit_yoy"):
            supply_demand["profit_yoy"] = sd_data["quarters"][0]["profit_yoy"]
            supply_demand = merge_pe_into_peg(supply_demand, quote.get("pe_ttm", 0))
        supply_demand["quarters"] = sd_data.get("quarters", [])
    except Exception:
        supply_demand = None

    return {
        "code": code,
        "name": name,
        "tier": tier,
        "quote": {k: v for k, v in quote.items() if not isinstance(v, (np.ndarray, pd.DataFrame))},
        "signals": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in signals.items() if v is not None},
        "score": score_result,
        "cost_correction": cost_result,
        "momentum": momentum,
        "fundamental_auto": fundamental_auto,
        "grade": grade_info,
        "supply_demand": supply_demand,
    }


def main():
    parser = argparse.ArgumentParser(description="量化多因子打分引擎")
    parser.add_argument("code", nargs="?", help="美股代码，如 NVDA")
    parser.add_argument("--cost", type=float, help="成本价")
    parser.add_argument("--shares", type=int, help="持仓数量")
    parser.add_argument("--buy-date", help="买入日期(YYYY-MM-DD)")
    parser.add_argument("--tier", default="核心", choices=["核心", "观察"], help="股票层级")
    parser.add_argument("--name", default="", help="股票名称")
    parser.add_argument("--kline-days", type=int, default=120, help="K线天数")
    parser.add_argument("--output", help="输出JSON文件")
    parser.add_argument("--pool", help="批量打分CSV文件")
    parser.add_argument("--no-index", action="store_true", help="不输出大盘风控")
    parser.add_argument("--index", default="SOXX", help="风控基准ETF，默认 SOXX")
    args = parser.parse_args()

    # 大盘风控
    if not args.no_index:
        index_risk = fetch_index_risk(args.index)
        print(format_index_output(index_risk))
        print()

    if args.pool:
        # 批量模式
        import csv
        with open(args.pool, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        results = []
        for row in rows:
            code = row.get("代码", "").strip()
            if not code:
                continue
            name = row.get("名称", "").strip()
            tier = row.get("层级", "核心").strip()
            cost_str = row.get("成本价", "").strip()
            shares_str = row.get("持仓量", "").strip()
            buy_date_str = row.get("买入日期", "").strip()

            cost = float(cost_str) if cost_str else None
            shares = int(shares_str) if shares_str else None
            buy_date = buy_date_str if buy_date_str else None

            log.info(f"打分: {code} {name}")
            try:
                result = score_single_stock(code, name, tier, cost, shares, buy_date, args.kline_days)
                results.append(result)
                print(format_score_output(
                    code, name, result["quote"], result["signals"],
                    result["score"], result["cost_correction"], tier,
                    result.get("momentum"),
                    result.get("fundamental_auto"), result.get("grade"),
                    result.get("supply_demand"),
                ))
                print()
            except Exception as e:
                log.error(f"{code} {name} 打分失败: {e}")

        if args.output and results:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2, default=str)
            log.info(f"结果已保存至 {args.output}")
    elif args.code:
        # 单股模式
        result = score_single_stock(args.code, args.name, args.tier, args.cost, args.shares, args.buy_date, args.kline_days)
        print(format_score_output(
            args.code, args.name or result["name"], result["quote"], result["signals"],
            result["score"], result["cost_correction"], args.tier,
            result.get("momentum"),
            result.get("fundamental_auto"), result.get("grade"),
            result.get("supply_demand"),
        ))

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=str)
            log.info(f"结果已保存至 {args.output}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
