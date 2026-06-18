"""
综合策略 v1 (原始版)
止损: -8%硬止损, -10%回撤止盈, 无冷却期
买入: 量化>55, 量比>1.1, RSI<80
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
    def ma(data, n):
        closes = np.array([d['close'] for d in data])
        return np.mean(closes[-n:]) if len(closes) >= n else closes[-1]
    
    @staticmethod
    def rsi(data, period=14):
        closes = np.array([d['close'] for d in data])
        if len(closes) < period + 1: return 50
        deltas = np.diff(closes[-(period+1):])
        gains = np.maximum(deltas, 0)
        losses = np.abs(np.minimum(deltas, 0))
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0: return 100
        return 100 - 100 / (1 + avg_gain / avg_loss)
    
    @staticmethod
    def atr(data, period=14):
        highs = np.array([d['high'] for d in data])
        lows = np.array([d['low'] for d in data])
        closes = np.array([d['close'] for d in data])
        if len(data) < period + 1: return highs[-1] - lows[-1]
        tr = []
        for i in range(1, len(highs)):
            tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
        return np.mean(tr[-period:])
    
    @staticmethod
    def atr_lots(game, code, risk_pct=0.02):
        data = StrategyBase.get_klines(code, game)
        if len(data) < 15: return 1
        price = data[-1]['close']
        atr_val = StrategyBase.atr(data)
        if atr_val <= 0: return 1
        risk_amount = game.get_portfolio_value()['total'] * risk_pct
        lots = max(1, int(risk_amount / (atr_val * 100) / 100))
        return lots


class UnifiedStrategy(StrategyBase):
    """
    v1: 原始版 — 紧密止损 + 无冷却 + 低门槛
    - 硬止损: -8%
    - 回撤止盈: -10%
    - 量化退出: <45
    - 无冷却期
    """
    name = "综合策略v1"
    
    def __init__(self):
        self.entry_prices = {}
        self.highest = {}
    
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足'}
        
        closes = np.array([d['close'] for d in data])
        highs = np.array([d['high'] for d in data])
        volumes = np.array([d['volume'] for d in data])
        price = closes[-1]
        
        ma5 = np.mean(closes[-5:])
        ma5_prev = np.mean(closes[-6:-1])
        ma20 = np.mean(closes[-20:])
        ma20_prev = np.mean(closes[-21:-1])
        std20 = np.std(closes[-20:])
        b_lower = ma20 - 2 * std20
        high60 = np.max(highs[-61:-1]) if len(highs) >= 61 else highs[-1]
        vol_ratio = volumes[-1] / np.mean(volumes[-20:])
        ma5_vol = np.mean(volumes[-5:])
        rsi_now = self.rsi(data)
        
        qs = game.calc_quant_score(code)
        quant = qs['score'] if qs else 50
        
        held = code in game.portfolio and game.portfolio[code].get('shares', 0) > 0
        
        if held:
            cost = game.portfolio[code]['cost_basis']
            pnl_pct = (price - cost) / cost * 100
            
            if code not in self.highest: self.highest[code] = price
            self.highest[code] = max(self.highest[code], price)
            
            if quant < 45:
                self._cleanup(code)
                return {'action': 'SELL', 'lots': 0, 'reason': f'量化<45'}
            
            if pnl_pct <= -8:
                self._cleanup(code)
                return {'action': 'SELL', 'lots': 0, 'reason': f'硬止损{pnl_pct:.1f}%'}
            
            dd = (price - self.highest[code]) / self.highest[code] * 100
            if pnl_pct > 5 and dd <= -10:
                peak = self.highest[code]
                self._cleanup(code)
                return {'action': 'SELL', 'lots': 0, 'reason': f'回撤止盈{peak:.1f}->{price:.1f}'}
            
            if ma5_prev >= ma20_prev and ma5 < ma20 and rsi_now > 35 and pnl_pct < 20:
                self._cleanup(code)
                return {'action': 'SELL', 'lots': 0, 'reason': f'MA5死叉'}
            
            return {'action': 'HOLD', 'lots': 0, 'reason': f'浮盈{pnl_pct:+.1f}%'}
        
        # Entry
        if ma5_prev <= ma20_prev and ma5 > ma20 and vol_ratio > 1.1 and quant > 55 and rsi_now < 80:
            self.entry_prices[code] = price; self.highest[code] = price
            return {'action': 'BUY', 'lots': 999, 'reason': f'A:金叉 量化{quant:.0f}'}
        
        if price > high60 * 1.005 and vol_ratio > 1.5 and volumes[-1] > ma5_vol and quant > 60:
            self.entry_prices[code] = price; self.highest[code] = price
            return {'action': 'BUY', 'lots': 999, 'reason': f'B:突破'}
        
        if price <= b_lower * 1.02 and rsi_now < 32 and vol_ratio < 0.8 and quant > 40:
            self.entry_prices[code] = price; self.highest[code] = price
            return {'action': 'BUY', 'lots': 999, 'reason': f'C:超跌'}
        
        return {'action': 'HOLD', 'lots': 0, 'reason': f'量化{quant:.0f}'}
    
    def _cleanup(self, code):
        self.entry_prices.pop(code, None)
        self.highest.pop(code, None)
