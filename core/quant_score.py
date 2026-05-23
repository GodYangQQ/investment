#!/usr/bin/env python3
"""
量化多因子打分引擎（零未来函数）
基于 stock_strategy.py 已计算的技术指标，输出0-100的量化评分 + 成本修正

用法：
    # 单只股票打分（无成本）
    python quant_score.py 600519

    # 单只股票打分（带成本+持仓）
    python quant_score.py 600519 --cost 95.50 --shares 500 --buy-date 2026-05-10

    # 批量打分（从CSV读取自选池）
    python quant_score.py --pool pool.csv

    # 输出JSON
    python quant_score.py 600519 --cost 95.50 --output score.json

CSV格式（pool.csv）：
    代码,名称,层级,成本价,持仓量,买入日期
    600519,贵州茅台,核心,1680.00,100,2026-05-15
    002475,立讯精密,观察,,,        （无持仓可不填成本/量/日期）
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
from stock_strategy import (
    fetch_realtime_quote,
    fetch_daily_kline,
    compute_indicators,
    get_latest_signals,
    _HAS_AKSHARE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 量化多因子打分（完全机器计算，零LLM参与）
# ═══════════════════════════════════════════════════════════════════════════

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

def score_trend(signals: dict) -> tuple[float, str]:
    """趋势因子（满分25，连续值）。基于MA排列间距 + 价格位置。"""
    price = signals.get("current_price", 0)
    ma5 = signals.get("ma5") or 0
    ma10 = signals.get("ma10") or 0
    ma20 = signals.get("ma20") or 0
    ma60 = signals.get("ma60") or 0

    if not all([ma5, ma10, ma20, ma60]) or price <= 0:
        return 5.0, "MA数据不全"

    # 1) MA排列分 (0-12)：用 MA5/MA10, MA10/MA20 的距离百分比
    #    理想：MA5 > MA10 > MA20，间距各约 1-3%
    gap_5_10 = (ma5 / ma10 - 1) * 100 if ma10 > 0 else -99
    gap_10_20 = (ma10 / ma20 - 1) * 100 if ma20 > 0 else -99
    gap_price_ma20 = (price / ma20 - 1) * 100 if ma20 > 0 else -99

    # 每个gap映射到0-4分：最佳区间 0.5%~4%，用钟形
    def gap_score(gap):
        if gap < -5:
            return 0.0
        elif gap < 0:
            return 1.0 + gap / 5  # -5~0 线性从0到1
        elif gap <= 3:
            return 2.0 + gap / 3 * 2.0  # 0~3 线性从2到4
        elif gap <= 8:
            return 4.0 - (gap - 3) / 5 * 2.0  # 3~8 线性从4到2
        else:
            return max(0.0, 2.0 - (gap - 8) / 10 * 2.0)  # >8 逐渐衰减

    align_score = (gap_score(gap_5_10) + gap_score(gap_10_20) + gap_score(gap_price_ma20)) / 3 * 3
    # max 12

    # 2) 中期均线位置分 (0-8)：price vs MA60
    gap_ma60 = (price / ma60 - 1) * 100 if ma60 > 0 else -99
    if gap_ma60 > 20:
        ma60_score = 8.0
    elif gap_ma60 > 5:
        ma60_score = 5.0 + (gap_ma60 - 5) / 15 * 3.0
    elif gap_ma60 > -3:
        ma60_score = 3.0 + (gap_ma60 + 3) / 8 * 2.0
    elif gap_ma60 > -10:
        ma60_score = 1.0 + (gap_ma60 + 10) / 7 * 2.0
    else:
        ma60_score = 0.0

    # 3) 均线方向分 (0-5)：MA20的5日斜率（用ma5_vs_ma20信号辅助）
    ma5_vs_ma20 = signals.get("ma5_vs_ma20", "")
    if ma5_vs_ma20 == "金叉":
        # 计算金叉强度：价在MA20上方越远越好（但有上限）
        if gap_price_ma20 > 2:
            dir_score = 5.0
        elif gap_price_ma20 > 0:
            dir_score = 2.5 + gap_price_ma20 / 2 * 1.25
        else:
            dir_score = 2.0 + gap_price_ma20 / 2 * 1.0
    else:
        dir_score = max(0.0, 2.0 + gap_price_ma20 / 5 * 2.0)

    total = _clamp(align_score + ma60_score + dir_score, 0, 25)
    total = round(total, 1)

    detail = f"MA排列{gap_5_10:.1f}%/{gap_10_20:.1f}%, 价距MA20={gap_price_ma20:.1f}%, 距MA60={gap_ma60:.1f}%"
    return total, detail


def score_position(signals: dict) -> tuple[float, str]:
    """位置因子（满分25，连续值）。距60日高点回撤 + 距20日低点位置。"""
    high_60d = signals.get("high_60d")
    low_20d = signals.get("low_20d")
    price = signals.get("current_price", 0)

    if high_60d is None or high_60d <= 0 or price <= 0:
        return 8.0, "60日数据不足"

    drawdown = (high_60d - price) / high_60d * 100  # 0=新高, 正=回撤

    # 回撤映射到0-20分：最佳 8-20%（买入回调），用非对称抛物线
    if drawdown <= 3:
        dd_score = 5.0 - drawdown / 3 * 1.0  # 0~3%: 5→4，追高风险
    elif drawdown <= 8:
        dd_score = 4.0 + (drawdown - 3) / 5 * 11.0  # 3~8%: 4→15
    elif drawdown <= 20:
        dd_score = 15.0 + (drawdown - 8) / 12 * 5.0  # 8~20%: 15→20（最佳区间）
    elif drawdown <= 30:
        dd_score = 20.0 - (drawdown - 20) / 10 * 12.0  # 20~30%: 20→8
    elif drawdown <= 45:
        dd_score = 8.0 - (drawdown - 30) / 15 * 5.0  # 30~45%: 8→3
    else:
        dd_score = max(0.0, 3.0 - (drawdown - 45) / 10 * 3.0)

    # 距20日低点补充分 (0-5)：价在20日低点上方10-30%最佳
    if low_20d and low_20d > 0:
        above_low = (price / low_20d - 1) * 100
        if 5 <= above_low <= 25:
            low_score = 5.0 - abs(above_low - 15) / 10 * 3.0
        elif above_low < 5:
            low_score = 2.0 + above_low / 5 * 2.0
        else:
            low_score = max(0.0, 2.0 - (above_low - 25) / 20 * 2.0)
    else:
        low_score = 2.5

    total = _clamp(dd_score + low_score, 0, 25)
    total = round(total, 1)

    detail = f"距60日高回撤{drawdown:.1f}%"
    if low_20d and low_20d > 0:
        detail += f", 距20日低{(price/low_20d-1)*100:.1f}%"
    return total, detail


def score_volume_price(signals: dict) -> tuple[float, str]:
    """量价因子（满分20，连续值）。5日均量vs20日均量 + OBV + 量价相关性。"""
    vol_ma5 = signals.get("vol_ma5") or 0
    vol_ma20 = signals.get("vol_ma20") or 0
    obv_trend = signals.get("obv_trend", "")
    cmf = signals.get("cmf_20d") or 0
    corr = signals.get("price_vol_corr_10d")

    # 1) 量比得分 (0-8)：vol_ma5/vol_ma20
    if vol_ma20 > 0:
        vol_ratio = vol_ma5 / vol_ma20
        if 1.0 <= vol_ratio <= 2.5:
            vol_score = 4.0 + (vol_ratio - 1.0) / 1.5 * 4.0  # 1~2.5: 4→8
        elif 0.7 <= vol_ratio < 1.0:
            vol_score = 1.0 + (vol_ratio - 0.7) / 0.3 * 3.0  # 0.7~1: 1→4
        elif vol_ratio > 2.5:
            vol_score = max(2.0, 8.0 - (vol_ratio - 2.5) / 3.0 * 6.0)  # >2.5: 逐渐衰减
        else:
            vol_score = max(0.0, vol_ratio / 0.7 * 1.0)
    else:
        vol_score = 3.0

    # 2) OBV趋势 (0-6)
    if obv_trend == "多头":
        obv_score = 6.0
    elif obv_trend == "空头":
        obv_score = 0.0
    else:
        obv_score = 3.0

    # 3) CMF (0-3)
    if cmf is not None:
        cmf_score = _clamp(1.5 + cmf * 15, 0, 3)
    else:
        cmf_score = 1.5

    # 4) 量价相关性 (0-3)：负相关=好（放量涨缩量跌），正相关=差
    if corr is not None and not (isinstance(corr, float) and np.isnan(corr)):
        if corr < -0.3:
            corr_score = 3.0
        elif corr < 0:
            corr_score = 1.5 + abs(corr) / 0.3 * 1.5
        elif corr < 0.3:
            corr_score = 1.5 - corr / 0.3 * 1.0
        else:
            corr_score = max(0.0, 0.5 - (corr - 0.3) / 0.7 * 0.5)
    else:
        corr_score = 1.5

    total = _clamp(vol_score + obv_score + cmf_score + corr_score, 0, 20)
    total = round(total, 1)

    detail_parts = []
    if vol_ma20 > 0:
        detail_parts.append(f"量比{vol_ma5/vol_ma20:.2f}")
    detail_parts.append(f"OBV={obv_trend}")
    return total, ", ".join(detail_parts)


def score_rsi(signals: dict) -> tuple[float, str]:
    """RSI因子（满分15，连续值）。用非对称钟形函数，40-60最优。"""
    rsi = signals.get("rsi14")
    if rsi is None:
        return 7.5, "RSI数据缺失"

    # 钟形函数：中心55，左右对称衰减，但左翼（超卖）容忍度更高
    center = 52.0
    if rsi > center:
        # 右翼（超买方向）：从中心到70缓慢衰减，70+快速衰减
        diff = rsi - center
        if diff <= 18:  # 52-70
            score = 15.0 - (diff / 18) ** 1.5 * 10.0
        else:  # 70+
            score = max(0.0, 5.0 - (diff - 18) / 30 * 5.0)
    else:
        # 左翼（超卖方向）：缓慢衰减，30以下加速衰减
        diff = center - rsi
        if diff <= 22:  # 30-52
            score = 15.0 - (diff / 22) ** 2.0 * 8.0
        else:  # <30
            score = max(0.0, 7.0 - (diff - 22) / 20 * 7.0)

    total = _clamp(score, 0, 15)
    total = round(total, 1)
    return total, f"RSI(14)={rsi:.1f}"


def score_volatility(signals: dict) -> tuple[float, str]:
    """波动率因子（满分15，连续值）。ATR(14)/价格 + 近5日波动率。"""
    atr = signals.get("atr14")
    price = signals.get("current_price", 0)
    vol5 = signals.get("volatility_5d")

    if atr is None or atr <= 0 or price <= 0:
        return 7.5, "ATR数据不足"

    atr_pct = (atr / price) * 100

    # ATR%映射：最佳2-5%，用抛物线
    if atr_pct <= 1:
        atr_score = 3.0 + atr_pct / 1.0 * 4.0  # 0~1: 3→7
    elif atr_pct <= 2:
        atr_score = 7.0 + (atr_pct - 1) / 1.0 * 6.0  # 1~2: 7→13
    elif atr_pct <= 5:
        atr_score = 13.0 + (atr_pct - 2) / 3.0 * 2.0  # 2~5: 13→15（最佳区间）
    elif atr_pct <= 8:
        atr_score = 15.0 - (atr_pct - 5) / 3.0 * 7.0  # 5~8: 15→8
    elif atr_pct <= 15:
        atr_score = 8.0 - (atr_pct - 8) / 7.0 * 5.0  # 8~15: 8→3
    else:
        atr_score = max(0.0, 3.0 - (atr_pct - 15) / 20 * 3.0)

    # 5日波动率调整 (±2)
    vol5_adj = 0.0
    if vol5 is not None and vol5 > 0:
        if 1.0 <= vol5 <= 3.0:
            vol5_adj = 1.0
        elif vol5 > 5.0:
            vol5_adj = -1.0

    total = _clamp(atr_score + vol5_adj, 0, 15)
    total = round(total, 1)
    return total, f"ATR/价格={atr_pct:.2f}%"


def score_bonus(signals: dict) -> tuple[float, str]:
    """附加项：MACD柱变化 + 连续阴阳线 + 布林带。±13分，连续值。"""
    reasons = []

    # 1) MACD柱强度 (0-7)：用柱值 / 价格 的标准化
    macd_cross = signals.get("macd_cross", "")
    macd_hist = signals.get("macd_hist") or 0
    price = signals.get("current_price", 1)
    dif = signals.get("dif") or 0
    dea = signals.get("dea") or 0

    hist_pct = abs(macd_hist) / price * 100 if price > 0 else 0

    if macd_cross == "金叉":
        macd_score = 5.0 + min(hist_pct * 80, 2.0)  # 5-7
        reasons.append(f"MACD金叉(+{macd_score:.1f})")
    elif macd_cross == "死叉":
        macd_score = -5.0 - min(hist_pct * 80, 2.0)  # -5~-7
        reasons.append(f"MACD死叉({macd_score:.1f})")
    elif dif > dea:
        # 多头趋势中，柱值越大越好
        macd_score = 1.0 + min(hist_pct * 50, 2.0)
    else:
        macd_score = -1.0 - min(hist_pct * 50, 2.0)

    # 2) 连续阴/阳线 (±4)
    cons_up = signals.get("consecutive_up", 0)
    cons_down = signals.get("consecutive_down", 0)
    cons_score = 0.0

    if cons_down >= 3:
        cons_score += min(cons_down - 2, 3) * 1.0  # 连阴3天+1，4天+2，5天+3
        reasons.append(f"连阴{cons_down}天(+{min(cons_down-2,3):.0f})")
    if cons_up >= 4:
        cons_score -= min(cons_up - 3, 3) * 1.0  # 连阳4天-1，5天-2，6天-3
        reasons.append(f"连阳{cons_up}天({cons_score:.0f})")

    # 3) 布林带位置 (±2)
    bb_upper = signals.get("bb_upper")
    bb_lower = signals.get("bb_lower")
    bb_mid = signals.get("bb_mid")
    bb_score = 0.0

    if bb_upper and bb_lower and bb_mid and bb_mid > 0:
        bb_width = (bb_upper - bb_lower) / bb_mid * 100
        # 布林带收窄：变盘信号
        if bb_width < 5:
            bb_score -= 2.0
            reasons.append(f"布林带宽{bb_width:.1f}%极窄(-2)")
        elif bb_width < 8:
            bb_score -= 0.5
        # 价格在布林下轨附近：超卖反弹
        bb_pos = signals.get("bb_position")
        if bb_pos is not None:
            if bb_pos < 15:
                bb_score += 2.0
                reasons.append(f"布林下轨(+2)")
            elif bb_pos > 85:
                bb_score -= 1.5
                reasons.append(f"布林上轨(-1.5)")

    total = _clamp(macd_score + cons_score + bb_score, -13, 13)
    total = round(total, 1)

    if not reasons:
        reasons.append("无附加")
    return total, "; ".join(reasons)

    return bonus, "; ".join(reasons) if reasons else "无附加"


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

    # 产业链归属（AI链判断，基于代码前缀/行业关键词，后续可由CSV标注覆盖）
    ai_chain_codes = set()  # 可由用户CSV中"AI链"列覆盖
    is_ai_chain = code in ai_chain_codes

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


def calc_rsi_slope_curvature(closes: list[float]) -> dict:
    """
    计算RSI(14)的5日斜率和曲率（5日-20日）。
    需要至少 14+21=35 个收盘价。
    """
    result = {
        "rsi_current": None,
        "rsi_slope_5d": None,
        "rsi_curvature": None,
        "rsi_arrow": "—",
        "data_available": False,
    }

    if len(closes) < 36:
        return result

    arr = np.array(closes, dtype=float)
    n = len(arr)

    def _rsi(arr_window: np.ndarray, period: int = 14) -> float:
        diffs = np.diff(arr_window[-period - 1:])
        gains = np.where(diffs > 0, diffs, 0.0)
        losses = np.where(diffs < 0, -diffs, 0.0)
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))
        if avg_loss == 0:
            return 100.0
        for i in range(period, len(diffs)):
            avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
            avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
        return float(100.0 - (100.0 / (1.0 + rs)))

    rsi_history = []
    for i in range(14, n):
        rsi_history.append(_rsi(arr[i - 14:i + 1]))

    if len(rsi_history) < 21:
        return result

    result["rsi_current"] = round(rsi_history[-1], 1)

    # 5日斜率
    s5 = (rsi_history[-1] - rsi_history[-6]) / 5
    result["rsi_slope_5d"] = round(s5, 1)

    # 曲率 = 5日斜率 - 20日斜率
    s20 = (rsi_history[-1] - rsi_history[-21]) / 20
    curv = s5 - s20
    result["rsi_curvature"] = round(curv, 1)

    # 方向箭头
    if s5 > 0.2:
        result["rsi_arrow"] = "↑"
    elif s5 < -0.2:
        result["rsi_arrow"] = "↓"
    else:
        result["rsi_arrow"] = "→"

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

def calc_total_score(signals: dict) -> dict:
    """汇总所有因子得分，返回结构化的打分结果。"""
    s_trend, r_trend = score_trend(signals)
    s_pos, r_pos = score_position(signals)
    s_vol, r_vol = score_volume_price(signals)
    s_rsi, r_rsi = score_rsi(signals)
    s_atr, r_atr = score_volatility(signals)
    s_bonus, r_bonus = score_bonus(signals)

    total = s_trend + s_pos + s_vol + s_rsi + s_atr + s_bonus
    total = round(max(0.0, min(100.0, total)), 1)

    # 可买度判定（阈值收紧，因为连续化后分数更分散）
    if total >= 78:
        buyability = "🟢 强烈推荐"
    elif total >= 68:
        buyability = "🟢 推荐"
    elif total >= 55:
        buyability = "🟡 关注"
    elif total >= 40:
        buyability = "🟠 偏弱"
    else:
        buyability = "🔴 回避"

    return {
        "factors": {
            "趋势因子": {"score": s_trend, "max": 25, "reason": r_trend},
            "位置因子": {"score": s_pos, "max": 25, "reason": r_pos},
            "量价因子": {"score": s_vol, "max": 20, "reason": r_vol},
            "RSI因子": {"score": s_rsi, "max": 15, "reason": r_rsi},
            "波动率因子": {"score": s_atr, "max": 15, "reason": r_atr},
            "附加项": {"score": s_bonus, "max": 13, "reason": r_bonus},
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
        pnl_label = "🟢🟢 大幅浮盈"
    elif pnl_pct >= 10:
        pnl_label = "🟢 中等浮盈"
    elif pnl_pct >= 3:
        pnl_label = "🟡 小幅浮盈"
    elif pnl_pct >= -3:
        pnl_label = "⚪ 成本附近"
    elif pnl_pct >= -8:
        pnl_label = "🟠 小幅浮亏"
    elif pnl_pct >= -15:
        pnl_label = "🔴 中等浮亏"
    else:
        pnl_label = "🔴🔴 大幅浮亏"

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
            op = f"🟢 加仓（原{base_pct:.0f}% → {final_pct:.0f}%）"
        else:
            multiplier = 1.2 if hold_days < 20 else 1.5
            final_pct = base_pct * multiplier
            op = f"🟢 加仓摊薄（原{base_pct:.0f}% → {final_pct:.0f}%）"
    elif total >= 65:
        if pnl_pct >= 0:
            final_pct = base_pct * (1.1 if hold_days >= 20 else 1.0)
            op = f"🟡 {'轻仓加仓' if hold_days >= 20 else '持有'}（{final_pct:.0f}%）"
        else:
            final_pct = base_pct
            op = f"🟡 持有观察（{final_pct:.0f}%）"
    elif total >= 50:
        if pnl_pct >= 0:
            final_pct = base_pct * (0.7 if hold_days >= 20 else 1.0)
            op = f"🟡 {'减仓30%' if hold_days >= 20 else '持有，上移止损至成本价'}（{final_pct:.0f}%）" if base_pct > 0 else "观望"
        else:
            final_pct = base_pct * 0.5
            op = f"🟠 减仓50%（弱势+被套）→ {final_pct:.0f}%"
    elif total >= 35:
        if base_pct > 0:
            final_pct = base_pct * (0.3 if hold_days < 20 else 0)
            op = f"🔴 {'减仓70%' if hold_days < 20 else '清仓'}"
        else:
            final_pct = 0
            op = "不买"
    else:
        final_pct = -1
        op = "🔴 清仓"

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
    rsi_detail: dict | None = None,
    fundamental_auto: dict | None = None,
    grade: dict | None = None,
) -> str:
    """格式化为 agent 可直接消费的结构化文本。"""
    lines = []
    lines.append(f"## 量化多因子评分 — {name}({code})")
    lines.append(f"> 计算时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据截止: T-1日 | 零未来函数")
    lines.append(f"> ⚠️ 以下所有数值由 Python 脚本精确计算，AI 不需重新计算。")
    lines.append("")

    # 基本信息
    lines.append("### 基本信息")
    lines.append(f"- 现价: {quote['price']:.2f} | 今日涨跌: {quote['pct_change']:+.2f}%")
    lines.append(f"- PE(TTM): {quote['pe_ttm']:.2f} | PB: {quote['pb']:.2f} | 市值: {quote.get('total_mv_yi', 0):.1f}亿")
    lines.append(f"- RSI(14): {signals.get('rsi14', 'N/A')} | MACD: {signals.get('macd_trend', 'N/A')} | 层级: {tier}")
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
        lines.append(f"| 产业链归属 | AI链(光模块/PCB/存储/铜缆/液冷) | 2 | {ai_pts} | {'是' if m.get('is_ai_chain') else '否(可由CSV标注覆盖)'} |")
        lsr = m.get("long_short_ratio")
        lsr_str = f"{lsr:.2f}" if lsr and lsr != 999 else ("∞(长期涨短期跌)" if lsr == 999 else "N/A")
        lsr_pts = 2 if (lsr and lsr > 5) else (1 if (lsr and lsr >= 3) else 0)
        lines.append(f"| 长动比 | 1年涨幅÷3月涨幅 | 2 | {lsr_pts} | {lsr_str} |")
        d20_pts = 1 if (m.get("twenty_day_pct") is not None and m["twenty_day_pct"] > 0) else 0
        lines.append(f"| 20日趋势 | 20日%>0为1 | 1 | {d20_pts} | {m.get('twenty_day_pct','N/A')}% |")
        lines.append(f"| **动量总分** | | **8** | **{m.get('momentum_total',0)}** | {m.get('momentum_grade','')} |")
        lines.append("")

    # 因子评分表
    lines.append("### 多因子技术评分")
    lines.append("| 因子 | 满分 | 得分 | 依据 |")
    lines.append("|------|------|------|------|")
    for fname, fdata in score_result["factors"].items():
        lines.append(f"| {fname} | {fdata['max']} | {fdata['score']} | {fdata['reason']} |")
    lines.append(f"| **总分** | **100** | **{score_result['total']}** | |")
    lines.append("")
    lines.append(f"**可买度**: {score_result['buyability']}")
    lines.append("")

    # RSI 斜率/曲率（脚本计算）
    if rsi_detail and rsi_detail.get("data_available"):
        lines.append("### RSI 辅助参考（脚本计算，权重15%，不可独立决策）")
        lines.append(f"- RSI(14): {rsi_detail['rsi_current']} | 5日斜率: {rsi_detail['rsi_slope_5d']:+.1f} {rsi_detail['rsi_arrow']} | 曲率: {rsi_detail['rsi_curvature']:+.1f}")
        curv = rsi_detail.get("rsi_curvature", 0)
        if curv > 0.5:
            lines.append(f"- 曲率>0 → RSI趋势加速中（注意方向）")
        elif curv < -0.5:
            lines.append(f"- 曲率<0 → RSI趋势衰竭/减速中")
        lines.append("")

    # 基本面自动初评（脚本计算，供AI修正）
    if fundamental_auto and fundamental_auto.get("data_available"):
        lines.append("### 基本面自动初评（脚本计算，AI可修正）")
        lines.append(f"- 自动评分: ⭐{fundamental_auto['score']}/5")
        if fundamental_auto.get("breakdown"):
            for item in fundamental_auto["breakdown"]:
                lines.append(f"  - {item}")
        lines.append(f"- ⚠️ 此评分为机器初评，不含概念/行业地位判断，AI 需结合财报细节修正")
        lines.append("")

    # 成本修正（如有）
    if cost_result["enabled"]:
        lines.append("### 持仓成本修正")
        lines.append("| 指标 | 值 | 说明 |")
        lines.append("|------|-----|------|")
        lines.append(f"| 成本价 | {cost_result['cost_price']:.2f} 元 | 用户提供 |")
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

    # 必填提醒
    lines.append("---")
    lines.append("> ⚠️ **AI 必须继续输出以下内容（脚本不计算，需AI判断）：**")
    lines.append("> 1. **最优买入点**（突破买入价 / 触底买入价 + 触发条件）")
    lines.append("> 2. **止盈止损表**（具体价格，按标的等级分档）")
    lines.append("> 3. **概念⭐评分(1-5)** 和 **最终基本面⭐(1-5)**（可在自动初评基础上修正）")
    lines.append("> 4. **关键价位**（阻力位/支撑位）")
    lines.append("> 5. **操作理由+风险提示**")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 大盘风控
# ═══════════════════════════════════════════════════════════════════════════

def fetch_index_risk() -> dict:
    """获取上证指数状态，返回大盘风控建议。"""
    import requests
    import re

    # 实时行情
    try:
        r = requests.get("https://qt.gtimg.cn/q=sh000001", timeout=10)
        r.encoding = "gbk"
        m = re.search(r'="(.+?)"', r.text)
        if not m:
            return {"error": "无法获取上证行情"}
        f = m.group(1).split("~")
        idx_price = float(f[3]) if f[3] else 0
        idx_pct = float(f[32]) if f[32] else 0
    except Exception as e:
        return {"error": f"上证行情获取失败: {e}"}

    # 历史K线（计算MA60/MA200）
    try:
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,250,qfq"
        r2 = requests.get(url, timeout=15)
        data = r2.json()
        klines = data.get("data", {}).get("sh000001", {}).get("qfqday", [])
        if not klines:
            klines = data.get("data", {}).get("sh000001", {}).get("day", [])
        if not klines:
            return {"error": "无法获取上证K线"}

        closes = [float(k[2]) for k in klines if len(k) >= 6]

        ma60 = np.mean(closes[-61:-1]) if len(closes) >= 62 else None  # T-1日
        ma200 = np.mean(closes[-201:-1]) if len(closes) >= 202 else None

        last_close = closes[-2] if len(closes) >= 2 else idx_price  # T-1收盘

        # 仓位上限
        if ma60 and last_close > ma60:
            position_cap = 100
            status = "🟢 强势（上证>MA60）"
            max_stocks = "5-8只"
        elif ma200 and last_close > ma200:
            position_cap = 50
            status = "🟡 震荡（MA60>上证>MA200）"
            max_stocks = "2-3只"
        elif ma200:
            position_cap = 30
            status = "🔴 弱势（上证<MA200）"
            max_stocks = "1-2只（仅核心池最高分）"
        else:
            position_cap = 80
            status = "⚠️ 数据不足"
            max_stocks = "4-6只"
    except Exception as e:
        return {"error": f"K线计算失败: {e}", "idx_price": idx_price, "idx_pct": idx_pct}

    return {
        "idx_price": idx_price,
        "idx_pct": idx_pct,
        "ma60": round(ma60, 2) if ma60 else None,
        "ma200": round(ma200, 2) if ma200 else None,
        "last_close": round(last_close, 2),
        "status": status,
        "position_cap": position_cap,
        "max_stocks": max_stocks,
    }


def format_index_output(risk: dict) -> str:
    if "error" in risk:
        return f"⚠️ 大盘风控: {risk['error']}"
    lines = [
        "## 大盘风控",
        f"- 上证指数: {risk['idx_price']:.2f} ({risk['idx_pct']:+.2f}%)",
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

    # 量化打分
    score_result = calc_total_score(signals)

    # 仓位建议
    base_pct, base_label = calc_position_advice(score_result["total"], tier)

    # 成本修正
    cost_result = apply_cost_correction(signals, score_result["total"], base_pct, cost_price, shares, buy_date)

    # == 新增：动量因子 ==
    momentum = calc_momentum_factors(df, code)

    # == 新增：RSI斜率/曲率 ==
    closes_list = df["close"].tolist()
    rsi_detail = calc_rsi_slope_curvature(closes_list)

    # == 新增：基本面自动初评 ==
    if _HAS_AKSHARE:
        try:
            from stock_strategy import fetch_financial_data
            financials = fetch_financial_data(code)
        except Exception:
            financials = None
    else:
        financials = None
    fundamental_auto = calc_fundamental_auto_score(quote, financials)
    if financials:
        fundamental_auto["roe"] = round(financials["净资产收益率(ROE)"], 1) if financials.get("净资产收益率(ROE)") else None
        fundamental_auto["gross_margin"] = round(financials["毛利率"], 1) if financials.get("毛利率") else None
        fundamental_auto["net_margin"] = round(financials["销售净利率"], 1) if financials.get("销售净利率") else None
        fundamental_auto["revenue_yoy"] = round(financials["营业总收入同比"], 1) if financials.get("营业总收入同比") else None
        fundamental_auto["net_profit_yoy"] = round(financials["归母净利润同比增长率"], 1) if financials.get("归母净利润同比增长率") else None
        fundamental_auto["debt_ratio"] = round(financials["资产负债率"], 1) if financials.get("资产负债率") else None
        rev = financials.get("营业总收入") or 0
        fundamental_auto["cf_ratio"] = round(financials.get("经营现金流量净额", 0) / rev, 3) if rev > 0 and financials.get("经营现金流量净额") else None

    # == 新增：标的等级 ==
    f_star = fundamental_star if fundamental_star is not None else fundamental_auto.get("score")
    c_star = concept_star  # 概念⭐只能人工标注，不自动计算
    grade_info = calc_stock_grade(f_star, c_star)

    return {
        "code": code,
        "name": name,
        "tier": tier,
        "quote": {k: v for k, v in quote.items() if not isinstance(v, (np.ndarray, pd.DataFrame))},
        "signals": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in signals.items() if v is not None},
        "score": score_result,
        "cost_correction": cost_result,
        "momentum": momentum,
        "rsi_detail": rsi_detail,
        "fundamental_auto": fundamental_auto,
        "grade": grade_info,
    }


def main():
    parser = argparse.ArgumentParser(description="量化多因子打分引擎")
    parser.add_argument("code", nargs="?", help="股票代码")
    parser.add_argument("--cost", type=float, help="成本价")
    parser.add_argument("--shares", type=int, help="持仓数量")
    parser.add_argument("--buy-date", help="买入日期(YYYY-MM-DD)")
    parser.add_argument("--tier", default="核心", choices=["核心", "观察"], help="股票层级")
    parser.add_argument("--name", default="", help="股票名称")
    parser.add_argument("--kline-days", type=int, default=120, help="K线天数")
    parser.add_argument("--output", help="输出JSON文件")
    parser.add_argument("--pool", help="批量打分CSV文件")
    parser.add_argument("--no-index", action="store_true", help="不输出大盘风控")
    args = parser.parse_args()

    # 大盘风控
    if not args.no_index:
        index_risk = fetch_index_risk()
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
                    result.get("momentum"), result.get("rsi_detail"),
                    result.get("fundamental_auto"), result.get("grade"),
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
            result.get("momentum"), result.get("rsi_detail"),
            result.get("fundamental_auto"), result.get("grade"),
        ))

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=str)
            log.info(f"结果已保存至 {args.output}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
