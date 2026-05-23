#!/usr/bin/env python3
"""
本地点对点回测引擎（零依赖聚宽平台）
=====================================
基于投资项目的量化多因子打分模型，对历史K线数据逐日模拟打分+调仓，
输出完整的回测绩效报告。

核心逻辑（与项目保持一致）：
  - 打分：trend(25) + position(25) + volume_price(20) + rsi(15) + volatility(15) + extra(±13)
  - 选股：按总得分降序取 Top N，排除PE>200等基本面硬伤
  - 调仓：每M个交易日轮动一次，等权分配
  - 止盈止损：回撤止盈 + 固定止损 + 均线止损

数据源：腾讯前复权K线（完全本地，不依赖聚宽/akshare）

用法：
    python backtest.py                          # 默认参数回测
    python backtest.py --start 2024-01-01       # 指定起始日期
    python backtest.py --top 10 --rebalance 5   # Top10选股，5日轮动
    python backtest.py --plot                   # 输出收益曲线图

输出：
    终端打印：年化收益、夏普比率、最大回撤、胜率等
    output/backtest_*.csv：逐日净值
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

# 确保可以 import core/ 下的模块
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from core.stock_strategy import (
    fetch_daily_kline,
    compute_indicators,
    get_latest_signals,
)

from core.quant_score import (
    calc_total_score,
    calc_momentum_factors,
)

from core.market_filter import get_market

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backtest")

# ============================================================================
# 配置
# ============================================================================

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "output", "backtest")

# 默认参数
DEFAULT_START = "2024-01-01"
DEFAULT_END = None          # None = 今天
DEFAULT_TOP_N = 10           # 持仓数
DEFAULT_REBALANCE_DAYS = 5   # 调仓周期（交易日）
# 多因子.py 借鉴项：
# - 保本/保利多级止损体系
# - 买入次日不风控（给1天观察期）
# - 最小持有天数约束（防频繁换仓）
# - 可选放弃涨幅第一名（防追高）
DEFAULT_LOOKBACK_DAYS = 300  # 打分所需K线天数
DEFAULT_INIT_CASH = 1_000_000  # 初始资金（元）

# 交易成本
COMMISSION_RATE = 0.0003      # 佣金万分之三
STAMP_TAX_RATE = 0.001        # 卖出印花税千分之一
MIN_COMMISSION = 5            # 最低佣金
SLIPPAGE = 0.001              # 滑点 0.1%

# 止盈止损（多级体系）
STOP_LOSS_PCT = 0.08          # 固定止损 -8%
TRAIL_STOP_PCT = 0.10         # 回撤止盈：从最高点回撤10%
TAKE_PROFIT_PCT = 0.30        # 固定止盈 +30%
PROTECT_BREAKEVEN = True      # 保本止损：盈利>3%后回落到成本价即卖（借鉴多因子.py）
PROTECT_PROFIT_PCT = 0.05     # 保利止损：盈利5-10%后回落到+5%即卖
MA_STOP_SHORT = 5             # 短期均线止损：盈利>10%后跌破MA5卖出
MA_STOP_LONG = 20             # 长期均线止损：持有>10天跌破MA20卖出
SKIP_TOP_GAINER = True        # 放弃20日涨幅第1名（借鉴多因子.py，防追高）
MIN_HOLD_DAYS = 5             # 最小持有天数（未满此天数不轮动卖出）
RISK_FREE_NEXT_DAY = True     # 买入次日不触发止损止盈（给1天观察期）

# 基本面快速过滤
MAX_PE = 200
EXCLUDE_ST = True             # 排除ST
EXCLUDE_NEW_STOCK = True      # 排除上市<60天的股票（数据不足）


# ============================================================================
# 1. 历史数据层：按日期切片，模拟"当日已知数据"
# ============================================================================

class DataFeed:
    """
    按回测日期管理K线数据的"时间机器"。
    每次调用 get_data(code, as_of_date) 只返回 as_of_date 当天及之前的数据，
    严格杜绝未来函数。
    """

    def __init__(self, stock_codes: list[str], lookback_days: int = 300):
        self.codes = stock_codes
        self.lookback = lookback_days
        self._cache: dict[str, pd.DataFrame] = {}  # code -> 全量K线
        self.names: dict[str, str] = {}             # code -> 中文名称

    def preload(self):
        """预加载所有股票的完整K线及名称"""
        total = len(self.codes)
        log.info("预加载 %d 只股票的历史K线及名称...", total)

        # 批量获取名称（通过腾讯行情）
        import requests, time
        batch_size = 50
        for i in range(0, total, batch_size):
            batch = self.codes[i:i + batch_size]
            symbols = []
            for c in batch:
                prefix = "sh" if c.startswith(("6", "9")) else "sz"
                symbols.append(f"{prefix}{c}")
            try:
                url = "http://qt.gtimg.cn/q=" + ",".join(symbols)
                resp = requests.get(url, timeout=10)
                resp.encoding = "gbk"
                for line in resp.text.strip().split("\n"):
                    if "~" not in line:
                        continue
                    parts = line.split('"')
                    if len(parts) < 2:
                        continue
                    fields = parts[1].split("~")
                    if len(fields) >= 2:
                        code = fields[2]
                        name = fields[1]
                        self.names[code] = name
            except Exception:
                pass
            if i + batch_size < total:
                time.sleep(0.05)

        for i, code in enumerate(self.codes):
            if (i + 1) % 50 == 0:
                log.info("  加载进度: %d/%d", i + 1, total)
            try:
                df = fetch_daily_kline(code, days=500)
                if df is not None and len(df) >= 60:
                    self._cache[code] = df
            except Exception:
                pass
        log.info("预加载完成，有效: %d/%d", len(self._cache), total)

    def get(self, code: str, as_of_date) -> Optional[pd.DataFrame]:
        """获取截至 as_of_date（含当日）的K线数据"""
        df = self._cache.get(code)
        if df is None:
            return None
        # 严格截断：只保留 as_of_date 及之前的数据
        mask = df["date"] <= pd.Timestamp(as_of_date)
        sliced = df[mask].copy()
        if len(sliced) < 60:
            return None
        return sliced.tail(self.lookback)


def _normalize_date(d) -> pd.Timestamp:
    """统一日期格式"""
    return pd.Timestamp(d)


# ============================================================================
# 2. 选股+打分（每期执行一次）
# ============================================================================

def score_universe(datafeed: DataFeed, as_of_date, top_n: int) -> list[dict]:
    """
    在 as_of_date 当天，对全股票池打分，返回 Top N。
    返回 list[dict]，每项含 code, score, price 等。
    """
    rankings = []
    for code in datafeed._cache:  # 遍历所有可交易股票
        df = datafeed.get(code, as_of_date)
        if df is None:
            continue

        try:
            df = compute_indicators(df)
            price = float(df["close"].iloc[-1])
            if price <= 0:
                continue

            # 排除ST（名称含ST）
            # 这里简化：跳过日线数据异常的

            signals = get_latest_signals(df, {"current_price": price})
            score_result = calc_total_score(signals)
            total = score_result["total"]

            rankings.append({
                "code": code,
                "name": datafeed.names.get(code, code),
                "score": total,
                "price": price,
                "signals": signals,
            })
        except Exception:
            continue

    rankings.sort(key=lambda x: x["score"], reverse=True)
    # 可选：放弃第一名（借鉴多因子.py，防追高）
    if SKIP_TOP_GAINER and len(rankings) > top_n:
        rankings = rankings[1:]
    return rankings[:top_n]


# ============================================================================
# 3. 组合管理 + 交易模拟
# ============================================================================

class Portfolio:
    """管理现金、持仓、净值，打印详细调仓日志"""

    def __init__(self, init_cash: float):
        self.init_cash = init_cash
        self.cash = init_cash
        self.positions: dict[str, dict] = {}  # code -> {shares, avg_cost, highest_price, current_price, buy_date}
        self.nav_history: list[dict] = []
        self.benchmark_nav: list[dict] = []   # 上证指数基准净值序列
        self.trade_log: list[dict] = []
        self._rebalance_count = 0
        self.stock_names: dict[str, str] = {}  # code -> name（每期更新）

    @property
    def total_value(self) -> float:
        pos_value = sum(
            p.get("shares", 0) * p.get("current_price", 0)
            for p in self.positions.values()
        )
        return self.cash + pos_value

    def record_nav(self, date, prices: dict[str, float]):
        """记录当日净值，并返回当日收益率(%)"""
        pos_value = 0.0
        for code, pos in list(self.positions.items()):
            price = prices.get(code)
            if price:
                pos["current_price"] = price
                pos_value += pos["shares"] * price
                if price > pos.get("highest_price", 0):
                    pos["highest_price"] = price

        total = self.cash + pos_value
        prev_total = self.nav_history[-1]["total_value"] if self.nav_history else self.init_cash
        daily_return = (total / prev_total - 1) * 100 if prev_total > 0 else 0

        self.nav_history.append({
            "date": str(date.date()) if hasattr(date, "date") else str(date),
            "total_value": total,
            "cash": self.cash,
            "positions_value": pos_value,
            "num_positions": len(self.positions),
            "daily_return": round(daily_return, 2),
        })
        return daily_return

    # ------------------------------------------------------------------
    # 调仓主方法
    # ------------------------------------------------------------------

    def execute_rebalance(self, target_stocks: list[dict], prices: dict[str, float],
                          date):
        """
        执行调仓：止损/止盈检查 → 卖出不在目标池的 → 买入目标池等权。
        打印完整调仓报告。
        """
        today = str(date.date()) if hasattr(date, "date") else str(date)
        self._rebalance_count += 1
        total_value_before = self.total_value
        cash_before = self.cash
        holdings_before = set(self.positions.keys())

        # ---- 更新股票名称映射 ----
        for s in target_stocks:
            self.stock_names[s["code"]] = s.get("name", s["code"])

        # ================================================================
        # 调仓报告头部
        # ================================================================
        _print_header(self._rebalance_count, today, total_value_before, cash_before,
                      len(self.positions))

        # ================================================================
        # 调仓前持仓明细
        # ================================================================
        _print_holdings(self, prices, "调仓前持仓")

        # ================================================================
        # 新一期打分排名
        # ================================================================
        _print_rankings(target_stocks, holdings_before)

        # ================================================================
        # Step 1: 止损/止盈检查（多级体系）
        # ================================================================
        for code, pos in list(self.positions.items()):
            price = prices.get(code)
            if not price or pos["shares"] <= 0:
                continue

            loss_pct = (price - pos["avg_cost"]) / pos["avg_cost"]
            highest = pos.get("highest_price", pos["avg_cost"])
            buy_date_str = pos.get("buy_date", today)

            # 买入次日不风控（借鉴多因子.py）
            if RISK_FREE_NEXT_DAY:
                try:
                    bd = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
                    td = datetime.strptime(today, "%Y-%m-%d").date()
                    if (td - bd).days <= 1:
                        continue
                except Exception:
                    pass

            # ---- 1) 固定止损 -8% ----
            if loss_pct <= -STOP_LOSS_PCT:
                _print_stop("🔴 固定止损", code, self.stock_names.get(code, code),
                             pos, price, loss_pct, today, "亏损超过8%")
                self._sell(code, pos["shares"], price, today, "止损")
                continue

            # ---- 2) 回撤止盈 -10%（曾盈利>5%才检查）----
            if highest > pos["avg_cost"] * 1.05:
                drawdown = (highest - price) / highest
                if drawdown >= TRAIL_STOP_PCT:
                    profit_from_high = (highest - pos["avg_cost"]) / pos["avg_cost"]
                    extra = f"最高价={highest:.2f}(+{profit_from_high*100:.1f}%) 回撤={drawdown*100:.1f}%"
                    _print_stop("🟡 回撤止盈", code, self.stock_names.get(code, code),
                                 pos, price, loss_pct, today, extra)
                    self._sell(code, pos["shares"], price, today, "回撤止盈")
                    continue

            # ---- 3) 固定止盈 +30% ----
            if loss_pct >= TAKE_PROFIT_PCT:
                _print_stop("🟢 固定止盈", code, self.stock_names.get(code, code),
                             pos, price, loss_pct, today, f"盈利{loss_pct*100:.1f}%，触发止盈")
                self._sell(code, pos["shares"], price, today, "止盈")
                continue

            # ---- 4) 保本止损（借鉴多因子.py）：盈利>3%后回落到成本价 ----
            if PROTECT_BREAKEVEN and highest > pos["avg_cost"] * 1.03 and price <= pos["avg_cost"] * 1.002:
                _print_stop("🟠 保本止损", code, self.stock_names.get(code, code),
                             pos, price, loss_pct, today,
                             f"曾盈利>3%(最高{highest:.2f})→回落到成本价{pos['avg_cost']:.2f}")
                self._sell(code, pos["shares"], price, today, "保本止损")
                continue

            # ---- 5) 保利止损（借鉴多因子.py）：盈利5-10%后回落到+5% ----
            if PROTECT_PROFIT_PCT > 0 and PROTECT_PROFIT_PCT <= loss_pct < 0.10                and highest > pos["avg_cost"] * 1.10                and price <= pos["avg_cost"] * (1 + PROTECT_PROFIT_PCT):
                _print_stop("🟡 保利止损", code, self.stock_names.get(code, code),
                             pos, price, loss_pct, today,
                             f"曾盈利>10%(最高{highest:.2f})→回落到+{PROTECT_PROFIT_PCT*100:.0f}%")
                self._sell(code, pos["shares"], price, today, "保利止损")
                continue

            # ---- 6) MA5短期止损（盈利>10%后使用，借鉴多因子.py）----
            if loss_pct >= 0.10 and MA_STOP_SHORT > 0:
                ma_val = _compute_ma_for_code(code, MA_STOP_SHORT, prices, self)
                if ma_val and price < ma_val:
                    _print_stop("🔵 MA5止损", code, self.stock_names.get(code, code),
                                 pos, price, loss_pct, today,
                                 f"盈利>10% → 跌破MA{MA_STOP_SHORT}={ma_val:.2f}")
                    self._sell(code, pos["shares"], price, today, "MA5止损")
                    continue

            # ---- 7) MA20长期止损（持有>10天，借鉴原逻辑）----
            if MA_STOP_LONG > 0:
                try:
                    bd = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
                    td = datetime.strptime(today, "%Y-%m-%d").date()
                    if (td - bd).days >= 10:
                        ma_val = _compute_ma_for_code(code, MA_STOP_LONG, prices, self)
                        if ma_val and price < ma_val:
                            _print_stop("🔵 MA20止损", code, self.stock_names.get(code, code),
                                         pos, price, loss_pct, today,
                                         f"持有>10天 → 跌破MA{MA_STOP_LONG}={ma_val:.2f}")
                            self._sell(code, pos["shares"], price, today, "MA20止损")
                            continue
                except Exception:
                    pass

        # ================================================================
        # Step 2: 确定买卖清单（含最小持有天数约束）
        # ================================================================
        target_codes = {s["code"] for s in target_stocks}
        cur_holdings = {c for c, p in self.positions.items() if p["shares"] > 0}
        to_sell = []
        for c in cur_holdings:
            if c not in target_codes:
                pos = self.positions[c]
                buy_date_str = pos.get("buy_date", today)
                can_sell = True
                if MIN_HOLD_DAYS > 0:
                    try:
                        bd = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
                        td = datetime.strptime(today, "%Y-%m-%d").date()
                        if (td - bd).days < MIN_HOLD_DAYS:
                            can_sell = False
                    except Exception:
                        pass
                if can_sell:
                    to_sell.append(c)
        to_keep = [c for c in cur_holdings if c in target_codes]
        to_buy = [s for s in target_stocks if s["code"] not in cur_holdings]

        # ---- 打印调仓动作 ----
        _print_trade_actions(to_sell, to_buy, self.positions, prices, target_stocks, self.stock_names)

        # ================================================================
        # Step 3: 执行卖出
        # ================================================================
        for code in to_sell:
            pos = self.positions.get(code)
            if pos and pos["shares"] > 0:
                price = prices.get(code, pos.get("current_price", 0))
                if price > 0:
                    name = self.stock_names.get(code, code)
                    cost = pos["avg_cost"]
                    pnl_pct = (price - cost) / cost * 100 if cost > 0 else 0
                    revenue = pos["shares"] * price * (1 - COMMISSION_RATE - STAMP_TAX_RATE)
                    log.info("  [执行] 卖出 %s %s  %d股 @%.2f  盈亏%+.2f%%  回收%s元  (原因: 轮动淘汰)",
                             code, name, pos["shares"], price, pnl_pct, _fmt_amount(revenue))
                    self._sell(code, pos["shares"], price, today, "轮动卖出")

        # ================================================================
        # Step 4: 买入
        # ================================================================
        total_slots = len(to_keep) + len(to_buy)
        if total_slots == 0:
            log.info("  ⚠️ 无可买入标的，调仓结束")
            return

        # 用卖出后的最新现金计算（借鉴多因子.py先卖后买）
        per_stock_cash = self.total_value / total_slots

        for stock in to_buy:
            price = prices.get(stock["code"], stock.get("price", 0))
            if price <= 0:
                continue
            avail = min(per_stock_cash, self.cash * 0.95)
            shares = int(avail / price / 100) * 100
            if shares < 100:
                log.info("  [跳过] %s 资金不足100股 (单价%.2f)", stock["code"], price)
                continue

            cost = shares * price * (1 + COMMISSION_RATE) + max(MIN_COMMISSION, shares * price * COMMISSION_RATE)
            if cost > self.cash:
                shares = int((self.cash * 0.95) / price / 100) * 100
                if shares < 100:
                    continue
                cost = shares * price * (1 + COMMISSION_RATE) + max(MIN_COMMISSION, shares * price * COMMISSION_RATE)

            self.cash -= cost
            self.positions[stock["code"]] = {
                "shares": shares,
                "avg_cost": price,
                "highest_price": price,
                "current_price": price,
                "buy_date": today,
            }
            self.trade_log.append({
                "date": today, "code": stock["code"], "action": "BUY",
                "shares": shares, "price": price, "cost": cost, "reason": "调仓买入",
            })
            log.info("  [执行] 买入 %s %s  %d股 @%.2f  成本%s元",
                     stock["code"], stock.get("name", ""), shares, price, _fmt_amount(cost))

        # ================================================================
        # 调仓后预计持仓
        # ================================================================
        _print_post_rebalance(self, target_codes, to_keep, to_buy, total_slots)

        # ================================================================
        # 汇总
        # ================================================================
        total_value_after = self.total_value
        log.info("")
        log.info("  📊 调仓汇总: 资产 %s→%s (%+.2f%%) | 现金 %s→%s | 持仓 %d→%d只",
                 _fmt_amount(total_value_before), _fmt_amount(total_value_after),
                 (total_value_after / total_value_before - 1) * 100,
                 _fmt_amount(cash_before), _fmt_amount(self.cash),
                 len(holdings_before), len(self.positions))
        log.info("  ✅ 调仓完成")
        log.info("=" * 60)

    # ------------------------------------------------------------------
    # 卖出
    # ------------------------------------------------------------------

    def _sell(self, code: str, shares: int, price: float, date: str, reason: str):
        pos = self.positions.get(code)
        if not pos or shares <= 0:
            return
        actual_shares = min(shares, pos["shares"])
        revenue = actual_shares * price * (1 - COMMISSION_RATE - STAMP_TAX_RATE)
        revenue -= max(MIN_COMMISSION, actual_shares * price * COMMISSION_RATE)
        self.cash += revenue

        self.trade_log.append({
            "date": date, "code": code, "action": "SELL",
            "shares": actual_shares, "price": price, "revenue": revenue, "reason": reason,
        })

        pos["shares"] -= actual_shares
        if pos["shares"] <= 0:
            del self.positions[code]


# ============================================================================
# 日志打印辅助函数
# ============================================================================

def _fmt_amount(val: float) -> str:
    """格式化金额"""
    if abs(val) >= 1e8:
        return f"{val/1e8:.2f}亿"
    elif abs(val) >= 1e4:
        return f"{val/1e4:.0f}万"
    else:
        return f"{val:,.0f}"

def _print_header(n: int, today: str, total: float, cash: float, pos_count: int):
    total_s = _fmt_amount(total)
    cash_s = _fmt_amount(cash)
    # 计算 padding 让日期和期数居中
    title = f"📊 调仓报告  {today}  第{n}期"
    pad = max(0, 56 - len(title))
    log.info("")
    log.info("╔" + "═" * 58 + "╗")
    log.info(f"║  {title}{' ' * pad}║")
    log.info("╠" + "═" * 58 + "╣")
    log.info(f"║  总资产: {total_s:>12s}  |  现金: {cash_s:>10s}  |  持仓: {pos_count}只" + " " * 8 + "║")
    log.info("╚" + "═" * 58 + "╝")
    log.info("")

def _print_holdings(pf: Portfolio, prices: dict[str, float], title: str):
    """打印当前持仓明细表"""
    codes = [c for c, p in pf.positions.items() if p["shares"] > 0]
    if not codes:
        log.info("  %s: (空仓)", title)
        return

    log.info("  %s:", title)
    log.info("  %-10s %8s %8s %8s %8s %5s %8s",
             "代码", "成本价", "现价", "盈亏%", "市值", "持仓天", "最高价")
    log.info("  " + "-" * 65)

    total_pos = 0.0
    total_pnl = 0.0
    today_str = pf.nav_history[-1]["date"] if pf.nav_history else ""

    for code in codes:
        pos = pf.positions[code]
        price = prices.get(code, pos.get("current_price", 0))
        cost = pos["avg_cost"]
        pnl_pct = (price - cost) / cost * 100 if cost > 0 else 0
        pos_value = pos["shares"] * price
        pnl_amt = (price - cost) * pos["shares"]
        highest = pos.get("highest_price", cost)
        # 持仓天数
        buy_date = pos.get("buy_date", today_str)
        try:
            bd = datetime.strptime(buy_date, "%Y-%m-%d").date()
            td = datetime.strptime(today_str, "%Y-%m-%d").date()
            hold_days = (td - bd).days
        except Exception:
            hold_days = 0
        total_pos += pos_value
        total_pnl += pnl_amt

        log.info("  %-10s %8.2f %8.2f %+7.2f%% %8s %4d天 %8.2f",
                 code, cost, price, pnl_pct, _fmt_amount(pos_value), hold_days, highest)

    log.info("  " + "-" * 65)
    log.info("  持仓市值合计: %s  |  浮动盈亏: %+s", _fmt_amount(total_pos), _fmt_amount(total_pnl))



def _compute_ma_for_code(code, period, prices, pf):
    """从持仓和净值历史中估算MA值（简化版：取最近N日收盘价均值）"""
    nav_list = pf.nav_history
    if len(nav_list) < period:
        return None
    # 从净值历史中无法直接拿到个股close序列，这里简化处理：
    # 用当前价和之前记录价的差值估一个趋势，不做精确MA计算
    # 实际回测中调仓时已经通过 prices 字典得到收盘价，这里只用 prices 近似
    return None  # 回测简化：不启用（因为没有历史逐日个股close序列）
def _print_daily_summary(pf, date, prices, daily_return, is_rebalance_day):
    """每个交易日收盘后打印持仓和当日收益摘要"""
    nav = pf.nav_history[-1] if pf.nav_history else {}
    total = nav.get("total_value", pf.total_value)
    cash = pf.cash
    cum_return = (total / pf.init_cash - 1) * 100
    date_str = str(date.date()) if hasattr(date, "date") else str(date)

    # 基准收益
    bench_ret_str = ""
    if pf.benchmark_nav:
        for bm in pf.benchmark_nav:
            if bm["date"] == date_str:
                bench_ret_str = f"  基准: {bm['return_pct']:+.2f}%"
                break

    tag = " [调仓日]" if is_rebalance_day else ""

    log.info("")
    tag_info = " 调仓日" if is_rebalance_day else ""
    log.info("  +" + "-" * 54 + " " + date_str + tag_info + " " + "-" * 7 + "+")

    codes = [c for c, p in pf.positions.items() if p["shares"] > 0]
    if codes:
        log.info("  |  %-8s %-6s %8s %8s %8s %8s %5s |",
                 "代码", "名称", "成本", "现价", "盈亏%", "市值", "天")
        log.info("  |" + "-" * 68 + "|")
        for code in codes:
            pos = pf.positions[code]
            price = prices.get(code, pos.get("current_price", 0))
            cost = pos["avg_cost"]
            pnl_pct = (price - cost) / cost * 100 if cost > 0 else 0
            pos_value = pos["shares"] * price
            buy_date = pos.get("buy_date", date_str)
            try:
                bd = datetime.strptime(buy_date, "%Y-%m-%d").date()
                td = datetime.strptime(date_str, "%Y-%m-%d").date()
                hold_days = (td - bd).days
            except Exception:
                hold_days = 0
            name = pf.stock_names.get(code, "")[:6]
            log.info("  |  %-8s %-6s %8.2f %8.2f %+7.2f%% %8s %4d天  |",
                     code, name, cost, price, pnl_pct,
                     _fmt_amount(pos_value), hold_days)
        log.info("  |" + "-" * 68 + "|")

    log.info("  |  总资产: %10s  |  现金: %10s  |  持仓: %d只       |",
             _fmt_amount(total), _fmt_amount(cash), len(codes))
    log.info("  |  当日收益: %+7.2f%%  |  累计收益: %+7.2f%%%s                     |",
             daily_return, cum_return, bench_ret_str)
    log.info("  +" + "-" * 68 + "+")


def _print_rankings(target_stocks: list[dict], holdings_before: set):
    """打印新一期打分排名"""
    log.info("")
    log.info("  🏆 新一期打分 Top%d (满分100):", len(target_stocks))
    log.info("  %-4s %-10s %6s %6s %6s %6s %6s %6s %5s",
             "排名", "代码", "总分", "趋势", "位置", "量价", "RSI", "波动", "附加")
    log.info("  " + "-" * 60)

    from core.quant_score import score_trend, score_position, score_volume_price, score_rsi, score_volatility, score_bonus

    for i, r in enumerate(target_stocks):
        s = r.get("signals", {})
        s_trend = round(score_trend(s)[0], 1)
        s_pos = round(score_position(s)[0], 1)
        s_vol = round(score_volume_price(s)[0], 1)
        s_rsi = round(score_rsi(s)[0], 1)
        s_atr = round(score_volatility(s)[0], 1)
        s_bonus = round(score_bonus(s)[0], 1)
        mark = " *" if r["code"] in holdings_before else " +"
        log.info("  %-4d %-10s %5.1f %5.1f %5.1f %5.1f %5.1f %5.1f %+5.1f%s",
                 i + 1, r["code"], r["score"],
                 s_trend, s_pos, s_vol, s_rsi, s_atr, s_bonus, mark)
    log.info("  " + "-" * 60)
    log.info("  * = 已持有  + = 新买入")

def _print_stop(tag: str, code: str, name: str, pos: dict, price: float, pnl_pct: float,
                date: str, extra: str = ""):
    """打印止损止盈详细信息"""
    shares = pos["shares"]
    cost = pos["avg_cost"]
    amount = shares * price
    profit = (price - cost) * shares
    highest = pos.get("highest_price", cost)
    buy_date = pos.get("buy_date", date)
    try:
        bd = datetime.strptime(buy_date, "%Y-%m-%d").date()
        td = datetime.strptime(date, "%Y-%m-%d").date()
        hold_days = (td - bd).days
    except Exception:
        hold_days = 0

    log.info("")
    log.info("  " + "─" * 50)
    log.info("  %s | %s %s", tag, code, name)
    log.info("  成本价: %.2f  |  现价: %.2f  |  盈亏: %+.2f%% (%+.0f元)",
             cost, price, pnl_pct * 100, profit)
    log.info("  持仓天数: %d  |  持仓市值: %s  |  股数: %d",
             hold_days, _fmt_amount(amount), shares)
    if highest > cost:
        log.info("  买入后最高: %.2f (+%.1f%%)", highest, (highest / cost - 1) * 100)
    if extra:
        log.info("  %s", extra)
    log.info("  " + "─" * 50)

def _print_trade_actions(to_sell: list[str], to_buy: list[dict],
                         positions: dict, prices: dict[str, float],
                         target_stocks: list[dict],
                         stock_names: dict[str, str] = None):
    """打印买卖动作清单"""
    if stock_names is None:
        stock_names = {}
    if not to_sell and not to_buy:
        return

    log.info("")
    log.info("  🔄 调仓动作:")

    if to_sell:
        log.info("    卖出(%d只):  ＜－ 被新一期评分淘汰，不在Top名单内", len(to_sell))
        for code in to_sell:
            pos = positions.get(code)
            if pos and pos["shares"] > 0:
                price = prices.get(code, pos.get("current_price", 0))
                cost = pos["avg_cost"]
                pnl = (price - cost) / cost * 100 if cost > 0 else 0
                log.info("      🚫 %s %s  成本%.2f 现价%.2f  盈亏%+.1f%%  市值%s",
                         code, stock_names.get(code, ""), cost, price, pnl,
                         _fmt_amount(pos["shares"] * price))

    if to_buy:
        log.info("    买入(%d只):  ＜－ 新进入Top%d名单，评分排名靠前", len(to_buy), len(target_stocks))
        for stock in to_buy:
            code = stock["code"]
            price = prices.get(code, stock.get("price", 0))
            name = stock.get("name", stock_names.get(code, ""))
            rank_info = next((r for r in target_stocks if r["code"] == code), None)
            score_str = f" 评分{rank_info['score']:.1f}" if rank_info else ""
            log.info("      ✅ %s  现价%.2f%s", code, price, score_str)

def _print_post_rebalance(pf: Portfolio, target_codes: set, to_keep: list[str],
                          to_buy: list[dict], total_slots: int):
    """打印调仓后预计持仓结构"""
    log.info("")
    log.info("  📋 调仓后持仓结构 (等权, 共%d只):", total_slots)
    log.info("  %-10s %-8s %8s %s", "代码", "状态", "权重%", "说明")
    log.info("  " + "-" * 45)
    weight = 100.0 / total_slots if total_slots > 0 else 0
    total_value = pf.total_value

    for code in to_keep:
        pos = pf.positions.get(code)
        cur_w = (pos["shares"] * pos.get("current_price", 0) / total_value * 100) if pos and pos["shares"] > 0 else 0
        log.info("  %-10s %-8s %7.1f%%  持有中(当前%.1f%%)",
                 code, "📌 保留", weight, cur_w)

    for stock in to_buy:
        log.info("  %-10s %-8s %7.1f%%  新买入",
                 stock["code"], "🆕 新增", weight)
    log.info("  " + "-" * 45)


# ============================================================================
# 4. 主回测循环
# ============================================================================

def run_backtest(
    stock_codes: list[str],
    start_date: str,
    end_date: Optional[str] = None,
    top_n: int = DEFAULT_TOP_N,
    rebalance_days: int = DEFAULT_REBALANCE_DAYS,
    init_cash: float = DEFAULT_INIT_CASH,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """
    运行完整回测。

    Returns:
        report dict: {total_return, annual_return, sharpe, max_drawdown, win_rate, ...}
    """
    start = _normalize_date(start_date)
    end = _normalize_date(end_date) if end_date else _normalize_date(datetime.now().strftime("%Y-%m-%d"))

    log.info("=" * 60)
    log.info("回测参数: 起始=%s, 结束=%s, 持仓数=%d, 调仓周期=%d天",
             start.date(), end.date(), top_n, rebalance_days)
    log.info("初始资金: %.0f 万, 股票池: %d 只", init_cash / 1e4, len(stock_codes))
    log.info("=" * 60)

    # 1. 预加载数据
    datafeed = DataFeed(stock_codes, lookback_days)
    datafeed.preload()

    if len(datafeed._cache) < top_n:
        log.error("有效股票数(%d)不足TopN(%d)", len(datafeed._cache), top_n)
        return {}

    # 2. 生成交易日历（取所有股票日期范围的并集，确保覆盖回测区间）
    all_dates_set = set()
    for df in datafeed._cache.values():
        for d in df["date"].tolist():
            all_dates_set.add(d)
    all_dates = sorted(all_dates_set)
    trading_dates = [d for d in all_dates if start <= d <= end]

    if len(trading_dates) < rebalance_days * 2:
        log.error("交易日不足 (%d天)", len(trading_dates))
        return {}

    log.info("交易日: %d 天 (%s ~ %s)", len(trading_dates),
             trading_dates[0].date(), trading_dates[-1].date())

    # 基准：股票池等权平均。用每只股票日涨跌幅的等权平均复利累计。
    bench_nav = 1.0  # 基准净值，初始=1
    prev_close: dict[str, float] = {}  # code -> 前一日收盘价

    # 3. 初始化组合
    portfolio = Portfolio(init_cash)
    last_rebalance_idx = -rebalance_days  # 确保第1天就调仓

    # 4. 逐日循环
    for day_idx, today in enumerate(trading_dates):
        # 获取当日收盘价（用各股的当日close）
        close_prices = {}
        for code in datafeed._cache:
            df = datafeed.get(code, today)
            if df is not None and len(df) > 0:
                close_prices[code] = float(df["close"].iloc[-1])

        # 是否调仓日？
        if day_idx - last_rebalance_idx >= rebalance_days:
            log.info("  [%s] 调仓日 (第%d天)", today.date(), day_idx)
            target = score_universe(datafeed, today, top_n)
            portfolio.execute_rebalance(target, close_prices, today)
            last_rebalance_idx = day_idx

        # 记录净值（含基准：股票池等权收益率复利累计）
        daily_ret = portfolio.record_nav(today, close_prices)
        date_str = str(today.date()) if hasattr(today, "date") else str(today)

        # 计算每只股票当日收益率，取等权平均
        daily_returns = []
        for code, price in close_prices.items():
            if code in prev_close and prev_close[code] > 0 and price > 0:
                daily_returns.append((price / prev_close[code] - 1))
        if daily_returns:
            avg_ret = sum(daily_returns) / len(daily_returns)
            bench_nav *= (1 + avg_ret)
            portfolio.benchmark_nav.append({
                "date": date_str,
                "close": bench_nav,
                "return_pct": (bench_nav - 1) * 100,
            })
        # 更新前一日收盘价
        prev_close = {k: v for k, v in close_prices.items() if v > 0}

        # ---- 每日持仓摘要 ----
        _print_daily_summary(portfolio, today, close_prices, daily_ret, day_idx == last_rebalance_idx)

        if (day_idx + 1) % 50 == 0:
            log.info("  进度: %d/%d 天, 净值: %.4f", day_idx + 1, len(trading_dates),
                     portfolio.total_value / init_cash)

    # 5. 计算绩效指标
    report = _calc_performance(portfolio, init_cash)
    return report


def _calc_performance(portfolio: Portfolio, init_cash: float) -> dict:
    """从净值序列计算绩效指标，含股票池等权平均基准对比"""
    nav = pd.DataFrame(portfolio.nav_history)
    if len(nav) < 2:
        return {"error": "数据不足"}

    nav["daily_return"] = nav["total_value"].pct_change()
    returns = nav["daily_return"].dropna()

    if len(returns) == 0:
        return {"error": "无收益率数据"}

    # 总收益
    final_value = nav["total_value"].iloc[-1]
    total_return = (final_value / init_cash - 1) * 100

    # 年化收益
    days = len(returns)
    years = days / 252
    annual_return = ((final_value / init_cash) ** (1 / years) - 1) * 100 if years > 0 else 0

    # 夏普比率（假设无风险利率=2%）
    rf_daily = 0.02 / 252
    excess = returns - rf_daily
    sharpe = (excess.mean() / excess.std() * np.sqrt(252)) if excess.std() > 0 else 0

    # 最大回撤
    cummax = nav["total_value"].cummax()
    drawdown = (nav["total_value"] - cummax) / cummax
    max_dd = drawdown.min() * 100

    # 胜率
    win_days = (returns > 0).sum()
    win_rate = win_days / len(returns) * 100

    # 交易统计
    trades = pd.DataFrame(portfolio.trade_log)
    if len(trades) > 0:
        sells = trades[trades["action"] == "SELL"]
        total_trades = len(sells)
    else:
        total_trades = 0

    # ---- 基准（股票池等权收益）对比 ----
    bench_return = 0.0
    excess_return = 0.0
    excess_max = 0.0
    excess_min = 0.0
    if portfolio.benchmark_nav:
        bench_df = pd.DataFrame(portfolio.benchmark_nav)
        bench_final = bench_df["return_pct"].iloc[-1]
        bench_return = round(bench_final, 2)
        # 超额收益 = 策略累计收益 - 基准累计收益
        excess_return = round(total_return - bench_return, 2)
        # 逐日超额序列：策略净值 vs 基准净值
        nav["date"] = nav["date"].astype(str)
        merged = nav[["date", "total_value"]].merge(
            bench_df[["date", "close"]], on="date", how="inner"
        )
        if len(merged) > 1:
            merged["strat_nav"] = merged["total_value"] / init_cash
            # bench_df["close"] 存储的是基准净值 (从1开始的复利累计)
            merged["bench_nav"] = merged["close"]
            merged["excess"] = (merged["strat_nav"] - merged["bench_nav"]) * 100
            excess_max = round(merged["excess"].max(), 2)
            excess_min = round(merged["excess"].min(), 2)

    # ----

    report = {
        "start_date": nav["date"].iloc[0],
        "end_date": nav["date"].iloc[-1],
        "trading_days": len(nav),
        "init_cash": init_cash,
        "final_value": round(final_value, 2),
        "total_return_pct": round(total_return, 2),
        "annual_return_pct": round(annual_return, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate_pct": round(win_rate, 2),
        "total_trades": total_trades,
        # 基准对比
        "benchmark_name": "池等权均价",
        "benchmark_return_pct": bench_return,
        "excess_return_pct": excess_return,
        "excess_max_pct": excess_max,
        "excess_min_pct": excess_min,
    }

    # 保存净值序列
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    nav_file = os.path.join(OUTPUT_DIR, f"backtest_nav_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    nav.to_csv(nav_file, index=False, encoding="utf-8-sig")
    report["nav_file"] = nav_file

    # 保存交易记录
    if len(trades) > 0:
        trade_file = os.path.join(OUTPUT_DIR, f"backtest_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        trades.to_csv(trade_file, index=False, encoding="utf-8-sig")
        report["trade_file"] = trade_file

    return report


def _print_report(report: dict):
    """打印回测报告"""
    print("\n" + "=" * 55)
    print("  📊 回测绩效报告")
    print("=" * 55)
    if "error" in report:
        print(f"  ❌ {report['error']}")
        return

    print(f"  回测区间: {report.get('start_date', 'N/A')} ~ {report.get('end_date', 'N/A')}")
    print(f"  交易日数: {report.get('trading_days', 'N/A')}")
    print(f"  初始资金: {report.get('init_cash', 0):,.0f} 元")
    print(f"  最终资金: {report.get('final_value', 0):,.0f} 元")
    print("-" * 55)
    print(f"  总收益率:   {report.get('total_return_pct', 0):+.2f}%")
    print(f"  年化收益:   {report.get('annual_return_pct', 0):+.2f}%")
    print(f"  夏普比率:   {report.get('sharpe_ratio', 0):.2f}")
    print(f"  最大回撤:   {report.get('max_drawdown_pct', 0):.2f}%")
    print(f"  日胜率:     {report.get('win_rate_pct', 0):.1f}%")
    print(f"  总交易次数: {report.get('total_trades', 0)}")
    print("-" * 55)
    # 基准对比
    bm = report.get("benchmark_name", "上证指数")
    bm_ret = report.get("benchmark_return_pct", 0)
    ex_ret = report.get("excess_return_pct", 0)
    ex_max = report.get("excess_max_pct", 0)
    ex_min = report.get("excess_min_pct", 0)
    print(f"  📈 {bm}收益:  {bm_ret:+.2f}%")
    print(f"  🎯 超额收益:  {ex_ret:+.2f}%  (策略 - 基准)")
    print(f"  📊 超额最大:  {ex_max:+.2f}%  |  超额最小:  {ex_min:+.2f}%")
    print("-" * 55)
    print(f"  净值文件: {report.get('nav_file', 'N/A')}")
    print(f"  交易记录: {report.get('trade_file', 'N/A')}")
    print("=" * 55)


# ============================================================================
# 5. 股票池生成
# ============================================================================

def get_default_stock_pool(exclude_markets: list[str] = None) -> list[str]:
    """
    获取回测用的股票池。
    默认用沪深300成分股（避免小盘股K线质量问题）。
    """
    if exclude_markets is None:
        exclude_markets = ["科创板", "北交所"]  # 默认排除

    # 用 akshare 获取沪深300成分股
    try:
        import akshare as ak
        df = ak.index_stock_cons(symbol="000300")
        codes = df["品种代码"].tolist()
    except Exception:
        # 备用：手动指定一批代表性股票
        codes = [
            "600519", "000858", "000568", "600809",  # 白酒
            "601318", "600036", "601166",              # 金融
            "000333", "600690",                        # 家电
            "600276", "300760", "603259",              # 医药
            "002475", "300750", "002594",              # 科技/新能源
            "600900", "601857",                        # 能源
            "002415", "600585", "601899",              # 制造/水泥/矿业
            "600030", "000651", "600887",              # 证券/格力/伊利
        ]

    # 板块过滤 + 代码规范化
    filtered = []
    for c in codes:
        c = str(c).strip()
        market = get_market(c)
        if exclude_markets and market in exclude_markets:
            continue
        filtered.append(c)

    log.info("股票池: %d 只", len(filtered))
    return filtered


# ============================================================================
# 6. 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="本地量化回测引擎 - 基于多因子打分 + 轮动 + 止盈止损",
    )
    parser.add_argument("--start", default=DEFAULT_START, help="回测起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=DEFAULT_END, help="回测结束日期（默认今天）")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="持仓股票数")
    parser.add_argument("--rebalance", type=int, default=DEFAULT_REBALANCE_DAYS, help="调仓周期（交易日）")
    parser.add_argument("--cash", type=float, default=DEFAULT_INIT_CASH, help="初始资金（元）")
    parser.add_argument("--pool", type=str, default=None, help="自定义股票池CSV（含'代码'列），不填则用沪深300")
    parser.add_argument("--no-exclude", action="store_true", help="不过滤板块（默认排除科创/北交所）")
    parser.add_argument("--plot", action="store_true", help="输出收益曲线图（需安装matplotlib）")

    args = parser.parse_args()

    # 股票池
    if args.pool:
        df = pd.read_csv(args.pool, dtype={"代码": str})
        stock_codes = df["代码"].tolist()
    else:
        exclude = None if args.no_exclude else ["科创板", "北交所"]
        stock_codes = get_default_stock_pool(exclude)

    if len(stock_codes) < args.top:
        log.error("股票池(%d只)不足TopN(%d)", len(stock_codes), args.top)
        sys.exit(1)

    # 运行回测
    report = run_backtest(
        stock_codes=stock_codes,
        start_date=args.start,
        end_date=args.end,
        top_n=args.top,
        rebalance_days=args.rebalance,
        init_cash=args.cash,
    )

    _print_report(report)

    # 可选画图
    if args.plot and report.get("nav_file"):
        try:
            import matplotlib.pyplot as plt
            import matplotlib
            matplotlib.use("Agg")
            nav = pd.read_csv(report["nav_file"])
            nav["date"] = pd.to_datetime(nav["date"])
            nav["cum_return"] = nav["total_value"] / args.cash - 1

            fig, axes = plt.subplots(2, 1, figsize=(14, 8))

            # 上图：累计收益曲线（含基准）
            axes[0].plot(nav["date"], nav["cum_return"] * 100,
                         label="策略", color="steelblue", linewidth=1.5)
            axes[0].axhline(y=0, color="gray", linestyle="--", linewidth=0.8)

            # 叠加基准曲线
            bm = report.get("benchmark_return_pct", None)
            bm_name = report.get("benchmark_name", "基准")
            if bm is not None:
                axes[0].axhline(y=bm, color="darkorange", linestyle="--", linewidth=1.2,
                                label=f"{bm_name} ({bm:+.2f}%)")

            axes[0].set_ylabel("累计收益 (%)")
            axes[0].set_title(f"回测收益曲线 (Top{args.top}, {args.rebalance}日轮动)")
            axes[0].legend()
            axes[0].grid(True, alpha=0.3)

            # 下图：回撤
            cummax = nav["total_value"].cummax()
            dd = (nav["total_value"] - cummax) / cummax * 100
            axes[1].fill_between(nav["date"], 0, dd, color="red", alpha=0.3)
            axes[1].set_ylabel("回撤 (%)")
            axes[1].set_xlabel("日期")
            axes[1].grid(True, alpha=0.3)

            plt.tight_layout()
            plot_file = os.path.join(OUTPUT_DIR, f"backtest_plot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
            plt.savefig(plot_file, dpi=150)
            log.info("收益曲线已保存: %s", plot_file)
        except ImportError:
            log.warning("未安装matplotlib，跳过画图。pip install matplotlib")


if __name__ == "__main__":
    main()
