"""
量化策略回测脚本
用法: python backtest.py 600519 2020-01-01 2026-01-01 [策略名]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from game_engine import StockDataFetcher
from strategies import STRATEGIES
import numpy as np

def backtest(code, start, end, strategy_name='unified', init_cash=100000):
    print(f"获取 {code} 数据...")
    fetcher = StockDataFetcher()
    klines = fetcher.get_kline(code, start_date=start)
    if not klines:
        print("获取数据失败")
        return
    
    klines = [k for k in klines if start <= k['date'] <= end]
    if len(klines) < 60:
        print(f"数据不足：{len(klines)}天")
        return
    
    strat = STRATEGIES.get(strategy_name)
    if strat is None:
        strat = STRATEGIES['unified']
    # Recreate strategy for clean state
    strat.__init__()
    print(f"策略: {strat.name}  |  {len(klines)}天  |  {start} -> {end}")
    
    # Setup minimal game engine for strategy
    from game_engine import GameEngine
    g = GameEngine()
    g.stocks[code] = {'name': code, 'klines': klines, 'date_map': {k['date']: k for k in klines}}
    g.start_date = start
    g.initial_cash = init_cash
    g.dates = [k['date'] for k in klines]
    g.total_days = len(g.dates)
    g.game_started = True
    
    # Manual portfolio tracking (avoid GameEngine buy/sell complexity)
    cash = init_cash
    shares = 0
    entry_price = 0
    trades = []
    peak_value = init_cash
    max_dd = 0
    
    init_price = klines[0]['close']
    benchmark_shares = int(init_cash / (init_price * 100)) * 100
    
    print(f"\n{'日期':<12} {'操作':<8} {'价格':<10} {'手数':<6} {'金额':<14} {'总资产':<14} {'累计%':<10} {'基准%':<10}")
    print("-" * 100)
    
    for day in range(g.total_days):
        g.current_day = day
        price = klines[day]['close']
        
        trade_str = ""
        result = strat.analyze(code, g)
        action = result['action']
        reason = result.get('reason', '')
        
        if action == 'BUY' and shares == 0:
            max_lots = int(cash / (price * 100))
            if max_lots == 0 and cash >= price * 100:
                max_lots = 1
            if max_lots > 0:
                cost = max_lots * price * 100
                commission = max(5.0, cost * 0.00025)
                cash -= (cost + commission)
                shares = max_lots * 100
                entry_price = price
                g.portfolio[code] = {'shares': shares, 'cost_basis': price}
                trades.append(('BUY', klines[day]['date'], price, max_lots, reason))
                trade_str = f"买{max_lots}手"
            else:
                trade_str = f"买信号(资金不足,需¥{price*100:,.0f})"
        
        elif action == 'SELL' and shares > 0:
            proceeds = shares * price
            commission = max(5.0, proceeds * 0.00025)
            stamp = proceeds * 0.001
            cash += (proceeds - commission - stamp)
            trades.append(('SELL', klines[day]['date'], price, shares//100, reason))
            trade_str = f"卖{shares//100}手"
            shares = 0
            # Sync to GameEngine
            g.portfolio.pop(code, None)
        
        # Final day: force sell and count as trade
        if day == g.total_days - 1 and shares > 0:
            proceeds = shares * price
            commission = max(5.0, proceeds * 0.00025)
            stamp = proceeds * 0.001
            cash += (proceeds - commission - stamp)
            trades.append(('SELL(清仓)', klines[day]['date'], price, shares//100, '强制清仓'))
            trade_str = f"清仓{shares//100}手"
            shares = 0
        
        total = cash + shares * price
        total_pct = (total - init_cash) / init_cash * 100
        bench_pct = (price - init_price) / init_price * 100
        peak_value = max(peak_value, total)
        dd = (peak_value - total) / peak_value * 100 if peak_value > 0 else 0
        max_dd = max(max_dd, dd)
        
        if trade_str or day == 0 or day == g.total_days - 1:
            print(f"{klines[day]['date']:<12} {trade_str:<8} {price:<10.2f} {shares//100 if shares else 0:<6} ¥{total:,.0f}   {total_pct:+7.1f}%  {bench_pct:+7.1f}%")
    
    # Results
    final_total = cash + shares * price
    final_pnl = (final_total - init_cash) / init_cash * 100
    bench_pnl = (klines[-1]['close'] - init_price) / init_price * 100
    
    years = len(klines) / 252
    ann_ret = ((final_total / init_cash) ** (1 / max(years, 0.1)) - 1) * 100
    bench_ann = ((klines[-1]['close'] / init_price) ** (1 / max(years, 0.1)) - 1) * 100
    
    buys = [t for t in trades if t[0] == 'BUY']
    sells = [t for t in trades if t[0].startswith('SELL')]
    wins = sum(1 for i in range(min(len(buys), len(sells))) if sells[i][2] > buys[i][2])
    win_rate = wins / len(buys) * 100 if buys else 0

    # Hold duration
    hold_days = []
    for i in range(len(buys)):
        sd = next((j for j, d in enumerate(klines) if d['date'] == sells[i][1]), len(klines)-1) if i < len(sells) else len(klines)-1
        bd = next((j for j, d in enumerate(klines) if d['date'] == buys[i][1]), 0)
        hold_days.append(sd - bd)
    avg_hold = np.mean(hold_days) if hold_days else 0
    total_trades = len(buys) + len(sells)
    
    excess = final_pnl - bench_pnl
    
    print()
    print("=" * 65)
    print(f"  {code} | {start} -> {end} | {strat.name}")
    print("=" * 65)
    print(f"  交易日: {len(klines)} ({years:.1f}年)")
    print(f"  交易: {len(buys)}买 {len(sells)}卖 | 胜率: {win_rate:.1f}% | 持仓{avg_hold:.0f}天")
    print(f"  最大回撤: {max_dd:.1f}%")
    if trades:
        print(f"\n  交易明细:")
        for i, t in enumerate(trades):
            pnl = ""
            if t[0].startswith('SELL') and i > 0:
                prev = trades[i-1]
                if prev[0] == 'BUY':
                    p = (t[2] - prev[2]) / prev[2] * 100
                    pnl = f"  {p:+.1f}%"
            print(f"  {t[1]} {t[0]:8s} ¥{t[2]:.2f} {t[3]}手{pnl}")
    print()
    print(f"  {'':<18} {'策略':>14} {'满仓持有':>14} {'超额':>14}")
    print(f"  {'最终收益率':<18} {final_pnl:>+13.1f}% {bench_pnl:>+13.1f}% {excess:>+13.1f}%")
    print(f"  {'年化收益率':<18} {ann_ret:>+13.1f}% {bench_ann:>+13.1f}% {'—':>14}")
    print(f"  {'最终资金':<18} ¥{final_total:>13,.0f} ¥{benchmark_shares*klines[-1]['close']+init_cash-benchmark_shares*init_price:>13,.0f} {'—':>14}")
    print()
    verdict = "策略跑赢基准" if excess > 0 else "策略不如满仓持有"
    print(f"  >>> {verdict} (超额 {excess:+.1f}%) <<<")
    print("=" * 65)


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print("用法: python backtest.py <代码> <开始> <结束> [策略]")
        print("示例: python backtest.py 600519 2020-01-01 2026-01-01")
        sys.exit(1)
    
    backtest(sys.argv[1], sys.argv[2], sys.argv[3],
             sys.argv[4] if len(sys.argv) > 4 else 'unified',
             float(sys.argv[5]) if len(sys.argv) > 5 else 100000)
