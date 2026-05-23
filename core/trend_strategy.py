#!/usr/bin/env python3
"""
趋势追涨交易策略引擎
====================
基于"情绪周期+趋势动量"的A股追涨策略，整合GPT建议。

核心逻辑：
  1. 股票池过滤（ST/北交所/流动性不足/庄股）
  2. 市场情绪过滤器（赚钱效应判断）
  3. 综合强势评分（RSI+动量+量比+资金）
  4. 情绪高潮过滤（避免追在极端位置）
  5. 选股：排名11-30中取5只
  6. ATR动态止损
  7. 趋势移动止盈
  8. 情绪分档仓位管理

用法：
    from trend_strategy import TrendStrategy, get_ai_pool
    strategy = TrendStrategy()
    picks = strategy.select(df_pool, sentiment_level="强")
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# AI算力产业链股票池
# ═══════════════════════════════════════════════════════════════════════════

AI_CHAIN_CODES = {
    # GPU/算力芯片
    "688256", "688041", "688047", "688008", "603986", "688072",
    "688120", "688019", "300474", "002371",
    # 服务器/算力设备
    "601138", "000977", "603019", "002281", "300308",
    # 光模块/CPO
    "300502", "300308", "002281", "300394", "688498",
    "300570", "688205",
    # PCB
    "002463", "603228", "002916", "600183",
    # 散热/液冷
    "300684", "002837", "300499", "301018",
    # 存储芯片
    "603986", "688525", "002409", "688110",
    # 铜缆连接器
    "002475", "300136", "300433",
    # IDC/算力运营
    "600845", "300383", "300738", "000815",
    # 先进封装
    "002156", "600584", "002185",
    # 半导体材料
    "300346", "300666", "300655",
    # AI应用
    "688111", "002230", "300033", "600536", "002410",
    # 机器人/具身智能
    "300124", "002472", "688017", "300660",
    # 新能源算力
    "300750", "300014", "002459",
}

# 默认排除的板块
EXCLUDE_MARKETS_DEFAULT = ["北交所"]

# 默认排除的关键词（ST等）
EXCLUDE_NAME_KEYWORDS = ["ST", "*ST", "退"]


# ═══════════════════════════════════════════════════════════════════════════
# 策略核心
# ═══════════════════════════════════════════════════════════════════════════

class TrendStrategy:
    """
    趋势追涨策略。

    参数可自定义，以下为默认值（来自GPT建议的优化方案）。
    """

    def __init__(
        self,
        # 选股参数
        select_start_rank: int = 11,       # 从第几名开始选
        select_end_rank: int = 30,         # 到第几名截止
        num_picks: int = 5,                # 每期选几只
        # 评分权重（量能加速度体系）
        w_vol_ratio: float = 0.25,         # 量比（当前放量程度）
        w_vol_accel: float = 0.30,         # 量比加速度（量能是否在加速涌入）
        w_moneyflow: float = 0.25,         # 资金流向(CMF/OBV)
        w_momentum: float = 0.20,          # 价格动量（确认上涨方向）
        # 高潮过滤
        max_consecutive_limit_up: int = 3,
        max_pct_5d: float = 35.0,
        max_ma20_distance: float = 35.0,
        # 流动性过滤
        min_turnover_rate: float = 5.0,    # 最低换手率(%)
        min_vol_ratio: float = 1.0,        # 最低量比（必须放量）
        # ATR止损止盈
        atr_stop_mult: float = 1.5,
        atr_trail_mult: float = 2.0,
    ):
        self.select_start_rank = select_start_rank
        self.select_end_rank = select_end_rank
        self.num_picks = num_picks
        self.w_vol_ratio = w_vol_ratio
        self.w_vol_accel = w_vol_accel
        self.w_moneyflow = w_moneyflow
        self.w_momentum = w_momentum
        self.max_consecutive_limit_up = max_consecutive_limit_up
        self.max_pct_5d = max_pct_5d
        self.max_ma20_distance = max_ma20_distance
        self.min_turnover_rate = min_turnover_rate
        self.min_vol_ratio = min_vol_ratio
        self.atr_stop_mult = atr_stop_mult
        self.atr_trail_mult = atr_trail_mult

    # ------------------------------------------------------------------
    # 1. 量能加速度综合评分
    # ------------------------------------------------------------------

    def calc_strength_score(self, row: dict) -> float:
        """
        量能加速度评分体系（零RSI，纯量能+资金驱动）

        综合评分 = 0.25×量比_norm + 0.30×量比加速度_norm + 0.25×资金_norm + 0.20×动量_norm

        row 应包含:
          - vol_ratio: 5日均量/20日均量
          - vol_ratio_prev: 上一期(5日前)的量比
          - cmf_20d: Chaikin Money Flow [-1,1]
          - obv_trend: OBV趋势方向，"多头"/"空头"/"震荡"
          - pct_5d: 5日涨跌幅(%)
        """
        vol_ratio = row.get("vol_ratio", 1.0) or 1.0
        vol_ratio_prev = row.get("vol_ratio_prev", 1.0) or 1.0
        cmf = row.get("cmf_20d", 0) or 0
        obv_trend = row.get("obv_trend", "")
        pct_5d = row.get("pct_5d", 0)

        # ---- 因子1: 量比（当前放量程度，0-100）----
        # 量比1.0→30分(及格), 2.0→80分(显著放量), 3.5+→100分(爆量)
        if vol_ratio >= 1.0:
            vol_norm = min(100, 30 + (vol_ratio - 1.0) / 2.5 * 70)
        else:
            vol_norm = max(0, vol_ratio / 1.0 * 30)

        # ---- 因子2: 量比加速度（量能是否在加速涌入，0-100）----
        # 量比从1.5→2.5 = 大量涌入且加速，得分最高
        # 量比从3.0→2.0 = 量能在萎缩，得分低
        if vol_ratio_prev > 0:
            vol_change = (vol_ratio - vol_ratio_prev) / vol_ratio_prev  # 量比变化率
        else:
            vol_change = 0

        # 映射：变化率-30%→0分, 0%→40分, +50%→100分
        vol_accel = min(100, max(0, 40 + vol_change / 0.5 * 60))

        # 加分项：量比绝对值高 且 还在加速 → 额外奖励
        if vol_ratio >= 2.0 and vol_change > 0.1:
            vol_accel = min(100, vol_accel + 10)

        # 扣分项：量比高但已减速 → 出货嫌疑
        if vol_ratio >= 2.5 and vol_change < -0.15:
            vol_accel = max(0, vol_accel - 20)

        # ---- 因子3: 资金流向（0-100）----
        # CMF: 0→40分, 0.2→90分, -0.2→0分
        cmf_norm = min(100, max(0, 40 + cmf / 0.2 * 50))

        # OBV趋势加成/减成（±15分）
        if obv_trend == "多头":
            cmf_norm = min(100, cmf_norm + 15)
        elif obv_trend == "空头":
            cmf_norm = max(0, cmf_norm - 20)

        # ---- 因子4: 价格动量（确认方向，0-100）----
        # 量能涌入需要有价格上涨配合，否则是放量滞涨（危险）
        if pct_5d >= 3:
            momentum_norm = min(100, 50 + (pct_5d - 3) / 12 * 50)
        elif pct_5d >= -3:
            momentum_norm = max(0, 50 - abs(pct_5d) / 3 * 50)
        else:
            momentum_norm = 0  # 放量下跌，严重扣分

        # 放量滞涨惩罚：量比>2但涨幅<1% → 资金可能在出逃
        if vol_ratio > 2.0 and pct_5d < 1.0:
            momentum_norm = max(0, momentum_norm - 30)

        total = (
            self.w_vol_ratio * vol_norm
            + self.w_vol_accel * vol_accel
            + self.w_moneyflow * cmf_norm
            + self.w_momentum * momentum_norm
        )
        return round(total, 1)

    # ------------------------------------------------------------------
    # 2. 高潮过滤器
    # ------------------------------------------------------------------

    def is_climax(self, row: dict) -> tuple[bool, str]:
        """
        判断是否处于情绪高潮（应避免买入）。

        量能版高潮信号：
          - 连续涨停过多
          - 短期涨幅过大
          - 距均线过远
          - 天量天价（量比>4 但涨幅<1%）→ 放量滞涨，出货信号
          - 量能脉冲衰退（量比>3 但量比加速度<-0.3）→ 爆量后急剧缩量
        """
        reasons = []

        cons_up = row.get("consecutive_limit_up", 0)
        if cons_up >= self.max_consecutive_limit_up:
            reasons.append(f"连续涨停{cons_up}天")

        pct_5d = row.get("pct_5d", 0)
        if pct_5d > self.max_pct_5d:
            reasons.append(f"5日涨幅{pct_5d:.1f}%>{self.max_pct_5d}%")

        price = row.get("close", 0)
        ma20 = row.get("ma20", 0) or 0
        if ma20 > 0:
            dist = (price / ma20 - 1) * 100
            if dist > self.max_ma20_distance:
                reasons.append(f"距MA20={dist:.1f}%>{self.max_ma20_distance}%")

        # 天量天价：量比极大但价格不涨 → 出货
        vol_ratio = row.get("vol_ratio", 1.0) or 1.0
        if vol_ratio > 4.0 and pct_5d < 1.0:
            reasons.append(f"天量滞涨(量比{vol_ratio:.1f}, 涨幅{pct_5d:.1f}%)")

        # 量能脉冲衰退：前一期爆量，当前急剧缩量
        vol_ratio_prev = row.get("vol_ratio_prev", 1.0) or 1.0
        if vol_ratio_prev > 3.0 and vol_ratio_prev > 0:
            change = (vol_ratio - vol_ratio_prev) / vol_ratio_prev
            if change < -0.3:
                reasons.append(f"量能脉冲衰退(量比{vol_ratio_prev:.1f}→{vol_ratio:.1f}, {change*100:.0f}%)")

        if reasons:
            return True, " | ".join(reasons)
        return False, ""

    # ------------------------------------------------------------------
    # 3. 流动性过滤
    # ------------------------------------------------------------------

    def has_sufficient_liquidity(self, row: dict) -> bool:
        """检查换手率 且 量比必须大于阈值（没放量的不追）"""
        turnover = row.get("turnover_rate", 0)
        vol_ratio = row.get("vol_ratio", 0) or 0
        return turnover >= self.min_turnover_rate and vol_ratio >= self.min_vol_ratio

    # ------------------------------------------------------------------
    # 4. 选股主方法
    # ------------------------------------------------------------------

    def select(self, pool_df: pd.DataFrame) -> list[dict]:
        """
        选股流程：高潮过滤 → 流动性过滤 → 量能加速度评分 → 排名11-30中取Top N
        """
        rows = []
        for _, row in pool_df.iterrows():
            d = row.to_dict()

            # 高潮过滤
            is_climax, climax_reason = self.is_climax(d)
            if is_climax:
                continue

            # 流动性过滤
            if not self.has_sufficient_liquidity(d):
                continue

            # 计算强度评分
            score = self.calc_strength_score(d)
            d["strength_score"] = score
            rows.append(d)

        # 按强度评分降序排列
        rows.sort(key=lambda x: x["strength_score"], reverse=True)

        # 从排名区间内选取
        start = self.select_start_rank - 1
        end = min(self.select_end_rank, len(rows))
        candidates = rows[start:end] if start < len(rows) else []

        # 取 Top num_picks
        picks = candidates[:self.num_picks]

        log.info("选股结果: 池%d只 → 过滤后%d只 → 排名%d-%d区间%d只 → 选中%d只",
                 len(pool_df), len(rows),
                 self.select_start_rank, self.select_end_rank,
                 len(candidates), len(picks))

        return [
            {
                "code": p["code"],
                "name": p.get("name", p["code"]),
                "score": p["strength_score"],
                "close": p["close"],
                "atr14": p.get("atr14", 0),
                "ma20": p.get("ma20", 0),
            }
            for p in picks
        ]

    # ------------------------------------------------------------------
    # 5. 仓位管理
    # ------------------------------------------------------------------

    def get_position_ratio(self) -> float:
        """固定满仓运行（策略靠选股和止损控制风险）"""
        return 1.0

    # ------------------------------------------------------------------
    # 6. 止盈止损
    # ------------------------------------------------------------------

    def calc_stop_price(self, entry_price: float, atr14: float) -> float:
        """ATR动态止损价 = 买入价 - 1.5×ATR"""
        if atr14 <= 0:
            return entry_price * 0.94  # 备用：-6%
        return entry_price - self.atr_stop_mult * atr14

    def calc_trail_stop(self, highest_price: float, atr14: float) -> float:
        """移动止盈线 = 最高价 - 2×ATR"""
        if atr14 <= 0:
            return highest_price * 0.92  # 备用：-8%回撤
        return highest_price - self.atr_trail_mult * atr14


# ═══════════════════════════════════════════════════════════════════════════
# 股票池工具
# ═══════════════════════════════════════════════════════════════════════════

def get_ai_pool(exclude_markets: list[str] = None) -> list[str]:
    """获取AI算力产业链股票池"""
    if exclude_markets is None:
        exclude_markets = EXCLUDE_MARKETS_DEFAULT

    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from market_filter import get_market

    codes = []
    for c in AI_CHAIN_CODES:
        market = get_market(c)
        if exclude_markets and market in exclude_markets:
            continue
        codes.append(c)
    return sorted(codes)
