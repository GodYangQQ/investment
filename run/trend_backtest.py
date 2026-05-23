#!/usr/bin/env python3
"""
趋势追涨策略回测引擎
====================
基于 trend_strategy.py 的追涨策略，进行历史数据回测。

核心逻辑：
  1. 每日判定市场情绪（强/一般/退潮）
  2. 对AI算力池打分排序，过滤高潮股，选排名11-30中取5只
  3. ATR动态止损 + 趋势移动止盈
  4. 情绪分档仓位：强=满仓，一般=半仓，退潮=空仓
  5. 每5个交易日轮动调仓

用法：
    python backtest_trend.py                                    # 默认参数
    python backtest_trend.py --start 2025-01-01 --top 5          # 2025年起
    python backtest_trend.py --rebalance 10 --pool-size 200      # 10日轮动
    python backtest_trend.py --no-ai-pool --pool hs300.csv       # 自定义池
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from core.stock_strategy import (
    fetch_daily_kline,
    compute_indicators,
    get_latest_signals,
)
from core.quant_score import calc_rsi_slope_curvature
from core.trend_strategy import (
    TrendStrategy,
    get_ai_pool,
    EXCLUDE_NAME_KEYWORDS,
)
from core.market_filter import get_market

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backtest_trend")

# ═══════════════════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════════════════

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "output", "backtest")

DEFAULT_START = "2024-06-01"
DEFAULT_END = None
DEFAULT_TOP_N = 5
DEFAULT_REBALANCE_DAYS = 5
DEFAULT_LOOKBACK_DAYS = 250
DEFAULT_INIT_CASH = 1_000_000

# 交易成本
COMMISSION_RATE = 0.0003
STAMP_TAX_RATE = 0.001
MIN_COMMISSION = 5
SLIPPAGE = 0.001


# ═══════════════════════════════════════════════════════════════════════════
# 1. 数据层
# ═══════════════════════════════════════════════════════════════════════════

class DataFeed:
    """历史数据时间机器，杜绝未来函数"""

    def __init__(self, stock_codes: list[str], lookback_days: int = 250):
        self.codes = stock_codes
        self.lookback = lookback_days
        self._cache: dict[str, pd.DataFrame] = {}
        self.names: dict[str, str] = {}

    def preload(self):
        """预加载所有K线"""
        total = len(self.codes)
        log.info("预加载 %d 只股票K线（先拉名称，再逐只取K线，大池子请耐心等待）...", total)

        import requests
        import time
        # Phase 1: 批量获取名称
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
                        self.names[fields[2]] = fields[1]
            except Exception:
                pass
            if i + batch_size < total:
                time.sleep(0.05)
        log.info("名称获取完成: %d 只", len(self.names))

        # Phase 2: 逐只拉K线（最耗时，每25只汇报一次）
        for i, code in enumerate(self.codes):
            if (i + 1) % 25 == 0 or i == 0:
                log.info("  加载K线: %d/%d (%.0f%%)", i + 1, total, (i + 1) / total * 100)
            try:
                df = fetch_daily_kline(code, days=500)
                if df is not None and len(df) >= 60:
                    # 过滤ST
                    name = self.names.get(code, "")
                    if any(kw in name for kw in EXCLUDE_NAME_KEYWORDS):
                        continue
                    self._cache[code] = df
            except Exception:
                pass
        log.info("预加载完成: %d/%d 有效", len(self._cache), total)

    def get(self, code: str, as_of_date) -> Optional[pd.DataFrame]:
        df = self._cache.get(code)
        if df is None:
            return None
        mask = df["date"] <= pd.Timestamp(as_of_date)
        sliced = df[mask].copy()
        if len(sliced) < 60:
            return None
        return sliced.tail(self.lookback)


def _normalize_date(d):
    return pd.Timestamp(d)


# ═══════════════════════════════════════════════════════════════════════════
# 2. 指标提取（将 compute_indicators 结果转为 trend_strategy 需要的格式）
# ═══════════════════════════════════════════════════════════════════════════

def extract_features(df: pd.DataFrame) -> dict:
    """
    从含技术指标的DataFrame提取趋势策略所需特征。
    调用 compute_indicators + get_latest_signals。
    """
    if df is None or len(df) < 20:
        return {}

    try:
        df = compute_indicators(df)
        price = float(df["close"].iloc[-1])
        signals = get_latest_signals(df, {"current_price": price})
    except Exception:
        return {}

    closes = df["close"].values
    volumes = df["volume"].values
    n = len(closes)

    # 基础字段
    features = {
        "close": price,
        "rsi14": signals.get("rsi14", 50),
        "ma20": signals.get("ma20", price),
        "ma5": signals.get("ma5", price),
        "ma60": signals.get("ma60", price),
        "atr14": signals.get("atr14", 0),
        "cmf_20d": signals.get("cmf_20d", 0),
    }

    # RSI斜率和曲率（区分"超买且上升" vs "超买但拐头"）
    rsi_info = calc_rsi_slope_curvature(list(closes))
    features["rsi_slope_5d"] = rsi_info.get("rsi_slope_5d", 0) or 0
    features["rsi_curvature"] = rsi_info.get("rsi_curvature", 0) or 0
    features["rsi_arrow"] = rsi_info.get("rsi_arrow", "→")

    # 5日涨跌幅
    if n >= 6:
        features["pct_5d"] = round((closes[-1] / closes[-6] - 1) * 100, 2)
    else:
        features["pct_5d"] = 0

    # 量比 (5日均量 / 20日均量)
    vol_ma5 = signals.get("vol_ma5") or 0
    vol_ma20 = signals.get("vol_ma20") or 0
    features["vol_ratio"] = round(vol_ma5 / vol_ma20, 2) if vol_ma20 > 0 else 1.0

    # 量比加速度：当前量比 vs 5日前量比（用5日前close数据近似估算）
    features["vol_ratio_prev"] = 1.0
    if n >= 11 and vol_ma20 > 0:
        # 用5-10日前的5日均量 / 当时的20日均量来近似
        prev_vol_ma5 = float(np.mean(volumes[-11:-6])) if len(volumes) >= 11 else vol_ma5
        prev_vol_ma20 = float(np.mean(volumes[-26:-6])) if len(volumes) >= 26 else vol_ma20
        features["vol_ratio_prev"] = round(prev_vol_ma5 / prev_vol_ma20, 2) if prev_vol_ma20 > 0 else 1.0

    # OBV趋势方向
    features["obv_trend"] = signals.get("obv_trend", "")

    # 换手率（从 signals 或估算）
    features["turnover_rate"] = signals.get("turnover_rate", 10)

    # 连续涨停估算（近似：连续涨幅>9%的天数）
    cons_up = 0
    for i in range(n - 1, max(n - 10, 0), -1):
        if closes[i - 1] > 0 and (closes[i] / closes[i - 1] - 1) > 0.09:
            cons_up += 1
        else:
            break
    features["consecutive_limit_up"] = cons_up

    # 连续阳线
    features["consecutive_up"] = signals.get("consecutive_up", 0)
    features["consecutive_down"] = signals.get("consecutive_down", 0)

    # PE
    features["PE_TTM"] = signals.get("pe_ttm", 0)

    # 市场板块
    features["market"] = get_market(features.get("code", ""))

    return features


# ═══════════════════════════════════════════════════════════════════════════
# 3. 组合管理
# ═══════════════════════════════════════════════════════════════════════════

class Portfolio:
    """管理现金、持仓、止盈止损"""

    def __init__(self, init_cash: float, strategy: TrendStrategy):
        self.init_cash = init_cash
        self.cash = init_cash
        self.strategy = strategy
        self.positions: dict[str, dict] = {}
        self.nav_history: list[dict] = []
        self.benchmark_nav: list[dict] = []   # 股票池等权均价基准净值序列
        self.trade_log: list[dict] = []
        self.stock_names: dict[str, str] = {}

    @property
    def total_value(self) -> float:
        pos_val = sum(
            p["shares"] * p.get("current_price", p["avg_cost"])
            for p in self.positions.values()
        )
        return self.cash + pos_val

    def record_nav(self, date, prices: dict[str, float]) -> float:
        pos_val = 0.0
        for code, pos in self.positions.items():
            price = prices.get(code)
            if price:
                pos["current_price"] = price
                pos_val += pos["shares"] * price
                if price > pos.get("highest_price", 0):
                    pos["highest_price"] = price

        total = self.cash + pos_val
        prev = self.nav_history[-1]["total_value"] if self.nav_history else self.init_cash
        daily_ret = (total / prev - 1) * 100 if prev > 0 else 0

        self.nav_history.append({
            "date": str(date.date()) if hasattr(date, "date") else str(date),
            "total_value": total,
            "cash": self.cash,
            "positions_value": pos_val,
            "num_positions": len(self.positions),
            "daily_return": round(daily_ret, 2),
        })
        return daily_ret

    def execute_rebalance(self, target_stocks: list[dict], prices: dict[str, float],
                          date):
        """
        执行调仓：检查止盈止损 → 卖出淘汰股 → 买入新股（满仓运行）
        """
        today = str(date.date()) if hasattr(date, "date") else str(date)

        # 更新名称
        for s in target_stocks:
            self.stock_names[s["code"]] = s.get("name", s["code"])

        # ---- Step 1: ATR止损 + 趋势止盈 ----
        for code, pos in list(self.positions.items()):
            price = prices.get(code)
            if not price or pos["shares"] <= 0:
                continue

            entry = pos["avg_cost"]
            highest = pos.get("highest_price", entry)
            atr = pos.get("atr_at_entry", 0)
            loss_pct = (price - entry) / entry * 100

            # ATR动态止损
            stop_price = self.strategy.calc_stop_price(entry, atr)
            if price <= stop_price:
                log.info("  🔴 ATR止损 %s: %.2f→%.2f (止损价%.2f, 亏损%.1f%%)",
                         code, entry, price, stop_price, loss_pct)
                self._sell(code, pos["shares"], price, today, "ATR止损")
                continue

            # 趋势移动止盈（盈利>5%后启用）
            if loss_pct > 5:
                trail_stop = self.strategy.calc_trail_stop(highest, atr)
                if price <= trail_stop:
                    log.info("  🟡 趋势止盈 %s: %.2f→%.2f (最高%.2f, 回撤止盈%.2f)",
                             code, entry, price, highest, trail_stop)
                    self._sell(code, pos["shares"], price, today, "趋势止盈")
                    continue

            # 破MA20止损（持有>10天，盈利回撤保护）
            if pos.get("hold_days", 0) > 10:
                ma20 = pos.get("ma20_at_entry", 0)
                if ma20 > 0 and price < ma20:
                    log.info("  🔵 MA20止损 %s: %.2f < MA20=%.2f",
                             code, price, ma20)
                    self._sell(code, pos["shares"], price, today, "MA20止损")
                    continue

        # ---- Step 2: 计算目标组合 ----
        target_codes = {s["code"] for s in target_stocks}
        cur_holdings = {c for c, p in self.positions.items() if p["shares"] > 0}
        to_sell = [c for c in cur_holdings if c not in target_codes]
        to_keep = [c for c in cur_holdings if c in target_codes]
        to_buy = [s for s in target_stocks if s["code"] not in cur_holdings]

        # ---- Step 3: 卖出淘汰股 ----
        for code in to_sell:
            pos = self.positions.get(code)
            if pos and pos["shares"] > 0:
                price = prices.get(code, pos.get("current_price", 0))
                if price > 0:
                    pnl = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
                    log.info("  [卖出] %s %s  %d股 @%.2f  盈亏%+.1f%%",
                             code, self.stock_names.get(code, code),
                             pos["shares"], price, pnl)
                    self._sell(code, pos["shares"], price, today, "轮动淘汰")

        # ---- Step 4: 买入新股（满仓等权）----
        total_slots = len(to_keep) + len(to_buy)
        if total_slots == 0:
            return

        # 等权分配：总资产 / 持仓数
        target_total_value = self.total_value
        per_stock_value = target_total_value / total_slots

        for stock in to_buy:
            price = prices.get(stock["code"], stock.get("close", 0))
            if price <= 0:
                continue

            budget = min(per_stock_value, self.cash * 0.95)
            shares = int(budget / price / 100) * 100
            if shares < 100:
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
                "hold_days": 0,
                "atr_at_entry": stock.get("atr14", 0),
                "ma20_at_entry": stock.get("ma20", 0),
            }
            self.trade_log.append({
                "date": today, "code": stock["code"], "action": "BUY",
                "shares": shares, "price": price, "cost": cost, "reason": "调仓买入",
            })
            log.info("  [买入] %s %s  %d股 @%.2f  成本%.0f元",
                     stock["code"], stock.get("name", ""), shares, price, cost)

        # 更新持仓天数
        for code in self.positions:
            pos = self.positions[code]
            try:
                bd = datetime.strptime(pos["buy_date"], "%Y-%m-%d").date()
                td = datetime.strptime(today, "%Y-%m-%d").date()
                pos["hold_days"] = (td - bd).days
            except Exception:
                pass

        log.info("  📊 调仓完成: 现金%.0f元 | 持仓%d只 | 总资产%.0f元",
                 self.cash, len(self.positions), self.total_value)

    def _sell(self, code: str, shares: int, price: float, date: str, reason: str):
        pos = self.positions.get(code)
        if not pos or shares <= 0:
            return
        actual = min(shares, pos["shares"])
        revenue = actual * price * (1 - COMMISSION_RATE - STAMP_TAX_RATE)
        revenue -= max(MIN_COMMISSION, actual * price * COMMISSION_RATE)
        self.cash += revenue
        self.trade_log.append({
            "date": date, "code": code, "action": "SELL",
            "shares": actual, "price": price, "revenue": revenue, "reason": reason,
        })
        pos["shares"] -= actual
        if pos["shares"] <= 0:
            del self.positions[code]


# ═══════════════════════════════════════════════════════════════════════════
# 4. 回测主循环
# ═══════════════════════════════════════════════════════════════════════════

def run_backtest(
    stock_codes: list[str],
    start_date: str,
    end_date: Optional[str] = None,
    top_n: int = DEFAULT_TOP_N,
    rebalance_days: int = DEFAULT_REBALANCE_DAYS,
    init_cash: float = DEFAULT_INIT_CASH,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """运行趋势追涨策略回测"""

    start = _normalize_date(start_date)
    end = _normalize_date(end_date) if end_date else _normalize_date(datetime.now().strftime("%Y-%m-%d"))

    strategy = TrendStrategy(num_picks=top_n)

    log.info("=" * 60)
    log.info("📈 趋势追涨策略回测")
    log.info("区间: %s ~ %s | 持仓%d只 | 轮动%d天 | 初始%.0f万",
             start.date(), end.date(), top_n, rebalance_days, init_cash / 1e4)
    log.info("评分权重: 量比=%.0f%% 量加速度=%.0f%% 资金=%.0f%% 动量=%.0f%%",
             strategy.w_vol_ratio * 100, strategy.w_vol_accel * 100,
             strategy.w_moneyflow * 100, strategy.w_momentum * 100)
    log.info("止盈止损: ATR止损×%.1f | 趋势止盈×%.1f",
             strategy.atr_stop_mult, strategy.atr_trail_mult)
    log.info("=" * 60)

    # 1. 预加载
    datafeed = DataFeed(stock_codes, lookback_days)
    datafeed.preload()
    if len(datafeed._cache) < top_n:
        log.error("有效股票(%d)不足TopN(%d)", len(datafeed._cache), top_n)
        return {"error": "股票池不足"}

    # 2. 交易日历
    all_dates = set()
    for df in datafeed._cache.values():
        for d in df["date"].tolist():
            all_dates.add(d)
    trading_dates = sorted([d for d in all_dates if start <= d <= end])
    if len(trading_dates) < rebalance_days * 2:
        log.error("交易日不足: %d天", len(trading_dates))
        return {"error": "交易日不足"}

    log.info("交易日: %d 天 (%s ~ %s)", len(trading_dates),
             trading_dates[0].date(), trading_dates[-1].date())

    # ---- 基准：股票池等权收益率复利累计 ----
    bench_nav = 1.0
    prev_close: dict[str, float] = {}

    # 3. 逐日循环
    portfolio = Portfolio(init_cash, strategy)
    last_rebalance_idx = -rebalance_days

    for day_idx, today in enumerate(trading_dates):
        # 当日收盘价
        close_prices = {}
        for code in datafeed._cache:
            df = datafeed.get(code, today)
            if df is not None and len(df) > 0:
                close_prices[code] = float(df["close"].iloc[-1])

        # 记录净值（含基准：股票池等权收益率复利累计）
        daily_ret = portfolio.record_nav(today, close_prices)
        date_str = str(today.date()) if hasattr(today, "date") else str(today)

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
        prev_close = {k: v for k, v in close_prices.items() if v > 0}

        # 调仓日？
        if day_idx - last_rebalance_idx >= rebalance_days:
            log.info("  [%s] 调仓日 #%d",
                     today.date(), day_idx // rebalance_days + 1)

            # 选股
            rows = []
            for code in datafeed._cache:
                df = datafeed.get(code, today)
                if df is None:
                    continue
                features = extract_features(df)
                if not features:
                    continue
                features["code"] = code
                features["name"] = datafeed.names.get(code, code)
                rows.append(features)

            pool_df = pd.DataFrame(rows)
            if len(pool_df) == 0:
                continue

            picks = strategy.select(pool_df)
            portfolio.execute_rebalance(picks, close_prices, today)
            last_rebalance_idx = day_idx

        # 进度日志
        if (day_idx + 1) % 50 == 0:
            nav = portfolio.total_value / init_cash
            log.info("  进度: %d/%d天 | 净值: %.4f | 持仓: %d只",
                     day_idx + 1, len(trading_dates), nav, len(portfolio.positions))

    # 5. 绩效计算
    report = _calc_performance(portfolio, init_cash)
    return report


def _calc_performance(portfolio: Portfolio, init_cash: float) -> dict:
    """计算回测绩效，含股票池等权收益基准对比"""
    nav = pd.DataFrame(portfolio.nav_history)
    if len(nav) < 2:
        return {"error": "数据不足"}

    nav["daily_return_pct"] = nav["total_value"].pct_change()
    returns = nav["daily_return_pct"].dropna()
    if len(returns) == 0:
        return {"error": "无收益率数据"}

    final_value = nav["total_value"].iloc[-1]
    total_return = (final_value / init_cash - 1) * 100
    days = len(returns)
    years = days / 252
    annual_return = ((final_value / init_cash) ** (1 / years) - 1) * 100 if years > 0 else 0

    rf_daily = 0.02 / 252
    excess = returns - rf_daily
    sharpe = (excess.mean() / excess.std() * np.sqrt(252)) if excess.std() > 0 else 0

    cummax = nav["total_value"].cummax()
    drawdown = (nav["total_value"] - cummax) / cummax
    max_dd = drawdown.min() * 100

    win_days = (returns > 0).sum()
    win_rate = win_days / len(returns) * 100

    trades = pd.DataFrame(portfolio.trade_log)
    if len(trades) > 0:
        sells = trades[trades["action"] == "SELL"]
        total_trades = len(sells)
        if total_trades > 0:
            # 从trade_log推算盈亏（简化）
            avg_win_rate = 0
        else:
            avg_win_rate = 0
    else:
        total_trades = 0
        avg_win_rate = 0

    # ---- 基准（股票池等权收益）对比 ----
    bench_return = 0.0
    excess_return = 0.0
    excess_max = 0.0
    excess_min = 0.0
    if portfolio.benchmark_nav:
        bench_df = pd.DataFrame(portfolio.benchmark_nav)
        bench_final = bench_df["return_pct"].iloc[-1]
        bench_return = round(bench_final, 2)
        excess_return = round(total_return - bench_return, 2)
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

    # 保存文件
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    nav_file = os.path.join(OUTPUT_DIR, f"trend_backtest_nav_{ts}.csv")
    nav.to_csv(nav_file, index=False, encoding="utf-8-sig")
    report["nav_file"] = nav_file

    if len(trades) > 0:
        trade_file = os.path.join(OUTPUT_DIR, f"trend_backtest_trades_{ts}.csv")
        trades.to_csv(trade_file, index=False, encoding="utf-8-sig")
        report["trade_file"] = trade_file

    return report


def _print_report(report: dict):
    """打印回测报告"""
    print("\n" + "=" * 55)
    print("  📊 趋势追涨策略 — 回测绩效报告")
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
    bm = report.get("benchmark_name", "池等权均价")
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


# ═══════════════════════════════════════════════════════════════════════════
# 5. 命令行入口
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="趋势追涨策略回测 — A股AI算力池追涨",
    )
    parser.add_argument("--start", default=DEFAULT_START, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=DEFAULT_END, help="结束日期（默认今天）")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="持仓股数（默认5）")
    parser.add_argument("--rebalance", type=int, default=DEFAULT_REBALANCE_DAYS, help="调仓周期（默认5天）")
    parser.add_argument("--cash", type=float, default=DEFAULT_INIT_CASH, help="初始资金")
    parser.add_argument("--no-ai-pool", action="store_true", help="不使用AI算力池")
    parser.add_argument("--pool", type=str, default=None, help="自定义股票池CSV（含'代码'列）")
    parser.add_argument("--pool-size", type=int, default=200, help="自定义池时取前N只")
    parser.add_argument("--plot", action="store_true", help="画收益曲线")

    args = parser.parse_args()

    # 股票池
    if args.pool:
        df = pd.read_csv(args.pool, dtype={"代码": str})
        stock_codes = df["代码"].tolist()[:args.pool_size]
        log.info("自定义股票池: %d 只", len(stock_codes))
    elif args.no_ai_pool:
        # 用沪深300
        try:
            import akshare as ak
            df = ak.index_stock_cons(symbol="000300")
            stock_codes = df["品种代码"].tolist()
        except Exception:
            log.error("无法获取沪深300成分股")
            sys.exit(1)
    else:
        stock_codes = get_ai_pool()
        log.info("AI算力池: %d 只", len(stock_codes))

    if len(stock_codes) < args.top * 2:
        log.error("股票池(%d只)至少需要top的2倍(%d)", len(stock_codes), args.top * 2)
        sys.exit(1)

    report = run_backtest(
        stock_codes=stock_codes,
        start_date=args.start,
        end_date=args.end,
        top_n=args.top,
        rebalance_days=args.rebalance,
        init_cash=args.cash,
    )

    _print_report(report)

    if args.plot and report.get("nav_file"):
        _plot_equity_curve(report, args.cash)


def _plot_equity_curve(report: dict, init_cash: float):
    try:
        import matplotlib.pyplot as plt
        import matplotlib
        matplotlib.use("Agg")

        nav = pd.read_csv(report["nav_file"])
        nav["date"] = pd.to_datetime(nav["date"])
        nav["cum_return"] = nav["total_value"] / init_cash - 1

        fig, axes = plt.subplots(2, 1, figsize=(14, 8))

        axes[0].plot(nav["date"], nav["cum_return"] * 100, color="steelblue", linewidth=1.5, label="策略")
        axes[0].axhline(y=0, color="gray", linestyle="--", linewidth=0.8)

        # 叠加基准收益线
        bm = report.get("benchmark_return_pct", None)
        bm_name = report.get("benchmark_name", "基准")
        if bm is not None:
            axes[0].axhline(y=bm, color="darkorange", linestyle="--", linewidth=1.2,
                            label=f"{bm_name} ({bm:+.2f}%)")

        axes[0].fill_between(nav["date"], 0, nav["cum_return"] * 100,
                             where=(nav["cum_return"] >= 0), color="steelblue", alpha=0.15)
        axes[0].fill_between(nav["date"], 0, nav["cum_return"] * 100,
                             where=(nav["cum_return"] < 0), color="red", alpha=0.15)
        axes[0].set_ylabel("累计收益率 (%)")
        axes[0].set_title(f"趋势追涨策略回测 ({report['start_date']} ~ {report['end_date']})")
        axes[0].legend()
        axes[0].grid(True, alpha=0.3)

        cummax = nav["total_value"].cummax()
        drawdown = (nav["total_value"] - cummax) / cummax * 100
        axes[1].fill_between(nav["date"], 0, drawdown, color="red", alpha=0.3)
        axes[1].plot(nav["date"], drawdown, color="darkred", linewidth=1)
        axes[1].set_ylabel("回撤 (%)")
        axes[1].set_xlabel("日期")
        axes[1].grid(True, alpha=0.3)

        plt.tight_layout()
        plot_file = report["nav_file"].replace(".csv", ".png")
        plt.savefig(plot_file, dpi=150, bbox_inches="tight")
        log.info("收益曲线已保存: %s", plot_file)
    except ImportError:
        log.warning("matplotlib未安装，跳过画图")
    except Exception as e:
        log.warning("画图失败: %s", e)


if __name__ == "__main__":
    main()
