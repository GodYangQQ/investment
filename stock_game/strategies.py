"""
Trading Strategies - All Variants
"""
import numpy as np


class StrategyBase:
    name = "base"
    
    @staticmethod
    def get_klines(code, game):
        dm = game.stocks.get(code, {}).get('date_map', {})
        current = game.get_current_date()
        if not current or not dm: return []
        dates = sorted([d for d in dm if d <= current])
        return [dm[d] for d in dates]
    
    @staticmethod
    def rsi(data, period=14):
        closes = np.array([d['close'] for d in data])
        if len(closes) < period + 1: return 50
        deltas = np.diff(closes[-(period+1):])
        gains = np.maximum(deltas, 0); losses = np.abs(np.minimum(deltas, 0))
        avg_gain = np.mean(gains); avg_loss = np.mean(losses)
        if avg_loss == 0: return 100
        return 100 - 100 / (1 + avg_gain / avg_loss)


# ====== S10: Pure Quant (BENCHMARK) ======
class S10PureQuant(StrategyBase):
    name = "S10纯量化"
    def __init__(self): pass
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
        qs = game.calc_quant_score(code); quant = qs['score'] if qs else 50
        price = data[-1]['close']
        held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
        if held:
            pnl = (price - game.portfolio[code]['cost_basis']) / game.portfolio[code]['cost_basis'] * 100
            if quant < 35: return {'action': 'SELL', 'lots': 0, 'reason': f'量化{quant:.0f}<35'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f} 浮盈{pnl:+.1f}%'}
        if quant > 68: return {'action': 'BUY', 'lots': 999, 'reason': f'量化{quant:.0f}>68'}
        return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}


# ====== S14: MA250 crossover (earliest entry) ======
class S14MACross(StrategyBase):
    name = "S14均线交叉"
    def __init__(self): pass
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 260: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
        closes = np.array([d['close'] for d in data]); price = closes[-1]
        ma50 = np.mean(closes[-50:]); ma50_p = np.mean(closes[-51:-1])
        ma250 = np.mean(closes[-250:]); ma250_p = np.mean(closes[-251:-1])
        held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
        if held:
            pnl = (price - game.portfolio[code]['cost_basis']) / game.portfolio[code]['cost_basis'] * 100
            if price < ma250 * 0.85: return {'action': 'SELL', 'lots': 0, 'reason': f'破MA250'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'价/MA250={price/ma250:.2f} 浮盈{pnl:+.1f}%'}
        if ma50_p <= ma250_p and ma50 > ma250: return {'action': 'BUY', 'lots': 999, 'reason': 'MA50金叉MA250'}
        return {'action': 'HOLD', 'lots': 0, 'reason': f'MA50{"↑" if ma50>ma250 else "↓"}MA250'}


# ====== S15: MA250 touch + RSI < 40 (pullback buy) ======
class S15Pullback(StrategyBase):
    name = "S15回踩MA250"
    def __init__(self): pass
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 260: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
        closes = np.array([d['close'] for d in data]); price = closes[-1]
        ma250 = np.mean(closes[-250:]); ratio = price / ma250
        rsi_now = self.rsi(data)
        held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
        if held:
            pnl = (price - game.portfolio[code]['cost_basis']) / game.portfolio[code]['cost_basis'] * 100
            if ratio > 1.5: return {'action': 'SELL', 'lots': 0, 'reason': f'过高{ratio:.2f}>1.5'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'价/MA={ratio:.2f} 浮盈{pnl:+.1f}%'}
        if 0.9 <= ratio <= 1.15 and rsi_now < 50: return {'action': 'BUY', 'lots': 999, 'reason': f'回踩MA250 RSI{rsi_now:.0f}'}
        return {'action': 'HOLD', 'lots': 0, 'reason': f'价/MA={ratio:.2f} RSI{rsi_now:.0f}'}


# ====== S16: Earliest of quant>60 OR MA crossover ======
class S16Earliest(StrategyBase):
    name = "S16最早入场"
    def __init__(self): pass
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
        closes = np.array([d['close'] for d in data]); price = closes[-1]
        ma50 = np.mean(closes[-50:]); ma50_p = np.mean(closes[-51:-1])
        ma250 = np.mean(closes[-250:]) if len(closes)>=250 else ma50
        qs = game.calc_quant_score(code); quant = qs['score'] if qs else 50
        held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
        if held:
            pnl = (price - game.portfolio[code]['cost_basis']) / game.portfolio[code]['cost_basis'] * 100
            if quant < 30: return {'action': 'SELL', 'lots': 0, 'reason': f'量化{quant:.0f}<30'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f} 浮盈{pnl:+.1f}%'}
        # Earliest of quant or MA crossover
        if quant > 60 or (ma50_p <= ma250 and ma50 > ma250):
            return {'action': 'BUY', 'lots': 999, 'reason': f'量化{quant:.0f} MA50{"↑" if ma50>ma250 else "↓"}MA250'}
        return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}


# ====== S17: Quant>55 + wide trail stop ======
class S17WideTrail(StrategyBase):
    name = "S17低门槛宽止损"
    def __init__(self): self.highest = {}
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
        closes = np.array([d['close'] for d in data]); price = closes[-1]
        qs = game.calc_quant_score(code); quant = qs['score'] if qs else 50
        held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
        if held:
            pnl = (price - game.portfolio[code]['cost_basis']) / game.portfolio[code]['cost_basis'] * 100
            if code not in self.highest: self.highest[code] = price
            self.highest[code] = max(self.highest[code], price)
            dd = (price - self.highest[code]) / self.highest[code] * 100
            if quant < 25: self.highest.pop(code,None); return {'action': 'SELL', 'lots': 0, 'reason': f'量化{quant:.0f}<25'}
            if dd <= -25: self.highest.pop(code,None); return {'action': 'SELL', 'lots': 0, 'reason': f'回撤{dd:.1f}%'}
            return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f} 浮盈{pnl:+.1f}% 回撤{abs(dd):.1f}%'}
        if quant > 55: self.highest[code] = price; return {'action': 'BUY', 'lots': 999, 'reason': f'量化{quant:.0f}>55'}
        return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}


# ====== S18: Quant>65 + hold forever (no sell) ======
class S18HoldForever(StrategyBase):
    name = "S18永持"
    def __init__(self): pass
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
        qs = game.calc_quant_score(code); quant = qs['score'] if qs else 50
        held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
        if held: return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f} 永久持有'}
        if quant > 65: return {'action': 'BUY', 'lots': 999, 'reason': f'量化{quant:.0f}>65'}
        return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}


# ====== S19: Quant>50, hold forever, even earlier ======
class S19UltraEarly(StrategyBase):
    name = "S19超早入场"
    def __init__(self): pass
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
        qs = game.calc_quant_score(code); quant = qs['score'] if qs else 50
        held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
        if held: return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f} 永持'}
        if quant > 50: return {'action': 'BUY', 'lots': 999, 'reason': f'量化{quant:.0f}>50'}
        return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}


STRATEGIES = {
    'unified': S10PureQuant(),
    's10': S10PureQuant(),
    's14': S14MACross(),
    's15': S15Pullback(),
    's16': S16Earliest(),
    's17': S17WideTrail(),
    's18': S18HoldForever(),
    's19': S19UltraEarly(),
}
