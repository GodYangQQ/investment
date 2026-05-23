# -*- coding: utf-8 -*-
"""
聚宽平台多因子量化回测策略
==========================
基于 investment 项目的 quant_score.py 打分模型，完整移植到聚宽平台。

打分体系（满分100）：
  trend(25) + position(25) + volume_price(20) + rsi(15) + volatility(15) + extra(±13)

选股逻辑：
  - 从全A股（或指定股票池）中，每N日计算多因子总分
  - 取Top K只，等权买入
  - 排除PE>200、ST、上市不足60天的新股

止盈止损：
  - 固定止损 -8%
  - 回撤止盈：从买入后最高点回撤 -10%
  - 均线止损：持有>10天且跌破MA20

用法：
  1. 复制本脚本到聚宽研究/策略平台
  2. 在 initialize() 中调整参数
  3. 点击"运行回测"
"""

import numpy as np
import pandas as pd
from jqdata import *


# ============================================================================
# 策略参数（在 initialize 中可调整）
# ============================================================================

def initialize(context):
    """初始化策略参数和定时任务"""
    # ---- 选股参数 ----
    g.top_n = 5                  # 持仓股票数
    g.rebalance_days = 10        # 调仓周期（交易日）
    g.lookback_days = 300        # 打分所需K线天数
    g.max_pe = 200               # PE上限过滤
    g.min_price = 5              # 最低股价（过滤仙股）
    g.exclude_st = True          # 排除ST
    g.exclude_new_list = True    # 排除上市<60天

    # 股票池：None=全A股，也可指定如 get_index_stocks('000300.XSHG')
    g.stock_pool = None

    # ---- 止盈止损参数 ----
    g.stop_loss_pct = 0.08       # 固定止损 -8%
    g.trail_stop_pct = 0.10      # 回撤止盈：从最高点回撤10%
    g.take_profit_pct = 0.30     # 固定止盈 +30%
    g.ma_stop_days = 20          # 均线止损周期（持有>10天才检查）

    # ---- 仓位管理 ----
    g.single_stock_max_pct = 0.25  # 单只最大仓位 25%

    # ---- 状态变量 ----
    g.last_rebalance_idx = -10     # 上次调仓的bar序号
    g.entry_prices = {}            # code -> 买入均价
    g.highest_prices = {}          # code -> 买入后最高价
    g.entry_dates = {}             # code -> 买入日期
    g.bar_count = 0               # bar计数器

    # ---- 定时任务 ----
    # 用 handle_data 逐日执行（简单可靠）
    g.run_mode = "daily"  # "daily" 或 "minute"

    # ---- 基础设置 ----
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    # 交易成本：佣金万三，印花税千一，最低5元
    set_order_cost(
        OrderCost(close_tax=0.001, open_commission=0.0003,
                  close_commission=0.0003, min_commission=5),
        type='stock'
    )
    set_slippage(FixedSlippage(0.001))

    log.info("=" * 50)
    log.info("多因子量化策略初始化完成")
    log.info("持仓数:%d  调仓周期:%d天  止损:%.0f%%  回撤止盈:%.0f%%",
             g.top_n, g.rebalance_days, g.stop_loss_pct * 100, g.trail_stop_pct * 100)
    log.info("=" * 50)


def handle_data(context, data):
    """逐日/逐分钟执行的主函数"""
    # 只在收盘前15:00左右执行（或每天执行一次）
    g.bar_count += 1

    # 每天只执行一次
    if g.run_mode == "daily":
        # 每个bar的第一分钟执行
        pass

    # 止损检查（每次bar都做）
    check_stop_loss(context, data)

    # 检查是否需要调仓
    if g.bar_count - g.last_rebalance_idx >= g.rebalance_days:
        # 防止同一周期重复执行
        current_date = context.current_dt.date()
        if not hasattr(g, '_last_rebalance_date') or g._last_rebalance_date != current_date:
            g._last_rebalance_date = current_date
            g.last_rebalance_idx = g.bar_count
            do_rebalance(context)


# ============================================================================
# 每日止损止盈检查
# ============================================================================

def check_stop_loss(context, data):
    """检查每只持仓是否需要止损止盈，每次bar触发。触发时打印详细信息。"""
    if not context.portfolio.positions:
        return

    current_data = get_current_data()
    today = context.current_dt.date()

    for code, pos in list(context.portfolio.positions.items()):
        if pos.total_amount <= 0:
            continue

        # 停牌跳过
        if code not in current_data or current_data[code].paused:
            continue

        price = current_data[code].last_price
        if price <= 0:
            continue

        avg_cost = g.entry_prices.get(code, pos.avg_cost)
        loss_pct = (price - avg_cost) / avg_cost
        buy_date = g.entry_dates.get(code, today)
        hold_days = (today - buy_date).days if buy_date else 0
        highest = g.highest_prices.get(code, avg_cost)

        # 更新最高价
        if price > highest:
            g.highest_prices[code] = price
            highest = price

        name = current_data[code].name if code in current_data else code

        # 1) 固定止损
        if loss_pct <= -g.stop_loss_pct:
            _print_stop_detail("🔴 固定止损", code, name, price, avg_cost, loss_pct,
                               hold_days, highest, pos.total_amount)
            order_target(code, 0)
            _cleanup_position(code)
            continue

        # 2) 回撤止盈（曾盈利>5%才检查）
        if highest > avg_cost * 1.05:
            drawdown = (highest - price) / highest
            if drawdown >= g.trail_stop_pct:
                profit_from_high = (highest - avg_cost) / avg_cost
                _print_stop_detail("🟡 回撤止盈", code, name, price, avg_cost, loss_pct,
                                   hold_days, highest, pos.total_amount,
                                   extra=f"最高价={highest:.2f}(+{profit_from_high*100:.1f}%) 回撤={drawdown*100:.1f}%")
                order_target(code, 0)
                _cleanup_position(code)
                continue

        # 3) 固定止盈
        if loss_pct >= g.take_profit_pct:
            _print_stop_detail("🟢 固定止盈", code, name, price, avg_cost, loss_pct,
                               hold_days, highest, pos.total_amount)
            order_target(code, 0)
            _cleanup_position(code)
            continue

        # 4) 均线止损（持有>10天）
        if hold_days >= 10:
            try:
                df_ma = attribute_history(code, g.ma_stop_days + 1, '1d', ['close'])
                if len(df_ma) >= g.ma_stop_days:
                    ma_val = df_ma['close'].mean()
                    if price < ma_val:
                        _print_stop_detail("🔵 均线止损", code, name, price, avg_cost, loss_pct,
                                           hold_days, highest, pos.total_amount,
                                           extra=f"MA{g.ma_stop_days}={ma_val:.2f}")
                        order_target(code, 0)
                        _cleanup_position(code)
            except Exception:
                pass


def _print_stop_detail(tag, code, name, price, cost, pnl_pct, hold_days, highest, shares, extra=""):
    """统一格式化止损止盈日志"""
    amount = shares * price
    profit = (price - cost) * shares
    log.info("=" * 55)
    log.info("%s | %s %s", tag, code, name)
    log.info("  成本价: %.2f  |  现价: %.2f  |  盈亏: %+.2f%%",
             cost, price, pnl_pct * 100)
    log.info("  持仓天数: %d  |  持仓市值: %,.0f  |  盈亏金额: %+,.0f",
             hold_days, amount, profit)
    if highest and highest > cost:
        log.info("  买入后最高: %.2f (+%.1f%%)", highest, (highest/cost-1)*100)
    if extra:
        log.info("  %s", extra)
    log.info("=" * 55)


def _cleanup_position(code):
    """清理持仓记录"""
    g.entry_prices.pop(code, None)
    g.highest_prices.pop(code, None)
    g.entry_dates.pop(code, None)


# ============================================================================
# 调仓主逻辑
# ============================================================================

def do_rebalance(context):
    """获取股票池 → 打分 → 排序 → 先卖后买 → 打印完整调仓报告"""
    today = context.current_dt.date()

    # ---- 调仓前持仓快照 ----
    cur_holdings = [code for code, pos in context.portfolio.positions.items()
                    if pos.total_amount > 0]
    total_value = context.portfolio.total_value
    available_cash = context.portfolio.cash

    # Step 1: 获取股票池
    pool = _get_stock_pool(context)

    # Step 2: 对每只股票打分
    log.info("⏳ 股票池: %d 只, 评分中...", len(pool))
    rankings = []
    for code in pool:
        score = score_one_stock(code, context)
        if score is not None:
            rankings.append(score)

    if not rankings:
        log.warning("❌ 打分后无符合条件的股票")
        return

    # 按总分降序排列
    rankings.sort(key=lambda x: x["total_score"], reverse=True)
    target_stocks = rankings[:g.top_n]
    target_codes = [r["code"] for r in target_stocks]

    # ---- 打印调仓报告头部 ----
    log.info("")
    log.info("╔" + "═" * 58 + "╗")
    log.info("║  📊 调仓报告  %s  第%d期" + " " * 30 + "║")
    log.info("╠" + "═" * 58 + "╣")
    log.info("║  总资产: %,.0f  |  现金: %,.0f  |  持仓数: %d/%d" + " " * 10 + "║")
    log.info("╚" + "═" * 58 + "╝")
    log.info("")

    # ---- 调仓前持仓明细 ----
    _print_holdings_snapshot(context, "📋 调仓前持仓", cur_holdings)

    # ---- 新一期打分排名 ----
    log.info("")
    log.info("  🏆 新一期打分 Top%d (满分100):", g.top_n)
    log.info("  %-4s %-10s %-8s %6s %6s %6s %6s %6s %6s %5s",
             "排名", "代码", "名称", "总分", "趋势", "位置", "量价", "RSI", "波动", "附加")
    log.info("  " + "-" * 65)
    for i, r in enumerate(target_stocks):
        s = r.get("signals", {})
        s_trend = round(_score_trend(s), 1)
        s_pos = round(_score_position(s), 1)
        s_vol = round(_score_volume_price(s), 1)
        s_rsi = round(_score_rsi(s), 1)
        s_atr = round(_score_volatility(s), 1)
        s_bonus = round(_score_bonus(s), 1)
        mark = " *" if r["code"] in cur_holdings else " +"
        log.info("  %-4d %-10s %-8s %5.1f %5.1f %5.1f %5.1f %5.1f %5.1f %+5.1f%s",
                 i + 1, r["code"], r.get("name", "")[:8],
                 r["total_score"], s_trend, s_pos, s_vol, s_rsi, s_atr, s_bonus, mark)
    log.info("  " + "-" * 65)
    log.info("  * = 已持有  + = 新买入")

    # ---- 确定买卖清单 ----
    to_sell = [c for c in cur_holdings if c not in target_codes]
    to_keep = [c for c in cur_holdings if c in target_codes]
    to_buy = [c for c in target_codes if c not in cur_holdings]

    # ---- 调仓动作预览 ----
    if to_sell or to_buy:
        log.info("")
        log.info("  🔄 调仓动作:")

    if to_sell:
        log.info("    卖出(%d只):", len(to_sell))
        current_data = get_current_data()
        for code in to_sell:
            pos = context.portfolio.positions.get(code)
            if pos and pos.total_amount > 0:
                price = current_data[code].last_price if code in current_data else 0
                cost = g.entry_prices.get(code, pos.avg_cost)
                pnl = (price - cost) / cost * 100 if cost > 0 else 0
                name = current_data[code].name if code in current_data else code
                log.info("      🚫 %s %s  成本%.2f 现价%.2f  盈亏%+.1f%%  市值%,.0f",
                         code, name, cost, price, pnl, pos.total_amount * price)

    if to_buy:
        log.info("    买入(%d只):", len(to_buy))
        current_data = get_current_data()
        for code in to_buy:
            info = current_data[code] if code in current_data else None
            price = info.last_price if info else 0
            name = info.name if info else code
            # 找到对应的排名信息
            rank_info = next((r for r in target_stocks if r["code"] == code), None)
            score_str = f" 评分{rank_info['total_score']:.1f}" if rank_info else ""
            log.info("      ✅ %s %s  现价%.2f%s", code, name, price, score_str)

    # Step 3: 执行卖出
    for code in to_sell:
        pos = context.portfolio.positions.get(code)
        if pos and pos.total_amount > 0:
            log.info("  [执行] 卖出 %s (%d股)", code, pos.total_amount)
            order_target(code, 0)
            _cleanup_position(code)

    # Step 4: 执行买入
    total_slots = len(to_keep) + len(to_buy)
    if total_slots > 0 and to_buy:
        per_stock_value = total_value / total_slots
        current_data = get_current_data()
        for code in to_buy:
            if code in current_data and not current_data[code].paused:
                price = current_data[code].last_price
                if price > 0:
                    target_value = min(per_stock_value, available_cash * 0.95 / max(len(to_buy), 1))
                    shares = int(target_value / price / 100) * 100
                    if shares < 100:
                        log.info("  [跳过] %s 资金不足100股 (单价%.2f)", code, price)
                        continue
                    order_target_value(code, target_value)
                    g.entry_prices[code] = price
                    g.highest_prices[code] = price
                    g.entry_dates[code] = today
                    log.info("  [执行] 买入 %s %.2f元 (%d股 @%.2f)",
                             code, target_value, shares, price)

    # ---- 调仓后预计持仓 ----
    _print_expected_holdings(target_codes, to_keep, to_buy, context, total_slots)

    log.info("")
    log.info("  ✅ 调仓完成")
    log.info("=" * 60)


def _print_holdings_snapshot(context, title, codes):
    """打印当前持仓明细表"""
    if not codes:
        log.info("  %s: 空仓", title)
        return

    current_data = get_current_data()
    total_pos_value = 0
    total_pnl = 0

    log.info("  %s:", title)
    log.info("  %-10s %-8s %8s %8s %8s %8s %5s %8s",
             "代码", "名称", "成本价", "现价", "盈亏%", "市值", "持仓天", "最高价")
    log.info("  " + "-" * 70)

    for code in codes:
        pos = context.portfolio.positions.get(code)
        if not pos or pos.total_amount <= 0:
            continue
        price = current_data[code].last_price if code in current_data else 0
        cost = g.entry_prices.get(code, pos.avg_cost)
        pnl = (price - cost) / cost * 100 if cost > 0 else 0
        name = current_data[code].name if code in current_data else code
        hold_days = (context.current_dt.date() - g.entry_dates.get(code, context.current_dt.date())).days
        highest = g.highest_prices.get(code, cost)
        pos_value = pos.total_amount * price
        total_pos_value += pos_value
        total_pnl += (price - cost) * pos.total_amount

        log.info("  %-10s %-8s %8.2f %8.2f %+7.2f%% %,8.0f %4d天 %8.2f",
                 code, name[:8], cost, price, pnl, pos_value, hold_days, highest)

    log.info("  " + "-" * 70)
    log.info("  持仓市值合计: %,.0f  |  浮动盈亏: %+,.0f", total_pos_value, total_pnl)


def _print_expected_holdings(target_codes, to_keep, to_buy, context, total_slots):
    """打印调仓后预计持仓结构"""
    log.info("")
    log.info("  📋 调仓后持仓结构 (等权, 共%d只):", total_slots)
    log.info("  %-10s %-8s %8s %12s",
             "代码", "状态", "权重%", "说明")
    log.info("  " + "-" * 45)

    total_value = context.portfolio.total_value
    weight = 100.0 / total_slots if total_slots > 0 else 0

    for code in to_keep:
        pos = context.portfolio.positions.get(code)
        current_weight = (pos.total_amount * pos.price / total_value * 100) if pos and pos.total_amount > 0 else 0
        log.info("  %-10s %-8s %7.1f%%  持有中(当前%.1f%%)",
                 code, "📌 保留", weight, current_weight)

    for code in to_buy:
        log.info("  %-10s %-8s %7.1f%%  新买入",
                 code, "🆕 新增", weight)

    # 已卖出的
    for code, pos in list(context.portfolio.positions.items()):
        if code not in to_keep and pos.total_amount > 0:
            log.info("  %-10s %-8s %7s  等待卖出成交",
                     code, "🔜 待卖", "-")
    log.info("  " + "-" * 45)


def _get_stock_pool(context):
    """获取股票池"""
    pool = g.stock_pool

    if pool is None:
        # 全A股 + 排除ST
        try:
            pool = list(get_all_securities(['stock']).index)
        except Exception:
            pool = list(get_index_stocks('000300.XSHG'))

    # 过滤
    current_data = get_current_data()
    filtered = []
    for code in pool:
        if code not in current_data:
            continue
        info = current_data[code]
        # 停牌
        if info.paused:
            continue
        # ST
        if g.exclude_st and (info.is_st or 'ST' in info.name):
            continue
        # 上市不足60天
        if g.exclude_new_list:
            try:
                days_listed = (context.current_dt.date() - info.start_date).days
                if days_listed < 60:
                    continue
            except Exception:
                pass
        # 最低价
        if info.last_price < g.min_price:
            continue
        filtered.append(code)

    # 限制数量（全A股太多，取前500只提高速度）
    if len(filtered) > 500:
        import random
        filtered = filtered[:500]

    return filtered


# ============================================================================
# 多因子打分（核心，完全移植自 quant_score.py）
# ============================================================================

def score_one_stock(code, context):
    """
    对单只股票进行多因子打分。
    完全移植 investment/core/quant_score.py 的逻辑。
    返回 {"code":..., "total_score":..., ...} 或 None
    """
    current_data = get_current_data()
    if code not in current_data or current_data[code].paused:
        return None

    price = current_data[code].last_price
    if price <= 0:
        return None

    # 获取K线数据
    try:
        df = attribute_history(code, g.lookback_days, '1d',
                               ['open', 'close', 'high', 'low', 'volume'])
        if df is None or len(df) < 60:
            return None
    except Exception:
        return None

    # PE过滤
    pe = current_data[code].pe_ratio
    if pe and pe > g.max_pe:
        return None

    # 计算技术指标（移植自 compute_indicators）
    df = _compute_indicators(df)

    # 获取最新信号
    signals = _get_latest_signals(df, price)

    # 多因子打分
    total = _calc_total_score(signals)

    return {
        "code": code,
        "name": current_data[code].name,
        "price": price,
        "total_score": total,
        "signals": signals,
    }


# ============================================================================
# 技术指标计算（移植自 stock_strategy.py → compute_indicators）
# ============================================================================

def _compute_indicators(df):
    """在聚宽K线数据上计算全部技术指标"""
    close = df["close"]
    volume = df["volume"]
    high = df["high"]
    low = df["low"]

    # MA
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

    # RSI(14)
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

    # ATR(14)
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    # Consecutive up/down
    df["is_up"] = (close > close.shift(1)).astype(int)
    df["is_down"] = (close < close.shift(1)).astype(int)

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

    # CMF (Chaikin Money Flow)
    mf_mult = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    mf_vol = mf_mult * volume
    df["cmf_20d"] = mf_vol.rolling(20).sum() / volume.rolling(20).sum()

    # 量价相关性
    df["price_vol_corr_10d"] = close.rolling(10).corr(volume)

    # 价格位置
    for w in [20, 60]:
        df[f"high_{w}d"] = high.rolling(w).max()
        df[f"low_{w}d"] = low.rolling(w).min()
        df[f"price_pos_{w}d"] = (
            (close - df[f"low_{w}d"])
            / (df[f"high_{w}d"] - df[f"low_{w}d"]).replace(0, np.nan)
            * 100
        )

    # 波动率
    df["volatility_5d"] = close.pct_change().rolling(5).std() * 100

    return df


def _get_latest_signals(df, price):
    """获取最新信号（移植自 stock_strategy.py → get_latest_signals）"""
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    def rnd(val, d=2):
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return None
        return round(float(val), d)

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
        "vol_ma5": rnd(latest["vol_ma5"], 0),
        "vol_ma20": rnd(latest["vol_ma20"], 0),
        "cmf_20d": rnd(latest["cmf_20d"], 4),
        "price_vol_corr_10d": rnd(latest["price_vol_corr_10d"], 3),
        "volatility_5d": rnd(latest["volatility_5d"]),
        "high_20d": rnd(latest["high_20d"]),
        "low_20d": rnd(latest["low_20d"]),
        "high_60d": rnd(latest["high_60d"]),
        "low_60d": rnd(latest["low_60d"]),
        "price_pos_20d": rnd(latest.get("price_pos_20d"), 1),
        "price_pos_60d": rnd(latest.get("price_pos_60d"), 1),
    }

    # OBV trend
    obv10 = latest.get("obv_ma10")
    obv20 = latest.get("obv_ma20")
    if obv10 is not None and obv20 is not None and not np.isnan(obv10) and not np.isnan(obv20):
        signals["obv_trend"] = "多头" if obv10 > obv20 else "空头"
    else:
        signals["obv_trend"] = None

    # Consecutive
    last_idx = len(df) - 1
    signals["consecutive_up"] = _count_consecutive(df["is_up"], last_idx, 1)
    signals["consecutive_down"] = _count_consecutive(df["is_down"], last_idx, 1)

    # MACD cross
    signals["macd_cross"] = "金叉" if latest["dif"] > latest["dea"] and prev["dif"] <= prev["dea"] else (
        "死叉" if latest["dif"] < latest["dea"] and prev["dif"] >= prev["dea"] else "无交叉"
    )
    signals["macd_trend"] = "多头" if latest["dif"] > latest["dea"] else "空头"

    # BB position
    bb_u = signals["bb_upper"]
    bb_l = signals["bb_lower"]
    if bb_u and bb_l and (bb_u - bb_l) > 0:
        signals["bb_position"] = rnd((price - bb_l) / (bb_u - bb_l) * 100, 1)

    return signals


def _count_consecutive(series, idx, value):
    cnt = 0
    for i in range(idx, -1, -1):
        if series.iloc[i] == value:
            cnt += 1
        else:
            break
    return cnt


# ============================================================================
# 多因子打分函数（完全移植自 quant_score.py）
# ============================================================================

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _score_trend(signals):
    """趋势因子 满分25"""
    price = signals.get("current_price", 0)
    ma5 = signals.get("ma5") or 0
    ma10 = signals.get("ma10") or 0
    ma20 = signals.get("ma20") or 0
    ma60 = signals.get("ma60") or 0

    if not all([ma5, ma10, ma20, ma60]) or price <= 0:
        return 5.0

    gap_5_10 = (ma5 / ma10 - 1) * 100 if ma10 > 0 else -99
    gap_10_20 = (ma10 / ma20 - 1) * 100 if ma20 > 0 else -99
    gap_price_ma20 = (price / ma20 - 1) * 100 if ma20 > 0 else -99

    def gap_score(gap):
        if gap < -5:
            return 0.0
        elif gap < 0:
            return 1.0 + gap / 5
        elif gap <= 3:
            return 2.0 + gap / 3 * 2.0
        elif gap <= 8:
            return 4.0 - (gap - 3) / 5 * 2.0
        else:
            return max(0.0, 2.0 - (gap - 8) / 10 * 2.0)

    align_score = (gap_score(gap_5_10) + gap_score(gap_10_20) + gap_score(gap_price_ma20)) / 3 * 3

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

    if signals.get("ma5_vs_ma20") == "金叉" or (ma5 > ma20):
        dir_score = 5.0 if gap_price_ma20 > 2 else (2.5 + gap_price_ma20 / 2 * 1.25 if gap_price_ma20 > 0 else 2.0 + gap_price_ma20 / 2 * 1.0)
    else:
        dir_score = max(0.0, 2.0 + gap_price_ma20 / 5 * 2.0)

    return _clamp(align_score + ma60_score + dir_score, 0, 25)


def _score_position(signals):
    """位置因子 满分25"""
    high_60d = signals.get("high_60d")
    low_20d = signals.get("low_20d")
    price = signals.get("current_price", 0)

    if high_60d is None or high_60d <= 0 or price <= 0:
        return 8.0

    drawdown = (high_60d - price) / high_60d * 100

    if drawdown <= 3:
        dd_score = 5.0 - drawdown / 3 * 1.0
    elif drawdown <= 8:
        dd_score = 4.0 + (drawdown - 3) / 5 * 11.0
    elif drawdown <= 20:
        dd_score = 15.0 + (drawdown - 8) / 12 * 5.0
    elif drawdown <= 30:
        dd_score = 20.0 - (drawdown - 20) / 10 * 12.0
    elif drawdown <= 45:
        dd_score = 8.0 - (drawdown - 30) / 15 * 5.0
    else:
        dd_score = max(0.0, 3.0 - (drawdown - 45) / 10 * 3.0)

    low_score = 2.5
    if low_20d and low_20d > 0:
        above_low = (price / low_20d - 1) * 100
        if 5 <= above_low <= 25:
            low_score = 5.0 - abs(above_low - 15) / 10 * 3.0
        elif above_low < 5:
            low_score = 2.0 + above_low / 5 * 2.0
        else:
            low_score = max(0.0, 2.0 - (above_low - 25) / 20 * 2.0)

    return _clamp(dd_score + low_score, 0, 25)


def _score_volume_price(signals):
    """量价因子 满分20"""
    vol_ma5 = signals.get("vol_ma5") or 0
    vol_ma20 = signals.get("vol_ma20") or 0
    obv_trend = signals.get("obv_trend", "")
    cmf = signals.get("cmf_20d") or 0
    corr = signals.get("price_vol_corr_10d")

    if vol_ma20 > 0:
        vol_ratio = vol_ma5 / vol_ma20
        if 1.0 <= vol_ratio <= 2.5:
            vol_score = 4.0 + (vol_ratio - 1.0) / 1.5 * 4.0
        elif 0.7 <= vol_ratio < 1.0:
            vol_score = 1.0 + (vol_ratio - 0.7) / 0.3 * 3.0
        elif vol_ratio > 2.5:
            vol_score = max(2.0, 8.0 - (vol_ratio - 2.5) / 3.0 * 6.0)
        else:
            vol_score = max(0.0, vol_ratio / 0.7 * 1.0)
    else:
        vol_score = 3.0

    if obv_trend == "多头":
        obv_score = 6.0
    elif obv_trend == "空头":
        obv_score = 0.0
    else:
        obv_score = 3.0

    cmf_score = _clamp(1.5 + (cmf or 0) * 15, 0, 3)

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

    return _clamp(vol_score + obv_score + cmf_score + corr_score, 0, 20)


def _score_rsi(signals):
    """RSI因子 满分15"""
    rsi = signals.get("rsi14")
    if rsi is None:
        return 7.5

    center = 52.0
    if rsi > center:
        diff = rsi - center
        if diff <= 18:
            score = 15.0 - (diff / 18) ** 1.5 * 10.0
        else:
            score = max(0.0, 5.0 - (diff - 18) / 30 * 5.0)
    else:
        diff = center - rsi
        if diff <= 22:
            score = 15.0 - (diff / 22) ** 2.0 * 8.0
        else:
            score = max(0.0, 7.0 - (diff - 22) / 20 * 7.0)

    return _clamp(score, 0, 15)


def _score_volatility(signals):
    """波动率因子 满分15"""
    atr = signals.get("atr14")
    price = signals.get("current_price", 0)
    vol5 = signals.get("volatility_5d")

    if atr is None or atr <= 0 or price <= 0:
        return 7.5

    atr_pct = (atr / price) * 100

    if atr_pct <= 1:
        atr_score = 3.0 + atr_pct / 1.0 * 4.0
    elif atr_pct <= 2:
        atr_score = 7.0 + (atr_pct - 1) / 1.0 * 6.0
    elif atr_pct <= 5:
        atr_score = 13.0 + (atr_pct - 2) / 3.0 * 2.0
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

    return _clamp(atr_score + vol5_adj, 0, 15)


def _score_bonus(signals):
    """附加项 ±13"""
    macd_cross = signals.get("macd_cross", "")
    macd_hist = signals.get("macd_hist") or 0
    price = signals.get("current_price", 1)
    dif = signals.get("dif") or 0
    dea = signals.get("dea") or 0

    hist_pct = abs(macd_hist) / price * 100 if price > 0 else 0

    if macd_cross == "金叉":
        macd_score = 5.0 + min(hist_pct * 80, 2.0)
    elif macd_cross == "死叉":
        macd_score = -5.0 - min(hist_pct * 80, 2.0)
    elif dif > dea:
        macd_score = 1.0 + min(hist_pct * 50, 2.0)
    else:
        macd_score = -1.0 - min(hist_pct * 50, 2.0)

    cons_up = signals.get("consecutive_up", 0)
    cons_down = signals.get("consecutive_down", 0)
    cons_score = 0.0
    if cons_down >= 3:
        cons_score += min(cons_down - 2, 3) * 1.0
    if cons_up >= 4:
        cons_score -= min(cons_up - 3, 3) * 1.0

    bb_score = 0.0
    bb_u = signals.get("bb_upper")
    bb_l = signals.get("bb_lower")
    bb_m = signals.get("bb_mid")
    if bb_u and bb_l and bb_m and bb_m > 0:
        bb_width = (bb_u - bb_l) / bb_m * 100
        if bb_width < 5:
            bb_score -= 2.0
        elif bb_width < 8:
            bb_score -= 0.5
        bb_pos = signals.get("bb_position")
        if bb_pos is not None:
            if bb_pos < 15:
                bb_score += 2.0
            elif bb_pos > 85:
                bb_score -= 1.5

    return _clamp(macd_score + cons_score + bb_score, -13, 13)


def _calc_total_score(signals):
    """汇总各因子得分"""
    s1 = _score_trend(signals)
    s2 = _score_position(signals)
    s3 = _score_volume_price(signals)
    s4 = _score_rsi(signals)
    s5 = _score_volatility(signals)
    s6 = _score_bonus(signals)

    total = s1 + s2 + s3 + s4 + s5 + s6
    return round(max(0.0, min(100.0, total)), 1)
