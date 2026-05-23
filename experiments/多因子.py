# 克隆自聚宽文章：https://www.joinquant.com/post/1399
# 标题：【量化课堂】多因子策略入门
# 作者：JoinQuant量化课堂

# -*- coding: utf-8 -*-
"""
V7 激进高频轮动策略 - 最终生产版（修复context未定义）
核心逻辑：
- 选股：放弃20日涨幅第一名，从第2名开始取最多3只（涨幅≥2%，量比≥1.2，最低价≥5元）
- 调仓：严格先卖后买，等待卖出全部成交（或停牌跳过）后才用最新现金买入
- 风控：买入次日生效，五级止盈止损（回撤4%、固定止损3.5%、保本、保利5%、5日线）
"""

import numpy as np
from jqdata import *

def initialize(context):
    # 策略参数
    g.max_stocks = 3
    g.lookback_days = 25
    g.min_gain = 0.02          # 20日涨幅≥2%
    g.min_vol_ratio = 1.2      # 量比≥1.2
    g.min_price = 5            # 最低股价≥5元
    g.stop_loss_fixed = 0.035  # 固定止损3.5%
    g.trail_rebate = 0.04      # 回撤4%止盈

    # 状态变量
    g.last_rebalance = None
    g.entry_prices = {}
    g.highest_prices = {}
    g.entry_dates = {}
    g.buy_dict = {}
    g.sell_trigger_today = []

    # 调仓状态机
    g.pending_sells = []
    g.pending_buys = []
    g.rebalance_phase = 0
    g.target_stocks = []

    # 定时任务
    run_daily(select_stocks, time='9:25')
    run_daily(rebalance, time='9:30')
    run_daily(check_stop, time='every_bar')

    # 基础设置
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    log.set_level('order', 'error')
    set_order_cost(OrderCost(close_tax=0.001, open_commission=0.0003,
                             close_commission=0.0003, min_commission=5), type='stock')


def select_stocks(context):
    """选股：放弃20日涨幅第一名，从第2名开始取最多g.max_stocks只"""
    current_data = get_current_data()
    pool = list(set(get_index_stocks('000905.XSHG') + get_index_stocks('399006.XSHE')))
    pool = [s for s in pool if not current_data[s].paused and not current_data[s].is_st
            and not s.startswith('688') and current_data[s].day_open > 0]

    candidates = []
    for stock in pool[:500]:
        try:
            df = get_price(stock, end_date=context.current_dt, count=g.lookback_days+5,
                          frequency='daily', fields=['close', 'volume'])
            if len(df) < g.lookback_days:
                continue
            c = df['close'].values
            v = df['volume'].values
            ret_20 = (c[-1] / c[-20]) - 1
            if ret_20 < g.min_gain:
                continue
            avg_vol = np.mean(v[-20:])
            if avg_vol <= 0:
                continue
            vol_ratio = v[-1] / avg_vol
            if vol_ratio < g.min_vol_ratio:
                continue
            if current_data[stock].day_open < g.min_price:
                continue
            candidates.append((stock, ret_20))
        except:
            continue

    if len(candidates) <= 1:
        g.target_stocks = []
        log.info("选股无结果（候选≤1）")
        return
    candidates.sort(key=lambda x: x[1], reverse=True)
    g.target_stocks = [c[0] for c in candidates[1:1+g.max_stocks]]
    log.info(f"选股放弃第一名: {candidates[0][0]} 涨{candidates[0][1]*100:.2f}% → 买入 {g.target_stocks}")


def safe_get_amount(stock, context):
    """安全获取持仓数量，不触发聚宽警告"""
    pos = context.portfolio.positions.get(stock)
    return pos.total_amount if pos and pos.total_amount > 0 else 0


def order_sell(stock, context, amount=None):
    """卖出股票（指定数量或全部）"""
    if amount is None:
        amount = safe_get_amount(stock, context)
    if amount >= 100:
        order_target_value(stock, 0)
        log.info(f"【卖出】{stock} {amount}股")
        return True
    else:
        log.warn(f"【卖出跳过】{stock} 持仓{amount}股不足100股")
        return False


def order_buy(stock, target_value, context):
    """买入股票到目标市值（确保能买至少100股）"""
    current_data = get_current_data()
    if stock not in current_data or current_data[stock].paused:
        return False
    price = current_data[stock].day_open
    if price <= 0:
        return False
    shares = int(target_value / price / 100) * 100
    if shares < 100:
        log.info(f"【买入跳过】{stock} 资金不足100股 (目标{target_value:.2f}元)")
        return False
    if context.portfolio.cash < target_value:
        log.info(f"【买入跳过】{stock} 可用资金不足")
        return False
    order_target_value(stock, target_value)
    log.info(f"【买入】{stock} {shares}股 目标市值{target_value:.0f}")
    return True


def rebalance(context):
    """严格先卖后买，处理停牌情况"""
    today = context.current_dt.date()
    if today.weekday() >= 5:
        return
    if g.last_rebalance == today and g.rebalance_phase == 0:
        return
    g.last_rebalance = today

    # 阶段0：确定需要卖出和买入的股票
    if g.rebalance_phase == 0:
        cur_holdings = [s for s, pos in context.portfolio.positions.items() if pos.total_amount > 0]
        to_sell = []
        for stock in cur_holdings:
            if stock not in g.target_stocks:
                buy_date = g.buy_dict.get(stock)
                days_held = (today - buy_date).days if buy_date else 0
                if days_held >= 5 or stock in g.sell_trigger_today:
                    to_sell.append(stock)
        to_buy = [s for s in g.target_stocks if s not in cur_holdings]

        if not to_sell and not to_buy:
            return

        if to_sell:
            log.info(f"【调仓阶段1】开始卖出: {to_sell}")
            g.rebalance_phase = 1
            g.pending_sells = to_sell[:]
            g.pending_buys = to_buy[:]
            for stock in to_sell:
                order_sell(stock, context)
        else:
            # 无卖出，直接买入
            log.info(f"【调仓阶段2】直接买入: {to_buy}")
            if to_buy:
                available_cash = context.portfolio.cash * 0.95
                per_value = available_cash / len(to_buy)
                for stock in to_buy:
                    if order_buy(stock, per_value, context):
                        g.entry_dates[stock] = today
                        g.buy_dict[stock] = today
                g.rebalance_phase = 0
                g.sell_trigger_today = []
            return

    # 阶段1：等待卖出全部成交，处理停牌
    if g.rebalance_phase == 1:
        current_data = get_current_data()
        remaining = []
        for stock in g.pending_sells:
            amount = safe_get_amount(stock, context)
            if amount > 0:
                # 检查是否停牌，如果停牌则无法卖出，跳过并记录
                if stock in current_data and current_data[stock].paused:
                    log.warn(f"【停牌跳过】{stock} 无法卖出，从等待列表中移除")
                    continue
                remaining.append(stock)
        if remaining:
            log.info(f"【等待卖出】剩余: {remaining}")
            return
        else:
            # 卖出全部完成（或停牌跳过）
            log.info("【卖出完成】进入买入阶段")
            g.rebalance_phase = 2

    # 阶段2：买入（所有可卖出股票已处理完成）
    if g.rebalance_phase == 2:
        if not g.pending_buys:
            g.rebalance_phase = 0
            return
        available_cash = context.portfolio.cash * 0.95
        per_value = available_cash / len(g.pending_buys)
        log.info(f"【买入阶段】目标: {g.pending_buys}, 每只{per_value:.0f}")
        today = context.current_dt.date()
        for stock in g.pending_buys:
            if order_buy(stock, per_value, context):
                g.entry_dates[stock] = today
                g.buy_dict[stock] = today
        g.rebalance_phase = 0
        g.pending_sells = []
        g.pending_buys = []
        g.sell_trigger_today = []


def get_ma5(stock, context):
    """获取股票5日均线（不含今日盘中）"""
    try:
        df = get_price(stock, count=6, end_date=context.previous_date,
                       frequency='daily', fields=['close'], skip_paused=True)
        if len(df) >= 5:
            return df['close'].rolling(5).mean().iloc[-1]
    except:
        pass
    return None


def check_stop(context):
    """五级止盈止损（买入次日生效）"""
    today = context.current_dt.date()
    current_data = get_current_data()
    g.sell_trigger_today = []

    for stock, pos in context.portfolio.positions.items():
        if pos.total_amount == 0:
            continue
        if stock not in current_data or current_data[stock].paused:
            continue

        # 初始化新持仓记录
        if stock not in g.entry_dates:
            cost = pos.avg_cost
            if cost > 0:
                g.entry_prices[stock] = cost
                g.entry_dates[stock] = today
                g.highest_prices[stock] = cost
                g.buy_dict[stock] = today
            else:
                continue

        # 买入当天不风控
        if g.entry_dates[stock] == today:
            continue

        cur = current_data[stock].last_price
        if cur <= 0:
            continue

        cost = g.entry_prices.get(stock, pos.avg_cost)
        if cost <= 0:
            continue

        high = max(g.highest_prices.get(stock, cost), cur)
        g.highest_prices[stock] = high
        ret = (cur - cost) / cost
        drawdown = (high - cur) / high if high > 0 else 0

        # 1. 回撤止盈
        if drawdown >= g.trail_rebate:
            log.info(f"【回撤止盈】{stock} 回撤{drawdown*100:.2f}%")
            if order_sell(stock, context):
                g.sell_trigger_today.append(stock)
                g.entry_prices.pop(stock, None)
                g.highest_prices.pop(stock, None)
                g.entry_dates.pop(stock, None)
            continue

        # 2. 固定止损
        if ret <= -g.stop_loss_fixed:
            log.info(f"【固定止损】{stock} 亏损{ret*100:.2f}%")
            if order_sell(stock, context):
                g.sell_trigger_today.append(stock)
                g.entry_prices.pop(stock, None)
                g.highest_prices.pop(stock, None)
                g.entry_dates.pop(stock, None)
            continue

        # 3. 保本止损
        if ret >= 0.03 and cur <= cost:
            log.info(f"【保本止损】{stock}")
            if order_sell(stock, context):
                g.sell_trigger_today.append(stock)
                g.entry_prices.pop(stock, None)
                g.highest_prices.pop(stock, None)
                g.entry_dates.pop(stock, None)
            continue

        # 4. 保利止损
        if 0.05 <= ret < 0.10:
            stop_price = cost * 1.05
            if cur <= stop_price:
                log.info(f"【保利止损】{stock} 触发{stop_price:.2f}")
                if order_sell(stock, context):
                    g.sell_trigger_today.append(stock)
                    g.entry_prices.pop(stock, None)
                    g.highest_prices.pop(stock, None)
                    g.entry_dates.pop(stock, None)
                continue

        # 5. 均线止损
        if ret >= 0.10:
            ma5 = get_ma5(stock, context)
            if ma5 is not None and cur < ma5:
                log.info(f"【均线止损】{stock} 价{cur:.2f}<5日线{ma5:.2f}")
                if order_sell(stock, context):
                    g.sell_trigger_today.append(stock)
                    g.entry_prices.pop(stock, None)
                    g.highest_prices.pop(stock, None)
                    g.entry_dates.pop(stock, None)
                continue


def after_trading_end(context):
    log.info("\n" + "="*60)
    log.info(f"收盘总资产: {context.portfolio.total_value:.2f}  现金: {context.portfolio.cash:.2f}")
    for stock, pos in context.portfolio.positions.items():
        if pos.total_amount > 0:
            log.info(f"{stock} 数量{pos.total_amount} 成本{pos.avg_cost:.2f} 现价{pos.price:.2f} 盈亏{(pos.price/pos.avg_cost-1)*100:+.2f}% 市值{pos.total_amount*pos.price:.2f}")
    log.info("="*60)
