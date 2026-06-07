"""
批量策略回测 — 15只股票 × N个策略
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from game_engine import StockDataFetcher, GameEngine
from strategies import STRATEGIES
from strategies_backup.v1_tight_stops import UnifiedStrategy as V1Strat
import numpy as np

STOCKS = [
    # 5 flat
    ('003816', '中国广核', 'flat'),
    ('601985', '中国核电', 'flat'),
    ('601006', '大秦铁路', 'flat'),
    ('600900', '长江电力', 'flat'),
    ('601988', '中国银行', 'flat'),
    # 5 doubled+
    ('002371', '北方华创', 'up'),
    ('002463', '沪电股份', 'up'),
    ('603986', '兆易创新', 'up'),
    ('300274', '阳光电源', 'up'),
    ('600176', '中国巨石', 'up'),
    # 5 declined
    ('002466', '天齐锂业', 'down'),
    ('600026', '中远海能', 'down'),
    ('300498', '温氏股份', 'down'),
    ('002714', '牧原股份', 'down'),
    ('000651', '格力电器', 'down'),
]

def make_strategies():
    """Generate 10 strategy variants by tuning parameters."""
    strats = []
    
    # 1. Current v2 (baseline)
    from strategies import UnifiedStrategy as V2
    s = V2()
    s.name = "S1_v2标准版"
    strats.append(s)
    
    # 2. v1 tight stops
    s = V1Strat()
    s.name = "S2_v1紧密止损"
    strats.append(s)
    
    # 3. v2 + no cooldown
    class NoCooldown(V2):
        name = "S3_无冷却"
        def analyze(self, code, game):
            self.last_sell_day = {}
            return super().analyze(code, game)
    strats.append(NoCooldown())
    
    # 4. v2 + wider stops (-20% hard, -25% trailing)
    class WideStops(V2):
        name = "S4_宽止损"
        def analyze(self, code, game):
            self._hard = -20; self._trail = -25; self._trail_profit = 15
            return self._do_analyze(code, game)
    strats.append(WideStops())
    
    # 5. v2 + tighter stops (-10% hard, -15% trailing)
    class TightStops(V2):
        name = "S5_紧止损"
    strats.append(TightStops())
    
    # 6. MA-only (no quant filter)
    class MAOnly(V2):
        name = "S6_纯均线"
        def analyze(self, code, game):
            data = self.get_klines(code, game)
            if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
            closes = np.array([d['close'] for d in data])
            volumes = np.array([d['volume'] for d in data])
            price = closes[-1]
            ma5 = np.mean(closes[-5:]); ma5_p = np.mean(closes[-6:-1])
            ma20 = np.mean(closes[-20:]); ma20_p = np.mean(closes[-21:-1])
            vol_ratio = volumes[-1] / np.mean(volumes[-20:])
            rsi = self.rsi(data)
            held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
            if held:
                if ma5_p >= ma20_p and ma5 < ma20:
                    return {'action': 'SELL', 'lots': 0, 'reason': 'MA5死叉'}
                return {'action': 'HOLD', 'lots': 0, 'reason': '持有'}
            if ma5_p <= ma20_p and ma5 > ma20 and vol_ratio > 1.1:
                return {'action': 'BUY', 'lots': 999, 'reason': '金叉'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'RSI{rsi:.0f}'}
    strats.append(MAOnly())
    
    # 7. High quant only (>70)
    class HighQuant(V2):
        name = "S7_高量化"
        def analyze(self, code, game):
            data = self.get_klines(code, game)
            if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
            closes = np.array([d['close'] for d in data])
            price = closes[-1]; ma5 = np.mean(closes[-5:]); ma5_p = np.mean(closes[-6:-1])
            ma20 = np.mean(closes[-20:]); ma20_p = np.mean(closes[-21:-1])
            qs = game.calc_quant_score(code); quant = qs['score'] if qs else 50
            held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
            if held:
                if quant < 40: return {'action': 'SELL', 'lots': 0, 'reason': f'量化{quant:.0f}<40'}
                return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}
            if ma5_p <= ma20_p and ma5 > ma20 and quant > 70:
                return {'action': 'BUY', 'lots': 999, 'reason': f'量化{quant:.0f}金叉'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}
    strats.append(HighQuant())
    
    # 8. Oversold only
    class OversoldOnly(V2):
        name = "S8_纯超跌"
        def analyze(self, code, game):
            data = self.get_klines(code, game)
            if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
            closes = np.array([d['close'] for d in data])
            price = closes[-1]; ma20 = np.mean(closes[-20:])
            std20 = np.std(closes[-20:]); b_lower = ma20 - 2*std20
            rsi = self.rsi(data); vol_ratio = np.array([d['volume'] for d in data])[-1] / np.mean(np.array([d['volume'] for d in data])[-20:])
            held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
            if held:
                cost = game.portfolio[code]['cost_basis']
                if (price-cost)/cost > 5: return {'action': 'SELL', 'lots': 0, 'reason': '获利了结'}
                return {'action': 'HOLD', 'lots': 0, 'reason': '持有待涨'}
            if price <= b_lower * 1.02 and rsi < 32 and vol_ratio < 0.8:
                return {'action': 'BUY', 'lots': 999, 'reason': f'超跌RSI{rsi:.0f}'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'等超跌 RSI{rsi:.0f}'}
    strats.append(OversoldOnly())
    
    # 9. Breakout only
    class BreakOnly(V2):
        name = "S9_纯突破"
        def analyze(self, code, game):
            data = self.get_klines(code, game)
            if len(data) < 65: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
            closes = np.array([d['close'] for d in data]); highs = np.array([d['high'] for d in data])
            volumes = np.array([d['volume'] for d in data]); price = closes[-1]
            high60 = np.max(highs[-61:-1]); vol_ratio = volumes[-1] / np.mean(volumes[-20:])
            held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
            if held:
                cost = game.portfolio[code]['cost_basis']; pnl = (price-cost)/cost*100
                if pnl <= -10: return {'action': 'SELL', 'lots': 0, 'reason': '止损'}
                if pnl > 20: return {'action': 'SELL', 'lots': 0, 'reason': '止盈+20%'}
                return {'action': 'HOLD', 'lots': 0, 'reason': f'浮盈{pnl:+.1f}%'}
            if price > high60 * 1.005 and vol_ratio > 1.5:
                return {'action': 'BUY', 'lots': 999, 'reason': f'突破{high60:.1f}'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'等突破{high60:.1f}'}
    strats.append(BreakOnly())
    
    # 10. Buy-hold-sell by quant score only
    class QuantOnly(V2):
        name = "S10_纯量化"
        def analyze(self, code, game):
            data = self.get_klines(code, game)
            if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
            qs = game.calc_quant_score(code); quant = qs['score'] if qs else 50
            held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
            if held:
                if quant < 35: return {'action': 'SELL', 'lots': 0, 'reason': f'量化{quant:.0f}<35'}
                return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}
            if quant > 68:
                return {'action': 'BUY', 'lots': 999, 'reason': f'量化{quant:.0f}>68'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}
    strats.append(QuantOnly())
    
    return strats


def run_one(code, start, end, strat, init_cash):
    """Run single backtest, return final pnl_pct and benchmark_pct."""
    try:
        fetcher = StockDataFetcher()
        klines = fetcher.get_kline(code, start_date=start)
        klines = [k for k in klines if start <= k['date'] <= end]
        if len(klines) < 60: return None, None, []
        
        g = GameEngine()
        g.stocks[code] = {'name':code, 'klines':klines, 'date_map':{k['date']:k for k in klines}}
        g.start_date=start; g.dates=[k['date'] for k in klines]; g.total_days=len(g.dates); g.game_started=True
        
        init_price = klines[0]['close']
        cash = init_cash; shares = 0; trades = []
        
        strat.__init__()  # Clean slate
        
        for day in range(g.total_days):
            g.current_day = day; price = klines[day]['close']
            result = strat.analyze(code, g)
            action = result['action']
            
            if action == 'BUY' and shares == 0:
                max_lots = int(cash / (price * 100))
                if max_lots == 0 and cash >= price * 100: max_lots = 1
                if max_lots > 0:
                    cost = max_lots * price * 100
                    cash -= (cost + max(5.0, cost*0.00025))
                    shares = max_lots * 100
                    g.portfolio[code] = {'shares':shares, 'cost_basis':price}
                    trades.append(('B', klines[day]['date'], price))
            
            elif action == 'SELL' and shares > 0:
                proceeds = shares * price
                cash += (proceeds - max(5.0, proceeds*0.00025) - proceeds*0.001)
                g.portfolio.pop(code, None)
                trades.append(('S', klines[day]['date'], price))
                shares = 0
            
            if day == g.total_days-1 and shares > 0:
                proceeds = shares * price
                cash += (proceeds - max(5.0, proceeds*0.00025) - proceeds*0.001)
        
        final = cash + shares * klines[-1]['close']
        pnl = (final - init_cash) / init_cash * 100
        bench = (klines[-1]['close'] - init_price) / init_price * 100
        return pnl, bench, trades
    except Exception as e:
        print(f"  Error: {e}")
        return None, None, []


def main():
    START = '2019-01-01'
    END = '2026-06-01'
    CASH = 500000
    
    strats = make_strategies()
    print(f"Strategy count: {len(strats)}")
    print(f"Stock count: {len(STOCKS)}")
    print(f"Period: {START} -> {END}")
    print(f"Total backtests: {len(strats) * len(STOCKS)}")
    print("=" * 80)
    
    # Results matrix: strat x stock -> (pnl, bench, trades)
    results = {}
    for si, strat in enumerate(strats):
        print(f"\n[{si+1}/{len(strats)}] {strat.name}")
        results[strat.name] = {}
        for code, name, cat in STOCKS:
            pnl, bench, trades = run_one(code, START, END, strat, CASH)
            results[strat.name][code] = (pnl, bench, len(trades))
            if pnl is not None:
                excess = pnl - bench
                print(f"  {name}({cat:5s}): {pnl:+7.1f}% vs {bench:+7.1f}% ({excess:+7.1f}%) {len(trades)}t")
            else:
                print(f"  {name}({cat:5s}): FAILED")
    
    # ==== RANKING ====
    print("\n\n" + "=" * 80)
    print("STRATEGY RANKING (avg return across 15 stocks)")
    print("=" * 80)
    
    rankings = []
    for sname in results:
        pnls = [v[0] for v in results[sname].values() if v[0] is not None]
        benches = [v[1] for v in results[sname].values() if v[1] is not None]
        excesses = [v[0]-v[1] for v in results[sname].values() if v[0] is not None]
        trades_total = sum(v[2] for v in results[sname].values() if v[2] is not None)
        
        if pnls:
            avg_pnl = np.mean(pnls)
            avg_bench = np.mean(benches)
            avg_excess = np.mean(excesses)
            win_count = sum(1 for e in excesses if e > 0)
            rankings.append((sname, avg_pnl, avg_bench, avg_excess, win_count, len(pnls), trades_total))
    
    rankings.sort(key=lambda x: x[0])  # sort by name
    rankings.sort(key=lambda x: x[3], reverse=True)  # sort by excess
    
    print(f"\n{'Rank':<5} {'Strategy':<25} {'Avg PnL':>9} {'Avg Bench':>9} {'Avg Excess':>10} {'Wins':>5}  Trades")
    print("-" * 95)
    for i, (name, pnl, bench, excess, wins, cnt, trades) in enumerate(rankings):
        print(f"{i+1:<5} {name:<25} {pnl:+8.1f}% {bench:+8.1f}% {excess:+9.1f}% {wins:>3}/{cnt}  {trades:>4}")
    
    # Per-category breakdown
    print(f"\n{'Strategy':<25} {'Flat(5)':>10} {'Up(5)':>10} {'Down(5)':>10}")
    print("-" * 60)
    for sname, _, _, _, _, _, _ in rankings[:5]:
        flat_pnls = [results[sname][c][0] for c,n,cat in STOCKS if cat=='flat' and results[sname][c][0] is not None]
        up_pnls = [results[sname][c][0] for c,n,cat in STOCKS if cat=='up' and results[sname][c][0] is not None]
        down_pnls = [results[sname][c][0] for c,n,cat in STOCKS if cat=='down' and results[sname][c][0] is not None]
        print(f"{sname:<25} {np.mean(flat_pnls):+9.1f}% {np.mean(up_pnls):+9.1f}% {np.mean(down_pnls):+9.1f}%")


if __name__ == '__main__':
    main()
