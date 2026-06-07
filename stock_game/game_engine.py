"""
Stock Trading Game - Backend v3
- Add stocks anytime during game
- Fixed initial cash
- Paginated Tencent API for full history
"""
import json
import urllib.request
from datetime import datetime
import numpy as np
from strategies import STRATEGIES

class StockDataFetcher:
    @staticmethod
    def get_kline(code, start_date=None, end_date=None):
        """Get K-line data, paginating Tencent API for full history."""
        market = f'sh{code}' if code.startswith(('6','5')) else f'sz{code}'
        
        def fetch_range(s, e, n=600):
            url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market},day,{s},{e},{n},qfq'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.sina.com.cn'
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            d = data.get('data', {}).get(market, {})
            k = d.get('qfqday', []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
            return k
        
        try:
            start = start_date or '2015-01-01'
            end = end_date or datetime.now().strftime('%Y-%m-%d')
            all_klines, seen = [], set()
            
            curr_y = int(start[:4])
            end_y = int(end[:4])
            while curr_y <= end_y:
                cs = f'{curr_y}-01-01'
                ce = f'{min(curr_y+1, end_y)}-12-31' if curr_y < end_y else end
                for k in fetch_range(cs, ce):
                    if len(k) >= 6 and k[0] not in seen:
                        seen.add(k[0])
                        all_klines.append({
                            'date': k[0], 'open': float(k[1]), 'close': float(k[2]),
                            'high': float(k[3]), 'low': float(k[4]), 'volume': float(k[5])
                        })
                curr_y += 2
            
            all_klines.sort(key=lambda x: x['date'])
            return all_klines
        except Exception as e:
            print(f"Fetch error for {code}: {e}")
            return []
    
    @staticmethod
    def get_quote(code):
        market = f'sh{code}' if code.startswith(('6','5')) else f'sz{code}'
        try:
            url = f'https://qt.gtimg.cn/q={market}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode('gbk', errors='replace')
            import re
            m = re.search(r'="(.+?)"', text)
            if m:
                fields = m.group(1).split('~')
                if len(fields) > 1:
                    return fields[1]
        except: pass
        return code


class GameEngine:
    def __init__(self):
        self.stocks = {}
        self.portfolio = {}
        self.cash = 100000.0
        self.current_day = 0
        self.total_days = 0
        self.dates = []
        self.transactions = []
        self.game_started = False
        self.game_over = False
        self.initial_cash = 100000.0
        self.start_date = None
        self.auto_pilot = False
        self.auto_strategy = 'unified'
        self._breakout = None
        self.strategy_log = []  # Strategy thinking log
    
    def add_stock(self, code):
        try:
            fetcher = StockDataFetcher()
            name = fetcher.get_quote(code)
            
            klines = fetcher.get_kline(code, start_date=self.start_date)
            if not klines or len(klines) < 30:
                return False, f"获取{code}数据失败(仅{len(klines) if klines else 0}条)，可能已退市或代码错误"
            
            self.stocks[code] = {'name': name, 'klines': klines}
            
            # Build date_map for all dates (including pre-game)
            self.stocks[code]['date_map'] = {k['date']: k for k in klines}
            
            # If game is running, add the new stock's dates to the game dates
            if self.game_started and self.dates:
                new_dates = {k['date'] for k in klines if k['date'] >= self.start_date}
                if new_dates:
                    all_dates = set(self.dates) | new_dates
                    self.dates = sorted(all_dates)
                    
                    # Recalculate total_days from the new start offset
                    game_start_idx = 0
                    for i, d in enumerate(self.dates):
                        if d >= self.start_date:
                            game_start_idx = i
                            break
                    self._game_start_idx = game_start_idx
                    self.total_days = len(self.dates) - game_start_idx
            
            return True, f"{name}({code}) {len(klines)}条数据"
        except Exception as e:
            return False, f"添加{code}出错: {str(e)}"
    
    def start_game(self, start_date, init_cash=100000, chart_lookback=100):
        if len(self.stocks) == 0:
            return False, "请先添加股票"
        
        self.start_date = start_date
        self.initial_cash = init_cash
        self.chart_lookback = chart_lookback
        
        # Fetch data starting lookback+100 days BEFORE the game start date for chart context
        from datetime import datetime, timedelta
        try:
            sd_dt = datetime.strptime(start_date, '%Y-%m-%d')
            pre_start = (sd_dt - timedelta(days=chart_lookback + 120)).strftime('%Y-%m-%d')
        except:
            pre_start = start_date
        
        fetcher = StockDataFetcher()
        for code in self.stocks:
            k = fetcher.get_kline(code, start_date=pre_start)
            if k:
                self.stocks[code]['klines'] = k
        
        # Build dates: include pre-start data for chart, but only game-able from start_date
        all_dates = set()
        for code, s in self.stocks.items():
            for k in s['klines']:
                all_dates.add(k['date'])
        
        self.dates = sorted(all_dates)
        
        # Game starts at the first date >= start_date
        game_start_idx = 0
        for i, d in enumerate(self.dates):
            if d >= start_date:
                game_start_idx = i
                break
        
        game_dates = self.dates[game_start_idx:]
        if len(game_dates) < 20:
            return False, f"交易日不足({len(game_dates)}天)，换早起始日期"
        
        self.total_days = len(game_dates)
        self.current_day = 0
        self._game_start_idx = game_start_idx  # offset into self.dates for pre-game data
        self.portfolio = {}
        self.cash = self.initial_cash
        self.transactions = []
        self.game_started = True
        self.game_over = False
        
        # Record initial prices for benchmark
        self._initial_prices = {}
        for code, s in self.stocks.items():
            date = game_dates[0]
            dm = s.get('date_map', {})
            if date in dm:
                self._initial_prices[code] = dm[date]['close']
        
        for code in self.stocks:
            self.stocks[code]['date_map'] = {
                k['date']: k for k in self.stocks[code]['klines']
            }
        
        return True, f"{self.total_days}个交易日 ({game_dates[0]} ~ {game_dates[-1]})"
    
    def _game_start_offset(self):
        return getattr(self, '_game_start_idx', 0)
    
    def _current_date_idx(self):
        return self._game_start_offset() + self.current_day
    
    def get_current_date(self):
        if not self.game_started or self.current_day >= self.total_days:
            return None
        idx = self._current_date_idx()
        return self.dates[idx] if idx < len(self.dates) else None
    
    def get_current_prices(self):
        if not self.game_started: return {}
        date = self.get_current_date()
        prev_idx = max(0, self._current_date_idx() - 1)
        prev_date = self.dates[prev_idx] if prev_idx < len(self.dates) else date
        
        prices = {}
        for code, s in self.stocks.items():
            dm = s.get('date_map', {})
            if date in dm:
                prev_close = dm[prev_date]['close'] if prev_date in dm else dm[date]['open']
                prices[code] = {
                    'open': dm[date]['open'], 'close': dm[date]['close'],
                    'high': dm[date]['high'], 'low': dm[date]['low'],
                    'volume': dm[date]['volume'], 'name': s['name'],
                    'prev_close': prev_close
                }
        return prices
    
    def buy_stock(self, code, lots):
        """Buy by lots (1 lot = 100 shares). A-share fees included."""
        if not self.game_started or self.game_over:
            return False, "游戏未进行"
        lots = int(lots)
        if lots <= 0:
            return False, "手数必须>0"
        prices = self.get_current_prices()
        if code not in prices:
            return False, "无行情"
        price = prices[code]['close']
        shares = lots * 100
        cost = shares * price
        # A-share buy commission: 0.025%, min ¥5
        commission = max(5.0, cost * 0.00025)
        total_cost = cost + commission
        if total_cost > self.cash:
            return False, f"资金不足 需¥{total_cost:,.0f}(含佣金¥{commission:.0f}) 可用¥{self.cash:,.0f}"
        self.cash -= total_cost
        if code not in self.portfolio:
            self.portfolio[code] = {'shares': 0, 'cost_basis': 0}
        old = self.portfolio[code]['shares'] * self.portfolio[code]['cost_basis']
        self.portfolio[code]['shares'] += shares
        self.portfolio[code]['cost_basis'] = (old + total_cost) / self.portfolio[code]['shares']
        self.transactions.append({
            'day': self.current_day, 'date': self.get_current_date(),
            'type': 'BUY', 'code': code, 'name': self.stocks[code]['name'],
            'shares': shares, 'price': price, 'amount': total_cost, 'fee': commission
        })
        return True, f"买 {self.stocks[code]['name']} {lots}手({shares}股) ¥{total_cost:,.0f}(佣金¥{commission:.0f})"
    
    def sell_stock(self, code, lots):
        """Sell by lots. 0 = sell all. A-share fees included."""
        if not self.game_started or self.game_over:
            return False, "游戏未进行"
        if code not in self.portfolio or self.portfolio[code]['shares'] <= 0:
            return False, "无持仓"
        prices = self.get_current_prices()
        if code not in prices:
            return False, "无行情"
        price = prices[code]['close']
        lots = int(lots)
        if lots == 0 or lots * 100 >= self.portfolio[code]['shares']:
            shares = self.portfolio[code]['shares']
            lots = shares // 100
        else:
            shares = lots * 100
        proceeds = shares * price
        # A-share sell: commission 0.025% min ¥5 + stamp tax 0.1%
        commission = max(5.0, proceeds * 0.00025)
        stamp = proceeds * 0.001
        net = proceeds - commission - stamp
        self.cash += net
        self.portfolio[code]['shares'] -= shares
        if self.portfolio[code]['shares'] == 0:
            del self.portfolio[code]
        self.transactions.append({
            'day': self.current_day, 'date': self.get_current_date(),
            'type': 'SELL', 'code': code, 'name': self.stocks[code]['name'],
            'shares': shares, 'price': price, 'amount': net, 'fee': commission + stamp
        })
        return True, f"卖 {self.stocks[code]['name']} {lots}手({shares}股) 到账¥{net:,.0f}(佣金¥{commission:.0f}+印花税¥{stamp:.0f})"
        proceeds = shares * price
        self.cash += proceeds
        self.portfolio[code]['shares'] -= shares
        if self.portfolio[code]['shares'] == 0:
            del self.portfolio[code]
        self.transactions.append({
            'day': self.current_day, 'date': self.get_current_date(),
            'type': 'SELL', 'code': code, 'name': self.stocks[code]['name'],
            'shares': shares, 'price': price, 'amount': proceeds
        })
        return True, f"卖 {self.stocks[code]['name']} {shares}股"
    
    def advance_day(self, days=1):
        if not self.game_started: return False, "未开始"
        if self.game_over: return False, "已结束"
        
        # Execute auto-pilot for EACH day when advancing multiple days
        for _ in range(days):
            if self.current_day >= self.total_days - 1:
                break
            self.current_day += 1
            if self.auto_pilot:
                self._run_auto_pilot()
        
        if self.current_day >= self.total_days - 1:
            self.game_over = True
        return True, ""
    
    def _run_auto_pilot(self):
        """Execute auto-pilot: buy on signal, sell on signal. Per-stock independent."""
        if not self.auto_pilot: return
        
        strat = STRATEGIES.get(self.auto_strategy)
        if not strat: return
        
        date = self.get_current_date()
        logs = []
        
        # Check each stock independently
        for code in list(self.stocks.keys()):
            result = strat.analyze(code, self)
            action = result['action']
            reason = result.get('reason', '')
            
            if action == 'SELL':
                if code in self.portfolio and self.portfolio[code].get('shares', 0) > 0:
                    lots = 0  # 0 = sell all
                    self.sell_stock(code, lots)
                    logs.append(f"[卖] {self.stocks[code]['name']} 全卖 {reason}")
            
            elif action == 'BUY':
                if code not in self.portfolio or self.portfolio[code].get('shares', 0) == 0:
                    prices = self.get_current_prices()
                    price = prices[code]['close'] if code in prices else 0
                    if price > 0:
                        max_lots = int(self.cash / (price * 100))
                        if max_lots > 0:
                            self.buy_stock(code, max_lots)
                            logs.append(f"[买] {self.stocks[code]['name']} {max_lots}手(全仓) {reason}")
                        else:
                            logs.append(f"[等] {self.stocks[code]['name']} 资金不足买1手 {reason}")
                    else:
                        logs.append(f"[等] {self.stocks[code]['name']} 无价格 {reason}")
                else:
                    pnl = 0
                    cost = self.portfolio[code]['cost_basis']
                    prices = self.get_current_prices()
                    if code in prices:
                        pnl = (prices[code]['close'] - cost) / cost * 100
                    logs.append(f"[持] {self.stocks[code]['name']} 浮盈{pnl:+.1f}% {reason}")
            
            else:
                if code in self.portfolio and self.portfolio[code].get('shares', 0) > 0:
                    pnl = 0
                    cost = self.portfolio[code]['cost_basis']
                    prices = self.get_current_prices()
                    if code in prices:
                        pnl = (prices[code]['close'] - cost) / cost * 100
                    logs.append(f"[持] {self.stocks[code]['name']} 浮盈{pnl:+.1f}% {reason}")
                else:
                    logs.append(f"[等] {self.stocks[code]['name']} {reason}")
        
        if logs:
            self.strategy_log.append({'date': date, 'logs': logs})
    
    def get_portfolio_value(self):
        prices = self.get_current_prices()
        sv = sum(self.portfolio[c]['shares'] * prices[c]['close']
                 for c in self.portfolio if c in prices)
        
        # Benchmark: equal-weight buy-and-hold from day 1
        initial = getattr(self, '_initial_prices', {})
        bench_ret = 0
        if initial:
            rets = []
            for code, init_p in initial.items():
                if code in prices:
                    rets.append((prices[code]['close'] - init_p) / init_p)
            if rets:
                bench_ret = sum(rets) / len(rets) * 100
        
        return {
            'cash': self.cash, 'stock_value': sv,
            'total': self.cash + sv, 'initial': self.initial_cash,
            'pnl': self.cash + sv - self.initial_cash,
            'pnl_pct': (self.cash + sv - self.initial_cash) / self.initial_cash * 100,
            'benchmark_pct': bench_ret
        }
    
    def get_historical_prices(self, code, lookback=200):
        if code not in self.stocks: return []
        dm = self.stocks[code].get('date_map', {})
        result = []
        current_date_idx = self._current_date_idx()
        start_idx = max(0, current_date_idx - lookback + 1)
        for i in range(start_idx, current_date_idx + 1):
            date = self.dates[i]
            if date in dm:
                result.append({
                    'date': date, 'open': dm[date]['open'], 'close': dm[date]['close'],
                    'high': dm[date]['high'], 'low': dm[date]['low'], 'volume': dm[date]['volume']
                })
        return result
    
    def get_full_state(self):
        return {
            'game_started': self.game_started, 'game_over': self.game_over,
            'current_day': self.current_day, 'total_days': self.total_days,
            'current_date': self.get_current_date(),
            'start_date': self.dates[self._game_start_offset()] if self.dates and self._game_start_offset() < len(self.dates) else None,
            'end_date': self.dates[-1] if self.dates else None,
            'cash': self.cash, 'initial_cash': self.initial_cash,
            'stocks': {c: {'name': s['name']} for c, s in self.stocks.items()},
            'portfolio': self.portfolio,
            'portfolio_value': self.get_portfolio_value(),
            'prices': self.get_current_prices(),
            'recent_transactions': self.transactions[-20:],
            '_chart_lookback': getattr(self, 'chart_lookback', 100),
            'trade_markers': [{'date': t['date'], 'type': t['type'], 'code': t['code'], 'price': t['price']}
                            for t in self.transactions],
            'quant_scores': {code: self.calc_quant_score(code) for code in self.stocks},
            'strategy_log': self.strategy_log[-8:],
            'auto_pilot': self.auto_pilot,
            'auto_strategy': self.auto_strategy,
        }
    
    def reset(self):
        self.__init__()

    def calc_quant_score(self, code):
        """Calculate simplified quant score from available K-line data up to current date."""
        if code not in self.stocks: return None
        
        dm = self.stocks[code].get('date_map', {})
        if not dm: return None
        
        # Only use dates up to the current game date
        current_date = self.get_current_date()
        if not current_date: return None
        
        all_dates = sorted([d for d in dm.keys() if d <= current_date])
        if len(all_dates) < 60: return None
        closes = np.array([dm[d]['close'] for d in all_dates])
        highs = np.array([dm[d]['high'] for d in all_dates])
        lows = np.array([dm[d]['low'] for d in all_dates])
        volumes = np.array([dm[d]['volume'] for d in all_dates])
        
        # MA calculations
        def ma(arr, n):
            return np.mean(arr[-n:]) if len(arr) >= n else arr[-1]
        
        ma5, ma10, ma20, ma60 = ma(closes, 5), ma(closes, 10), ma(closes, 20), ma(closes, 60)
        current = closes[-1]
        
        # 1. Trend (25)
        trend = 12
        if ma5 > ma10 > ma20 > ma60:
            spread = (ma5 - ma20) / ma20 * 100
            trend = min(25, 15 + abs(spread) * 2)
        elif ma5 > ma10 > ma20:
            trend = min(18, 10 + (current - ma20) / ma20 * 50)
        elif current > ma20:
            trend = 10 + (current - ma20) / ma20 * 100
        else:
            trend = max(3, 8 - abs(ma20 - current) / ma20 * 100)
        trend = max(0, min(25, trend))
        
        # 2. Position (25) - distance from 60d high
        high60 = np.max(highs[-60:]) if len(highs) >= 60 else highs[-1]
        dd = (high60 - current) / high60 * 100
        if 5 <= dd <= 20:
            pos = 20
        elif dd < 3:
            pos = 5
        else:
            pos = max(3, 20 - (dd - 20) * 0.3)
        pos = max(0, min(25, pos))
        
        # 3. Volume (20)
        vol_ratio = np.mean(volumes[-5:]) / np.mean(volumes[-20:]) if len(volumes) >= 20 else 1
        vol = 8
        if 1.0 <= vol_ratio <= 2.0:
            vol += 8
        elif vol_ratio > 0.7:
            vol += 4
        vol = min(20, vol)
        
        # 4. RSI (15)
        deltas = np.diff(closes[-15:])
        gains = np.maximum(deltas, 0)
        losses = np.abs(np.minimum(deltas, 0))
        avg_gain = np.mean(gains) if len(gains) > 0 else 0
        avg_loss = np.mean(losses) if len(losses) > 0 else 1
        rsi = 100 - 100 / (1 + avg_gain / max(avg_loss, 0.0001))
        if 45 <= rsi <= 60:
            rsi_score = 15
        elif 40 <= rsi < 45 or 60 < rsi <= 70:
            rsi_score = 12
        elif 30 <= rsi < 40:
            rsi_score = 8
        else:
            rsi_score = max(3, 15 - abs(rsi - 52) * 0.5)
        
        # 5. Volatility (15)
        atr = np.mean(highs[-14:] - lows[-14:]) if len(highs) >= 14 else highs[-1] - lows[-1]
        atr_pct = atr / current * 100
        if 2 <= atr_pct <= 5:
            v_score = 14
        elif atr_pct < 1:
            v_score = 7
        else:
            v_score = max(3, 14 - (atr_pct - 5) * 0.5)
        
        total = trend + pos + vol + rsi_score + v_score
        total = max(0, min(100, total))
        
        level = 'strong' if total >= 78 else ('buy' if total >= 68 else ('watch' if total >= 55 else 'weak'))
        return {'score': round(total, 1), 'level': level, 'rsi': round(rsi, 1), 'trend': round(trend, 1),
                'pos': round(pos, 1), 'vol': round(vol, 1), 'rsi_s': round(rsi_score, 1), 'v_s': round(v_score, 1)}

    def remove_stock(self, code):
        """Remove a stock from the game."""
        if code not in self.stocks:
            return False, "股票不存在"
        if code in self.portfolio and self.portfolio[code]['shares'] > 0:
            return False, "请先卖出该股票持仓"
        del self.stocks[code]
        return True, "已删除"

game = GameEngine()
