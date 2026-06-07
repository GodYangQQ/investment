"""
综合策略 v2: 更宽的止损+冷却期+更高的入场门槛
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
    综合策略 v2: 更宽的止损+冷却期+更高的入场门槛
    - 硬止损: -15%
    - 回撤止盈: 盈利>10%后回落-20%
    - 冷却期: 卖出后5天内不买回
    - 入场门槛提高: 量化>60, 量比>1.2
    """
    name = "综合策略"
    
    def __init__(self):
        self.entry_prices = {}
        self.highest = {}
        self.last_sell_day = {}
    
    def analyze(self, code, game):
        data = self.get_klines(code, game)
        if len(data) < 60: return {'action': 'HOLD', 'lots': 0, 'reason': '数据不足(<60天)'}
        
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
        day_idx = len(data) - 1
        
        # ========== EXIT ==========
        if held:
            cost = game.portfolio[code]['cost_basis']
            pnl_pct = (price - cost) / cost * 100
            
            if code not in self.highest: self.highest[code] = price
            self.highest[code] = max(self.highest[code], price)
            
            # Quant exit (very low threshold)
            if quant < 30:
                self._cleanup(code)
                return {'action': 'SELL', 'lots': 0, 'reason': f'量化{quant:.0f}<30清仓'}
            
            # Hard stop -15%
            if pnl_pct <= -15:
                self._cleanup(code)
                self.last_sell_day[code] = day_idx
                return {'action': 'SELL', 'lots': 0, 'reason': f'硬止损{pnl_pct:.1f}%'}
            
            # Trailing stop -20% from peak
            dd = (price - self.highest[code]) / self.highest[code] * 100
            if pnl_pct > 10 and dd <= -20:
                peak = self.highest[code]
                self._cleanup(code)
                self.last_sell_day[code] = day_idx
                return {'action': 'SELL', 'lots': 0, 'reason': f'回撤止盈{peak:.1f}->{price:.1f}'}
            
            # MA dead cross only to protect break-even
            if ma5_prev >= ma20_prev and ma5 < ma20 and rsi_now > 40 and abs(pnl_pct) < 5:
                self._cleanup(code)
                self.last_sell_day[code] = day_idx
                return {'action': 'SELL', 'lots': 0, 'reason': f'MA5死叉保本 RSI{rsi_now:.0f}'}
            
            return {'action': 'HOLD', 'lots': 0,
                    'reason': f'量化{quant:.0f} 浮盈{pnl_pct:+.1f}% 峰值回落{abs(dd):.1f}%'}
        
        # ========== ENTRY ==========
        cooldown = day_idx - self.last_sell_day.get(code, -999)
        if cooldown < 5:
            return {'action': 'HOLD', 'lots': 0, 'reason': f'冷却中({cooldown}/5天)'}
        
        # A: Golden cross
        if ma5_prev <= ma20_prev and ma5 > ma20 and vol_ratio > 1.2 and quant > 60 and rsi_now < 75:
            self.entry_prices[code] = price
            self.highest[code] = price
            return {'action': 'BUY', 'lots': 999, 'reason': f'A:金叉 量化{quant:.0f} 量比{vol_ratio:.1f}'}
        
        # B: Breakout
        if price > high60 * 1.01 and vol_ratio > 1.5 and volumes[-1] > ma5_vol and quant > 65:
            self.entry_prices[code] = price
            self.highest[code] = price
            return {'action': 'BUY', 'lots': 999, 'reason': f'B:突破{high60:.1f} 量化{quant:.0f}'}
        
        # C: Oversold
        if price <= b_lower * 1.01 and rsi_now < 28 and vol_ratio < 0.6 and quant > 45:
            self.entry_prices[code] = price
            self.highest[code] = price
            return {'action': 'BUY', 'lots': 999, 'reason': f'C:超跌 RSI{rsi_now:.0f} 量化{quant:.0f}'}
        
        return {'action': 'HOLD', 'lots': 0,
                'reason': f'量化{quant:.0f} RSI{rsi_now:.0f} 量{vol_ratio:.2f}'}
    
    def _cleanup(self, code):
        self.entry_prices.pop(code, None)
        self.highest.pop(code, None)


STRATEGIES = {'unified': UnifiedStrategy()}
