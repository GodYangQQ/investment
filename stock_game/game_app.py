"""
Stock Trading Game - Desktop GUI (tkinter + matplotlib)
Same features as web version, packaged as EXE.
"""
import json
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from datetime import datetime, timedelta
import threading
import queue

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import numpy as np

# Import game engine
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from game_engine import GameEngine, StockDataFetcher

# ============================================================
# COLORS (A-share: red up, green down)
# ============================================================
BG = '#0a0e14'
CARD = '#0f141c'
TEXT = '#e4e6eb'
DIM = '#6b7480'
RED = '#f44336'
GREEN = '#4caf50'
BLUE = '#2196F3'
UP_COLOR = '#ef5350'
DOWN_COLOR = '#26a69a'
MA_COLORS = ['#e91e63', '#9c27b0', '#ff9800', '#00bcd4', '#4caf50']

# ============================================================
# MAIN APP
# ============================================================
class StockGameApp:
    def __init__(self, root):
        self.root = root
        self.root.title("股票交易模拟游戏")
        self.root.geometry("1400x900")
        self.root.configure(bg=BG)
        self.root.minsize(1000, 700)
        
        self.game = GameEngine()
        self.added_stocks = {}
        self.selected_chart_code = None
        self._after_id = None
        
        self._build_ui()
        self._setup_styles()
    
    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=TEXT, font=('Microsoft YaHei', 10))
        style.configure('TButton', font=('Microsoft YaHei', 10))
        style.configure('Green.TButton', foreground=GREEN)
        style.configure('Red.TButton', foreground=RED)
    
    # ============================================================
    # UI BUILD
    # ============================================================
    def _build_ui(self):
        # Main container
        self.main_frame = ttk.Frame(self.root)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        
        # TOP: Setup panel
        self._build_setup_panel()
        
        # BOTTOM: Game area (initially hidden)
        self._build_game_area()
    
    def _build_setup_panel(self):
        self.setup_frame = tk.Frame(self.main_frame, bg=CARD, bd=1, relief=tk.SOLID, highlightbackground='#1e2530')
        self.setup_frame.pack(fill=tk.X, pady=(0, 8))
        
        # Row 1: Stock code input
        row1 = tk.Frame(self.setup_frame, bg=CARD)
        row1.pack(fill=tk.X, padx=12, pady=(10, 4))
        
        tk.Label(row1, text="股票代码:", bg=CARD, fg=DIM, font=('Microsoft YaHei', 10)).pack(side=tk.LEFT)
        self.code_entry = tk.Entry(row1, width=10, bg='#1a2030', fg=TEXT, insertbackground=TEXT,
                                   font=('Microsoft YaHei', 10), bd=1, relief=tk.SOLID)
        self.code_entry.pack(side=tk.LEFT, padx=6)
        self.code_entry.bind('<Return>', lambda e: self.add_stock())
        
        tk.Button(row1, text="添加股票", command=self.add_stock, bg=BLUE, fg='white',
                  font=('Microsoft YaHei', 10), bd=0, padx=12, pady=3, cursor='hand2').pack(side=tk.LEFT, padx=4)
        
        tk.Label(row1, text="可多次添加，游戏中途也可添加", bg=CARD, fg=DIM, font=('Microsoft YaHei', 9)).pack(side=tk.LEFT, padx=10)
        
        # Row 2: Stock list
        row2 = tk.Frame(self.setup_frame, bg=CARD)
        row2.pack(fill=tk.X, padx=12, pady=2)
        self.stock_list_label = tk.Label(row2, text="等待添加...", bg=CARD, fg=DIM, font=('Microsoft YaHei', 9))
        self.stock_list_label.pack(side=tk.LEFT)
        
        # Row 3: Date + Cash
        row3 = tk.Frame(self.setup_frame, bg=CARD)
        row3.pack(fill=tk.X, padx=12, pady=4)
        
        tk.Label(row3, text="起始日期:", bg=CARD, fg=DIM, font=('Microsoft YaHei', 10)).pack(side=tk.LEFT)
        self.date_entry = tk.Entry(row3, width=12, bg='#1a2030', fg=TEXT, insertbackground=TEXT,
                                    font=('Microsoft YaHei', 10), bd=1, relief=tk.SOLID)
        self.date_entry.insert(0, "2018-01-01")
        self.date_entry.pack(side=tk.LEFT, padx=6)
        
        tk.Label(row3, text="初始资金:", bg=CARD, fg=DIM, font=('Microsoft YaHei', 10)).pack(side=tk.LEFT, padx=(16, 0))
        self.cash_entry = tk.Entry(row3, width=10, bg='#1a2030', fg=TEXT, insertbackground=TEXT,
                                    font=('Microsoft YaHei', 10), bd=1, relief=tk.SOLID)
        self.cash_entry.insert(0, "100000")
        self.cash_entry.pack(side=tk.LEFT, padx=6)
        
        tk.Label(row3, text="元", bg=CARD, fg=DIM, font=('Microsoft YaHei', 10)).pack(side=tk.LEFT)
        
        tk.Label(row3, text="   K线天数:", bg=CARD, fg=DIM, font=('Microsoft YaHei', 10)).pack(side=tk.LEFT, padx=(16,0))
        self.lookback_var = tk.StringVar(value="100")
        self.lookback_combo = ttk.Combobox(row3, textvariable=self.lookback_var, values=["50","100","150","200"],
                                            state='readonly', width=5, font=('Microsoft YaHei', 10))
        self.lookback_combo.pack(side=tk.LEFT, padx=4)
        
        # Row 4: Buttons
        row4 = tk.Frame(self.setup_frame, bg=CARD)
        row4.pack(fill=tk.X, padx=12, pady=(4, 10))
        
        self.start_btn = tk.Button(row4, text="开始游戏", command=self.start_game, bg=GREEN, fg='white',
                                    font=('Microsoft YaHei', 11, 'bold'), bd=0, padx=20, pady=4,
                                    state=tk.DISABLED, cursor='hand2')
        self.start_btn.pack(side=tk.LEFT)
        
        tk.Button(row4, text="重置", command=self.reset_game, bg='#555', fg='#ccc',
                  font=('Microsoft YaHei', 10), bd=0, padx=12, pady=3, cursor='hand2').pack(side=tk.LEFT, padx=8)
    
    def _build_game_area(self):
        self.game_frame = tk.Frame(self.main_frame, bg=BG)
        
        # ---- STATS BAR ----
        self.stats_frame = tk.Frame(self.game_frame, bg=BG)
        self.stats_frame.pack(fill=tk.X, pady=(0, 6))
        
        self.stat_labels = {}
        for i, (label, key) in enumerate([('总资产', 'total'), ('现金', 'cash'), ('持仓市值', 'stock'),
                                           ('总盈亏', 'pnl'), ('基准收益', 'bench'), ('日涨跌', 'daily')]):
            card = tk.Frame(self.stats_frame, bg=CARD, bd=1, relief=tk.SOLID, highlightbackground='#1e2530')
            card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0 if i==0 else 3, 0))
            tk.Label(card, text=label, bg=CARD, fg=DIM, font=('Microsoft YaHei', 9)).pack(pady=(8, 0))
            val = tk.Label(card, text="--", bg=CARD, fg=TEXT, font=('Microsoft YaHei', 14, 'bold'))
            val.pack()
            sub = tk.Label(card, text="", bg=CARD, fg=DIM, font=('Microsoft YaHei', 8))
            sub.pack(pady=(0, 6))
            self.stat_labels[key] = (val, sub)
        
        # ---- DATE BAR ----
        self.date_frame = tk.Frame(self.game_frame, bg=CARD, bd=1, relief=tk.SOLID, highlightbackground='#1e2530')
        self.date_frame.pack(fill=tk.X, pady=(0, 6))
        date_inner = tk.Frame(self.date_frame, bg=CARD)
        date_inner.pack(fill=tk.X, padx=10, pady=8)
        
        self.date_label = tk.Label(date_inner, text="----", bg=CARD, fg=TEXT, font=('Microsoft YaHei', 13, 'bold'))
        self.date_label.pack(side=tk.LEFT)
        
        self.day_info_label = tk.Label(date_inner, text="", bg=CARD, fg=DIM, font=('Microsoft YaHei', 10))
        self.day_info_label.pack(side=tk.LEFT, padx=12)
        
        # Progress bar
        self.progress_canvas = tk.Canvas(date_inner, width=200, height=6, bg='#1e2530', bd=0, highlightthickness=0)
        self.progress_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)
        
        for btn_text, days in [("▶ 1天", 1), ("5天", 5), ("20天", 20), ("100天", 100)]:
            tk.Button(date_inner, text=btn_text, command=lambda d=days: self.skip_days(d),
                      bg='#ff9800' if days==1 else BLUE, fg='white',
                      font=('Microsoft YaHei', 9), bd=0, padx=8, pady=3, cursor='hand2').pack(side=tk.LEFT, padx=2)
        
        # Auto-pilot controls
        tk.Label(date_inner, text=" 托管:", bg=CARD, fg=DIM, font=('Microsoft YaHei', 9)).pack(side=tk.LEFT, padx=(10,2))
        self.auto_var = tk.StringVar(value='off')
        self.auto_combo = ttk.Combobox(date_inner, textvariable=self.auto_var,
            values=['off', '综合策略'],
            state='readonly', width=14, font=('Microsoft YaHei', 9))
        self.auto_combo.pack(side=tk.LEFT, padx=2)
        self.auto_combo.bind('<<ComboboxSelected>>', self._on_auto_change)
        
        tk.Button(date_inner, text="结束", command=self.end_game, bg=RED, fg='white',
                  font=('Microsoft YaHei', 9), bd=0, padx=8, pady=3, cursor='hand2').pack(side=tk.LEFT, padx=4)
        
        # ---- ADD STOCK IN GAME ----
        self.add_game_frame = tk.Frame(self.game_frame, bg=CARD, bd=1, relief=tk.SOLID, highlightbackground='#1e2530')
        self.add_game_frame.pack(fill=tk.X, pady=(0, 6))
        ag_inner = tk.Frame(self.add_game_frame, bg=CARD)
        ag_inner.pack(fill=tk.X, padx=10, pady=6)
        tk.Label(ag_inner, text="添加新股:", bg=CARD, fg=DIM, font=('Microsoft YaHei', 9)).pack(side=tk.LEFT)
        self.add_code_game = tk.Entry(ag_inner, width=8, bg='#1a2030', fg=TEXT, insertbackground=TEXT,
                                       font=('Microsoft YaHei', 10), bd=1, relief=tk.SOLID)
        self.add_code_game.pack(side=tk.LEFT, padx=6)
        self.add_code_game.bind('<Return>', lambda e: self.add_stock_in_game())
        tk.Button(ag_inner, text="添加", command=self.add_stock_in_game, bg=BLUE, fg='white',
                  font=('Microsoft YaHei', 9), bd=0, padx=10, pady=2, cursor='hand2').pack(side=tk.LEFT)
        
        # ---- CHART TABS ----
        self.tab_frame = tk.Frame(self.game_frame, bg=CARD, bd=1, relief=tk.SOLID, highlightbackground='#1e2530')
        self.tab_frame.pack(fill=tk.X, pady=(0, 6))
        self.tab_container = tk.Frame(self.tab_frame, bg=CARD)
        self.tab_container.pack(fill=tk.X, padx=6, pady=6)
        
        # ---- CHART ----
        self.fig = Figure(figsize=(10, 5), dpi=100, facecolor=CARD)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor(CARD)
        self.ax.tick_params(colors=DIM, labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color('#1e2530')
        
        # Chart canvas - fixed height
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.game_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=False, padx=0, pady=(0, 6))
        self.canvas.get_tk_widget().config(height=380)
        
        # ---- BOTTOM PANEL ----
        bottom_frame = tk.Frame(self.game_frame, bg=BG, height=200)
        bottom_frame.pack(fill=tk.BOTH, expand=True)
        bottom_frame.pack_propagate(False)  # Keep height
        
        # Portfolio
        self.portfolio_frame = tk.Frame(bottom_frame, bg=CARD, bd=1, relief=tk.SOLID, highlightbackground='#1e2530')
        self.portfolio_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 3))
        tk.Label(self.portfolio_frame, text="持仓", bg=CARD, fg=DIM, font=('Microsoft YaHei', 9, 'bold')).pack(padx=10, pady=(6, 2), anchor='w')
        self.holdings_text = tk.Text(self.portfolio_frame, height=6, bg=CARD, fg=TEXT, font=('Microsoft YaHei', 9),
                                      bd=0, wrap=tk.WORD, state=tk.DISABLED)
        self.holdings_text.pack(fill=tk.X, padx=10, pady=(0, 6))
        
        # Trade
        self.trade_frame = tk.Frame(bottom_frame, bg=CARD, bd=1, relief=tk.SOLID, highlightbackground='#1e2530')
        self.trade_frame.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 3))
        tk.Label(self.trade_frame, text="交易", bg=CARD, fg=DIM, font=('Microsoft YaHei', 9, 'bold')).pack(padx=10, pady=(6, 2), anchor='w')
        
        trade_inner = tk.Frame(self.trade_frame, bg=CARD)
        trade_inner.pack(fill=tk.X, padx=10, pady=4)
        
        self.trade_stock_var = tk.StringVar()
        self.trade_stock_combo = ttk.Combobox(trade_inner, textvariable=self.trade_stock_var, state='readonly',
                                               font=('Microsoft YaHei', 10), width=18)
        self.trade_stock_combo.pack(fill=tk.X, pady=2)
        
        lot_frame = tk.Frame(trade_inner, bg=CARD)
        lot_frame.pack(fill=tk.X, pady=2)
        tk.Label(lot_frame, text="手数:", bg=CARD, fg=DIM, font=('Microsoft YaHei', 9)).pack(side=tk.LEFT)
        self.lots_entry = tk.Entry(lot_frame, width=8, bg='#1a2030', fg=TEXT, insertbackground=TEXT,
                                    font=('Microsoft YaHei', 10), bd=1, relief=tk.SOLID)
        self.lots_entry.insert(0, "1")
        self.lots_entry.pack(side=tk.LEFT, padx=6)
        tk.Label(lot_frame, text="1手=100股", bg=CARD, fg=DIM, font=('Microsoft YaHei', 8)).pack(side=tk.LEFT)
        
        btn_frame = tk.Frame(trade_inner, bg=CARD)
        btn_frame.pack(fill=tk.X, pady=2)
        tk.Button(btn_frame, text="买入", command=lambda: self.do_trade('buy'), bg=RED, fg='white',
                  font=('Microsoft YaHei', 10, 'bold'), bd=0, padx=12, pady=2, cursor='hand2').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=1)
        tk.Button(btn_frame, text="卖出", command=lambda: self.do_trade('sell'), bg=GREEN, fg='white',
                  font=('Microsoft YaHei', 10, 'bold'), bd=0, padx=12, pady=2, cursor='hand2').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=1)
        tk.Button(btn_frame, text="清仓", command=self.sell_all, bg='#555', fg='#ccc',
                  font=('Microsoft YaHei', 9), bd=0, padx=8, pady=2, cursor='hand2').pack(side=tk.LEFT, padx=1)
        
        # Quick trade
        quick_frame = tk.Frame(trade_inner, bg=CARD)
        quick_frame.pack(fill=tk.X, pady=2)
        for label, frac in [("全仓", 1), ("半仓", 0.5), ("1/3", 0.33), ("1/4", 0.25)]:
            tk.Button(quick_frame, text=label, command=lambda f=frac: self.quick_buy(f), bg='#444', fg='#ccc',
                      font=('Microsoft YaHei', 8), bd=0, padx=6, pady=1, cursor='hand2').pack(side=tk.LEFT, padx=1)
        tk.Label(quick_frame, text="  ", bg=CARD).pack(side=tk.LEFT)
        for label, frac in [("全卖", 1), ("半卖", 0.5), ("1/3", 0.33), ("1/4", 0.25)]:
            tk.Button(quick_frame, text=label, command=lambda f=frac: self.quick_sell(f), bg='#444', fg='#ccc',
                      font=('Microsoft YaHei', 8), bd=0, padx=6, pady=1, cursor='hand2').pack(side=tk.LEFT, padx=1)
        
        # Transactions
        self.tx_frame = tk.Frame(bottom_frame, bg=CARD, bd=1, relief=tk.SOLID, highlightbackground='#1e2530')
        self.tx_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(self.tx_frame, text="交易记录", bg=CARD, fg=DIM, font=('Microsoft YaHei', 9, 'bold')).pack(padx=10, pady=(6, 2), anchor='w')
        self.tx_text = tk.Text(self.tx_frame, height=6, bg=CARD, fg=TEXT, font=('Consolas', 9),
                                bd=0, wrap=tk.WORD, state=tk.DISABLED)
        self.tx_text.pack(fill=tk.X, padx=10, pady=(0, 6))
    
    # ============================================================
    # ACTIONS
    # ============================================================
    def add_stock(self, code=None):
        if not code:
            code = self.code_entry.get().strip()
        if not code or len(code) != 6:
            messagebox.showwarning("提示", "请输入6位股票代码")
            return
        
        self._set_status("正在获取数据...")
        self.root.update()
        
        def _fetch():
            ok, msg = self.game.add_stock(code)
            self.root.after(0, lambda: self._on_add_stock(ok, msg, code))
        
        threading.Thread(target=_fetch, daemon=True).start()
    
    def _on_add_stock(self, ok, msg, code):
        if ok:
            self.added_stocks[code] = msg.split('(')[1].split(')')[0] if '(' in msg else code
            self.stock_list_label.config(text="  ".join([f"[{c} {n}]" for c, n in self.added_stocks.items()]))
            self.start_btn.config(state=tk.NORMAL)
            self.code_entry.delete(0, tk.END)
            self._set_status(f"添加成功: {msg}")
            messagebox.showinfo("成功", msg)
        else:
            self._set_status(f"添加失败: {msg}")
            messagebox.showerror("错误", msg)
    
    def add_stock_in_game(self):
        code = self.add_code_game.get().strip()
        if code:
            self.add_stock(code)
            self.add_code_game.delete(0, tk.END)
            self.root.after(2000, self.refresh_all)
    
    def start_game(self):
        sd = self.date_entry.get().strip()
        try:
            ic = float(self.cash_entry.get().strip())
        except:
            ic = 100000
        
        if not sd:
            messagebox.showwarning("提示", "请选择起始日期")
            return
        
        self._set_status("正在初始化游戏...")
        self.root.update()
        
        def _start():
            lookback = int(self.lookback_var.get())
            ok, msg = self.game.start_game(sd, ic, lookback)
            self.root.after(0, lambda: self._on_start(ok, msg))
        
        threading.Thread(target=_start, daemon=True).start()
    
    def _on_start(self, ok, msg):
        if ok:
            self.setup_frame.pack_forget()
            self.game_frame.pack(fill=tk.BOTH, expand=True)
            self.root.update_idletasks()  # Force layout recalculation
            self._set_status("游戏中")
            self.root.bind('<Right>', lambda e: self.skip_days(1))
            self.root.bind('<space>', lambda e: self.skip_days(1))
            self.root.after(200, self.refresh_all)  # Defer refresh to let UI settle
            messagebox.showinfo("开始", msg)
        else:
            messagebox.showerror("错误", msg)
    
    def skip_days(self, days):
        self.game.advance_day(days)
        self.refresh_all()
    
    def end_game(self):
        if messagebox.askyesno("结束", "确定结束游戏吗？"):
            self.game.advance_day(99999)
            self.refresh_all()
            self._set_status("游戏结束")
    
    def do_trade(self, trade_type):
        code = self.trade_stock_var.get()
        if not code or not code.startswith('6'):
            messagebox.showwarning("提示", "请先选择股票")
            return
        code = code.split()[0] if ' ' in code else code
        
        try:
            lots = int(self.lots_entry.get())
        except:
            messagebox.showwarning("提示", "请输入有效手数")
            return
        
        if trade_type == 'buy':
            ok, msg = self.game.buy_stock(code, lots)
        else:
            ok, msg = self.game.sell_stock(code, lots)
        
        if ok:
            self.lots_entry.delete(0, tk.END)
            self.lots_entry.insert(0, "1")
            messagebox.showinfo("成功", msg)
        else:
            messagebox.showerror("错误", msg)
        self.refresh_all()
    
    def sell_all(self):
        code = self.trade_stock_var.get()
        if not code: return
        code = code.split()[0] if ' ' in code else code
        ok, msg = self.game.sell_stock(code, 0)
        if ok:
            messagebox.showinfo("成功", msg)
        else:
            messagebox.showerror("错误", msg)
        self.refresh_all()
    
    def quick_buy(self, fraction):
        code = self.trade_stock_var.get()
        if not code: return
        code = code.split()[0] if ' ' in code else code
        prices = self.game.get_current_prices()
        if code not in prices:
            messagebox.showwarning("提示", "当前日期无数据")
            return
        price = prices[code]['close']
        max_lots = int(self.game.cash * fraction / (price * 100))
        if max_lots <= 0:
            messagebox.showwarning("提示", f"资金不足买1手(需¥{price*100:,.0f})")
            return
        self.lots_entry.delete(0, tk.END)
        self.lots_entry.insert(0, str(max_lots))
        self.do_trade('buy')
    
    def quick_sell(self, fraction):
        code = self.trade_stock_var.get()
        if not code: return
        code = code.split()[0] if ' ' in code else code
        h = self.game.portfolio.get(code)
        if not h or h['shares'] <= 0:
            messagebox.showwarning("提示", "无持仓")
            return
        lots = max(1, int(h['shares'] / 100 * fraction))
        self.lots_entry.delete(0, tk.END)
        self.lots_entry.insert(0, str(lots))
        self.do_trade('sell')
    
    def remove_stock(self, code):
        if code not in self.game.stocks: return
        if code in self.game.portfolio and self.game.portfolio[code].get('shares', 0) > 0:
            messagebox.showwarning("提示", "请先卖出持仓")
            return
        if messagebox.askyesno("删除", f"确定删除 {self.game.stocks[code]['name']} 吗？"):
            del self.game.stocks[code]
            if self.selected_chart_code == code:
                self.selected_chart_code = None
            self.refresh_all()
    
    def reset_game(self):
        if messagebox.askyesno("重置", "确定重置所有数据吗？"):
            self.game.reset()
            self.added_stocks.clear()
            self.stock_list_label.config(text="等待添加...")
            self.start_btn.config(state=tk.DISABLED)
            self.game_frame.pack_forget()
            self.setup_frame.pack(fill=tk.X, pady=(0, 8))
            self._set_status("已重置")
    
    def _on_auto_change(self, event=None):
        val = self.auto_var.get()
        strat_map = {'综合策略': 'unified'}
        if val in strat_map:
            self.game.auto_pilot = True
            self.game.auto_strategy = strat_map[val]
        else:
            self.game.auto_pilot = False
    
    # ============================================================
    # REFRESH
    # ============================================================
    def refresh_all(self):
        try:
            state = self.game.get_full_state()
            self._update_stats(state)
            self._update_tabs(state)
            self._update_trade_dropdown(state)
            self._update_holdings(state)
            self._update_transactions(state)
            self._draw_chart(state)
        except Exception as e:
            print(f"refresh_all error: {e}")
            import traceback; traceback.print_exc()
    
    def _update_stats(self, state):
        pv = state['portfolio_value']
        for stat_key, pv_key in [('total', 'total'), ('cash', 'cash'), ('stock', 'stock_value')]:
            val, sub = self.stat_labels[stat_key]
            val.config(text='¥{:,.0f}'.format(pv[pv_key]))
        
        pnl_val, pnl_sub = self.stat_labels['pnl']
        pnl = pv['pnl']
        pnl_val.config(text=f"{'+' if pnl>=0 else ''}¥{abs(pnl):,.0f}", fg=RED if pnl >= 0 else GREEN)
        pnl_sub.config(text=f"{'+' if pv['pnl_pct']>=0 else ''}{pv['pnl_pct']:.2f}%",
                       fg=RED if pnl >= 0 else GREEN)
        
        # Benchmark
        bench_val, bench_sub = self.stat_labels['bench']
        bp = pv.get('benchmark_pct', 0)
        bench_val.config(text=f"{'+' if bp>=0 else ''}{bp:.2f}%", fg=RED if bp >= 0 else GREEN)
        bench_sub.config(text="等权持有基准")
        
        # Daily change
        daily_val, daily_sub = self.stat_labels['daily']
        prices = state.get('prices', {})
        stks = list(state.get('stocks', {}).keys())
        if stks and stks[0] in prices:
            p = prices[stks[0]]
            dchg = (p['close'] - p['prev_close']) / p['prev_close'] * 100 if p['prev_close'] > 0 else 0
            daily_val.config(text=f"{'+' if dchg>=0 else ''}{dchg:.2f}%",
                            fg=RED if dchg >= 0 else GREEN)
            daily_sub.config(text=f"¥{p['close']:.2f}")
        else:
            daily_val.config(text="--", fg=TEXT)
            daily_sub.config(text="")
        
        self.day_info_label.config(text=f"第 {state['current_day']+1} / {state['total_days']} 天")
        self.date_label.config(text=state.get('current_date', '----'))
        
        # Progress bar
        pg = state['current_day'] / max(1, state['total_days'] - 1) * 200
        self.progress_canvas.delete('all')
        self.progress_canvas.create_rectangle(0, 0, pg, 6, fill=BLUE, outline='')
    
    def _update_tabs(self, state):
        for w in self.tab_container.winfo_children():
            w.destroy()
        
        stocks = state.get('stocks', {})
        scores = state.get('quant_scores', {})
        if not stocks: return
        
        if not self.selected_chart_code or self.selected_chart_code not in stocks:
            self.selected_chart_code = list(stocks.keys())[0]
        
        for code, s in stocks.items():
            is_active = code == self.selected_chart_code
            sc = scores.get(code, {})
            sc_text = f" {sc['score']:.0f}" if sc else ""
            frame = tk.Frame(self.tab_container, bg='#1b3a1b' if is_active else '#1a2030',
                            bd=1, relief=tk.SOLID, highlightbackground=RED if is_active else '#2a3545')
            frame.pack(side=tk.LEFT, padx=2)
            
            btn = tk.Label(frame, text=s['name'] + sc_text, bg=frame['bg'], fg=RED if is_active else DIM,
                          font=('Microsoft YaHei', 9), padx=8, pady=2, cursor='hand2')
            btn.pack(side=tk.LEFT)
            btn.bind('<Button-1>', lambda e, c=code: self._switch_chart(c))
            
            # Delete button
            dl = tk.Label(frame, text=" ×", bg=frame['bg'], fg=DIM, font=('Microsoft YaHei', 10),
                         padx=2, pady=2, cursor='hand2')
            dl.pack(side=tk.LEFT)
            dl.bind('<Button-1>', lambda e, c=code: self.remove_stock(c))
            dl.bind('<Enter>', lambda e, l=dl: l.config(fg='#fff'))
            dl.bind('<Leave>', lambda e, l=dl: l.config(fg=DIM))
    
    def _switch_chart(self, code):
        self.selected_chart_code = code
        self.refresh_all()
    
    def _update_trade_dropdown(self, state):
        prices = state.get('prices', {})
        stocks = state.get('stocks', {})
        scores = state.get('quant_scores', {})
        items = []
        for c, s in stocks.items():
            p = prices.get(c)
            sc = scores.get(c, {})
            sc_str = f" Q:{sc['score']:.0f}" if sc else ""
            items.append(f"{c} {s['name']}" + (f" ¥{p['close']:.2f}{sc_str}" if p else " (无数据)"))
        self.trade_stock_combo['values'] = items
        # Auto-select viewed chart stock
        if self.selected_chart_code and self.selected_chart_code in stocks:
            for item in items:
                if item.startswith(self.selected_chart_code + ' '):
                    self.trade_stock_var.set(item)
                    break
        elif items and not self.trade_stock_var.get():
            self.trade_stock_var.set(items[0])
    
    def _update_holdings(self, state):
        pf = state.get('portfolio', {})
        prices = state.get('prices', {})
        self.holdings_text.config(state=tk.NORMAL)
        self.holdings_text.delete('1.0', tk.END)
        if not pf:
            self.holdings_text.insert('1.0', "空仓")
        else:
            lines = []
            for code, h in pf.items():
                p = prices.get(code)
                if not p: continue
                mv = h['shares'] * p['close']
                cost = h['cost_basis']
                pnl = mv - h['shares'] * cost
                pp = (p['close'] - cost) / cost * 100 if cost > 0 else 0
                sign = '+' if pnl >= 0 else ''
                lines.append(f"{p['name']}: {h['shares']}股 @{p['close']:.2f} | {sign}¥{abs(pnl):,.0f} ({sign}{pp:.1f}%)")
            self.holdings_text.insert('1.0', '\n'.join(lines) if lines else "空仓")
        self.holdings_text.config(state=tk.DISABLED)
    
    def _update_transactions(self, state):
        txns = state.get('recent_transactions', [])
        self.tx_text.config(state=tk.NORMAL)
        self.tx_text.delete('1.0', tk.END)
        if not txns:
            self.tx_text.insert('1.0', "暂无")
        else:
            lines = []
            for t in reversed(txns[-15:]):
                act = '买' if t['type'] == 'BUY' else '卖'
                fee = f" 费¥{t.get('fee',0):.0f}" if t.get('fee') else ''
                lines.append(f"{t['date']} {act} {t['name']} {t['shares']}股 @{t['price']:.2f} ¥{t['amount']:,.0f}{fee}")
            self.tx_text.insert('1.0', '\n'.join(lines))
        self.tx_text.config(state=tk.DISABLED)
    
    # ============================================================
    # CHART
    # ============================================================
    def _draw_chart(self, state):
        code = self.selected_chart_code
        if not code:
            self.ax.clear(); self.ax.set_facecolor(CARD); self.canvas.draw()
            return
        
        lookback = getattr(self.game, 'chart_lookback', 100)
        data = self.game.get_historical_prices(code, lookback)
        if len(data) < 2:
            self.ax.clear(); self.ax.set_facecolor(CARD)
            self.ax.text(0.5, 0.5, '无K线数据', transform=self.ax.transAxes,
                        ha='center', va='center', color=DIM, fontsize=14)
            self.canvas.draw()
            return
        
        # Clear old twin axes
        for ax in list(self.fig.axes):
            if ax != self.ax:
                self.fig.delaxes(ax)
        
        try:
            closes = [d['close'] for d in data]
            highs = [d['high'] for d in data]
            lows = [d['low'] for d in data]
            opens = [d['open'] for d in data]
            volumes = [d['volume'] for d in data]
            dates = [datetime.strptime(d['date'], '%Y-%m-%d') for d in data]
            
            all_vals = closes + highs + lows
            mn, mx = min(all_vals) * 0.96, max(all_vals) * 1.04
            rng = mx - mn or 1
            
            self.ax.clear()
            self.ax.set_facecolor(CARD)
            
            # Candlesticks
            width = max(0.3, 0.7 * (dates[1] - dates[0]).days / 5 if len(dates) > 1 else 0.6)
            for i in range(min(len(data), 200)):  # Limit to 200 for performance
                is_up = closes[i] >= opens[i]
                color = UP_COLOR if is_up else DOWN_COLOR
                body_bottom = min(opens[i], closes[i])
                body_height = max(abs(closes[i] - opens[i]), rng * 0.001)
                self.ax.add_patch(Rectangle((mdates.date2num(dates[i]) - width/2, body_bottom),
                                            width, body_height, facecolor=color, edgecolor=color, linewidth=0.5))
                self.ax.plot([dates[i], dates[i]], [lows[i], highs[i]], color=color, linewidth=0.8)
            
            # MA lines
            for period, ma_color in zip([5, 10, 20, 30, 60], MA_COLORS):
                if len(data) < period: continue
                ma_vals, ma_dates = [], []
                for i in range(period - 1, len(data)):
                    ma = sum(closes[i-period+1:i+1]) / period
                    ma_vals.append(ma); ma_dates.append(dates[i])
                if ma_vals:
                    self.ax.plot(ma_dates, ma_vals, color=ma_color, linewidth=1)
                    self.ax.annotate(f'MA{period}', xy=(ma_dates[-1], ma_vals[-1]),
                                    xytext=(4, 0), textcoords='offset points',
                                    fontsize=7, color=ma_color, fontweight='bold')
            
            # Volume (twin axis)
            ax2 = self.ax.twinx()
            vol_max = max(volumes) if volumes else 1
            vol_colors = [UP_COLOR if closes[i] >= opens[i] else DOWN_COLOR for i in range(len(data))]
            ax2.bar(dates, [v / vol_max * rng * 0.2 + mn for v in volumes],
                    width=width * 0.8, color=vol_colors, alpha=0.4)
            ax2.set_ylim(mn, mx); ax2.tick_params(colors=DIM, labelsize=7)
            
            self.ax.set_xlim(dates[0], dates[-1])
            self.ax.set_ylim(mn, mx)
            self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
            locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
            self.ax.xaxis.set_major_locator(locator)
            self.ax.tick_params(colors=DIM, labelsize=8)
            for spine in self.ax.spines.values():
                spine.set_color('#1e2530')
            self.ax.grid(True, color='#1a2030', linewidth=0.5, alpha=0.5)
            
            # B/S trade markers
            markers = state.get('trade_markers', [])
            date_to_idx = {d['date']: i for i, d in enumerate(data)}
            for m in markers:
                if m['code'] == code and m['date'] in date_to_idx:
                    i = date_to_idx[m['date']]
                    x, y = mdates.date2num(dates[i]), highs[i]
                    color = RED if m['type'] == 'BUY' else GREEN
                    label = 'B' if m['type'] == 'BUY' else 'S'
                    self.ax.annotate(label, xy=(x, y), xytext=(0, 8), textcoords='offset points',
                                    fontsize=9, fontweight='bold', color=color, ha='center',
                                    bbox=dict(boxstyle='circle,pad=0.1', facecolor=color, edgecolor='none', alpha=0.3))
            
            self.fig.tight_layout()
            self.canvas.draw()
        except Exception as e:
            print(f"Chart draw error: {e}")
            import traceback; traceback.print_exc()


# ============================================================
# MAIN
# ============================================================
def main():
    root = tk.Tk()
    app = StockGameApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()
