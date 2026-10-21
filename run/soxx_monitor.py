#!/usr/bin/env python3
"""
US ETF 分时/多周期看盘器（PyQt5 + matplotlib）
=============================================
功能：
  · 标的可输入（默认 SOXX，SOXL 同样支持）
  · 周期：1分 / 5分 / 15分 / 30分 / 60分 / 日 / 周 / 月
  · 均线 MA5 / MA17 / MA21 / MA45（按所选周期取平均）
  · 成交量
  · 秒级价格刷新（约 1.5s，轮询东财报价）
  · 手动画线：水平线 / 趋势线；当前价在线下方=压力线(红)，上方=支撑线(绿)
  · 画线按 标的+周期 保存到 data/gui_lines.json，重启保留

数据来源：东方财富（美股：报价 + 1分钟K） + Nasdaq 日线（经 core/us_data）
运行：  python run/soxx_monitor.py
        python run/soxx_monitor.py --symbol SOXL
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import numpy as np
import pandas as pd
import requests

import matplotlib
matplotlib.use("Qt5Agg")
matplotlib.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Zen Hei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

from matplotlib.figure import Figure
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.patches import Rectangle
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtWidgets import QShortcut

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))
DATA_DIR = os.path.join(ROOT, "data")
LINES_FILE = os.path.join(DATA_DIR, "gui_lines.json")

UP = "#e03c3c"      # 涨/支撑 红
DOWN = "#1f9e8f"    # 跌/压力 绿
GRID = "#2b333d"
BG = "#0e1116"
FG = "#d7dde5"
MA_COLORS = {5: "#ffd54f", 17: "#e040fb", 21: "#40c4ff", 45: "#ff8a65"}
MA_WINDOWS = [5, 17, 21, 45]

EM_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
EM_HOSTS = ["https://push2his.eastmoney.com", "https://push2.eastmoney.com", "https://push2delay.eastmoney.com"]


# ---------------- 数据层 ----------------
def em_get(path: str, retries: int = 3):
    for i in range(retries):
        host = EM_HOSTS[i % len(EM_HOSTS)]
        try:
            r = requests.get(host + path, headers=EM_H, timeout=8)
            r.raise_for_status()
            j = r.json()
            if j.get("data") is not None:
                return j
        except Exception:
            pass
        time.sleep(0.3)
    return None


def secids(symbol: str):
    s = symbol.upper().strip()
    return [f"105.{s}", f"106.{s}", f"107.{s}"]  # NASDAQ / NYSE / AMEX


def fetch_quote(symbol: str):
    for sid in secids(symbol):
        js = em_get(f"/api/qt/stock/get?secid={sid}&fields=f43,f57,f58,f60,f169,f170,f47,f48,f86")
        d = (js or {}).get("data") or {}
        if d.get("f43"):
            price = d["f43"] / 1000.0
            prev = (d.get("f60") or 0) / 1000.0 or price
            return {"symbol": d.get("f57", symbol), "name": d.get("f58", ""),
                    "price": price, "prev": prev, "pct": (price / prev - 1) * 100 if prev else 0.0,
                    "volume": d.get("f47"), "ts": d.get("f86")}
    return None


OKX_H = {"User-Agent": "Mozilla/5.0"}
OKX_HOSTS = ["https://www.okx.com", "https://aws.okx.com"]


def _okx_candidates(inst_id: str):
    inst_id = str(inst_id).strip()
    base = inst_id.split("-")[0]
    cands = [inst_id]
    for suf in ("-USDT", "-USD", "-USDC"):
        for nm in (base, base + "x"):
            c = nm + suf
            if c not in cands:
                cands.append(c)
    return cands[:8]


def fetch_quote_okx(inst_id: str, bases=None):
    """欧易公开行情。instId 例如 SOXX-USDT / SPYx-USDT；未命中自动尝试常见别名。"""
    bases = bases or OKX_HOSTS[:1]
    for host in bases:
        for c in _okx_candidates(inst_id):
            try:
                r = requests.get(host + "/api/v5/market/ticker",
                                 params={"instId": c}, headers=OKX_H, timeout=5)
                d = (r.json() or {}).get("data") or []
                if d:
                    last = float(d[0]["last"])
                    o24 = float(d[0].get("open24h") or last)
                    return {"symbol": c, "name": "OKX",
                            "price": last, "prev": o24,
                            "pct": (last / o24 - 1) * 100 if o24 else 0.0,
                            "volume": 0, "ts": 0}
            except Exception:
                continue
    return None


def _parse_kline(kl):
    rows = []
    for k in kl:
        p = k.split(",")
        try:
            rows.append({"date": pd.to_datetime(p[0]), "open": float(p[1]),
                         "close": float(p[2]), "high": float(p[3]),
                         "low": float(p[4]), "volume": float(p[5])})
        except (ValueError, IndexError):
            continue
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("date").sort_index()


def _parse_trends(tr):
    # trends2: 时间,开,收,高,低,量,额,均价
    rows = []
    for k in tr:
        p = k.split(",")
        try:
            rows.append({"date": pd.to_datetime(p[0]), "open": float(p[1]),
                         "close": float(p[2]), "high": float(p[3]),
                         "low": float(p[4]), "volume": float(p[5])})
        except (ValueError, IndexError):
            continue
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("date").sort_index()


def fetch_trends(symbol: str, ndays: int = 1):
    for _ in range(3):
        for sid in secids(symbol):
            js = em_get(f"/api/qt/stock/trends2/get?secid={sid}&ndays={ndays}&iscr=0"
                        "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58")
            tr = ((js or {}).get("data", {}) or {}).get("trends") or []
            if tr:
                df = _parse_trends(tr)
                if df is not None:
                    return df
        time.sleep(0.5)
    return None


def fetch_1m(symbol: str, lmt: int = 1500):
    # kline 优先（可多日）；挂了就用 trends2（当日分时）
    for sid in secids(symbol):
        js = em_get(f"/api/qt/stock/kline/get?secid={sid}&klt=1&fqt=1&lmt={lmt}&end=20500101"
                    "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
        kl = ((js or {}).get("data", {}) or {}).get("klines") or []
        if kl:
            df = _parse_kline(kl)
            if df is not None:
                return df
    return fetch_trends(symbol)


def fetch_intraday(symbol: str, klt: int):
    if klt == 1:
        return fetch_1m(symbol)
    for sid in secids(symbol):
        js = em_get(f"/api/qt/stock/kline/get?secid={sid}&klt={klt}&fqt=1&lmt=400&end=20500101"
                    "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
        kl = ((js or {}).get("data", {}) or {}).get("klines") or []
        if kl:
            df = _parse_kline(kl)
            if df is not None:
                return df
    base = fetch_1m(symbol)
    if base is None or base.empty:
        return None
    return base.resample(f"{klt}min").agg(AGG).dropna()


def fetch_daily(symbol: str, days: int = 1200):
    try:
        from us_data import fetch_daily_kline
        d = fetch_daily_kline(symbol, days).copy()
        d["date"] = pd.to_datetime(d["date"])
        return d.set_index("date")[["open", "high", "low", "close", "volume"]].sort_index()
    except Exception:
        return None


# ---------------- 夜盘（盘前/盘后，Nasdaq 逐笔） ----------------
NASDAQ_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nasdaq.com/"}
NIGHT_CACHE = os.path.join(DATA_DIR, "gui_night.json")


def _parse_et_date(txt):
    import re
    from datetime import datetime
    m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", txt or "")
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
    except Exception:
        return None


def fetch_extended_ticks(symbol: str, markettype: str):
    try:
        r = requests.get(
            f"https://api.nasdaq.com/api/quote/{symbol}/extended-trading?assetclass=etf&markettype={markettype}",
            headers=NASDAQ_H, timeout=12)
        d = (r.json() or {}).get("data") or {}
        rows = ((d.get("tradeDetailTable") or {}).get("rows") or [])
        date = None
        for info in (d.get("lastUpdateInfo") or []):
            date = _parse_et_date(info)
            if date:
                break
        ticks = []
        for row in rows:
            t = row.get("time")
            try:
                p = float(str(row.get("price", "")).replace("$", "").replace(",", ""))
                v = int(str(row.get("shareVolume", "0")).replace(",", "") or 0)
            except Exception:
                continue
            if t and p:
                ticks.append([t, p, v])
        return {"date": date, "session": markettype, "ticks": ticks}
    except Exception:
        return {"date": None, "session": markettype, "ticks": []}


def night_bars(symbol: str, tf: str = "1m"):
    """轮询盘前/盘后逐笔 → 累积 → 聚合成 1分钟K（取最新交易日）。"""
    try:
        with open(NIGHT_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    node = cache.setdefault(symbol.upper(), {})
    for mt in ("pre", "post"):
        fresh = fetch_extended_ticks(symbol, mt)
        d = fresh.get("date") or "unknown"
        k = f"{d}|{mt}"
        arr = node.get(k, [])
        seen = {tuple(x) for x in arr}
        for t in fresh["ticks"]:
            if tuple(t) not in seen:
                arr.append(list(t))
                seen.add(tuple(t))
        node[k] = arr
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(NIGHT_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass

    recs = []
    for k, arr in node.items():
        d, mt = k.split("|")
        if d == "unknown":
            continue
        for t, p, v in arr:
            try:
                recs.append({"date": pd.to_datetime(f"{d} {t}"), "price": float(p), "volume": int(v)})
            except Exception:
                continue
    if not recs:
        return None
    df = pd.DataFrame(recs).sort_values("date")
    latest = df["date"].dt.date.max()
    df = df[df["date"].dt.date == latest]
    if df.empty:
        return None
    g = df.set_index("date").resample("1min").agg(
        {"price": ["first", "max", "min", "last"], "volume": "sum"}).dropna()
    g.columns = ["open", "high", "low", "close", "volume"]
    if tf != "1m":
        try:
            k = int(tf[:-1])
            g = g.resample(f"{k}min").agg(AGG).dropna()
        except Exception:
            pass
    return g


TF_RULES = {"1m": ("1min", "i"), "5m": ("5min", "i"), "15m": ("15min", "i"),
            "30m": ("30min", "i"), "60m": ("60min", "i"),
            "1d": ("1D", "d"), "1w": ("W-FRI", "d"), "1M": ("ME", "d")}

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


CACHE_DIR = os.path.join(DATA_DIR, "gui_cache")


def cache_save(symbol: str, tf: str, df: pd.DataFrame):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(os.path.join(CACHE_DIR, f"{symbol}_{tf}.csv"))
    except Exception:
        pass


def cache_load(symbol: str, tf: str):
    try:
        d = pd.read_csv(os.path.join(CACHE_DIR, f"{symbol}_{tf}.csv"),
                        index_col=0, parse_dates=True)
        return d[["open", "high", "low", "close", "volume"]]
    except Exception:
        return None


def _load_bars_live(symbol: str, tf: str):
    rule, kind = TF_RULES[tf]
    if kind == "i":
        return fetch_intraday(symbol, int(tf[:-1]))
    src = fetch_daily(symbol)
    if src is None or src.empty:
        return None
    if tf == "1d":
        return src
    try:
        return src.resample(rule).agg(AGG).dropna()
    except ValueError:
        return src.resample(rule.replace("ME", "M")).agg(AGG).dropna()


def load_bars(symbol: str, tf: str):
    df = _load_bars_live(symbol, tf)
    if df is not None and not df.empty:
        cache_save(symbol, tf, df)
        return df
    return None


# ---------------- 画线持久化 ----------------
def _load_lines() -> dict:
    try:
        with open(LINES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_lines(d: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = LINES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, LINES_FILE)
    except Exception:
        pass


# ---------------- 画布 ----------------
class ChartCanvas(FigureCanvas):
    def __init__(self, win):
        self.fig = Figure(figsize=(13, 7.5), dpi=100, facecolor=BG)
        super().__init__(self.fig)
        self.win = win
        self.bars = None
        self.mode = None
        self.p1 = None
        self.live_line = None
        self.preview_line = None
        self.ax = None
        self.axv = None
        self._press = None
        self._panning = False
        self._user_view = False
        self.selected = None
        self._drag_idx = None
        self._drag_handle = None
        self._drag_pushed = False
        self._move_idx = None
        self._move_start = None
        self._move_px = None
        self._move_orig = None
        self._move_pushed = False
        self._sel_line_artist = None
        self._sel_handles = []
        self.mpl_connect("button_press_event", self.on_click)
        self.mpl_connect("scroll_event", self.on_scroll)
        self.mpl_connect("motion_notify_event", self.on_motion)
        self.mpl_connect("button_release_event", self.on_release)
        # 限制重绘帧率（约 30fps），避免鼠标事件把重绘任务堆积
        self._dirty = False
        self._draw_timer = QtCore.QTimer(self)
        self._draw_timer.timeout.connect(self._on_draw_timer)
        self._draw_timer.start(33)

    def _request_draw(self):
        self._dirty = True

    def _on_draw_timer(self):
        if self._dirty:
            self._dirty = False
            self.draw_idle()

    # --- 坐标映射 ---
    def dt2x(self, dt):
        idx = self.bars.index
        if len(idx) < 2:
            return 0.0
        span = (idx[-1] - idx[0]).total_seconds() or 1.0
        return (dt - idx[0]).total_seconds() / span * (len(idx) - 1)

    def x2dt(self, x):
        idx = self.bars.index
        span = (idx[-1] - idx[0]).total_seconds()
        return idx[0] + pd.Timedelta(seconds=span * x / max(1, len(idx) - 1))

    def _classify(self, y_at_now):
        p = self.win.last_price or (self.bars["close"].iloc[-1] if self.bars is not None and len(self.bars) else 0)
        # 价格在线上方 → 支撑(绿)；在线下方 → 压力(红)
        return DOWN if p >= y_at_now else UP

    # --- 绘制 ---
    def redraw(self):
        bars = self.bars
        old_view = None
        if getattr(self, "_user_view", False) and self.ax is not None \
                and bars is not None and not bars.empty:
            try:
                old_view = (self.ax.get_xlim(), self.ax.get_ylim())
            except Exception:
                old_view = None
        self.fig.clear()
        self.preview_line = None
        if bars is None or bars.empty:
            ax = self.fig.add_subplot(111)
            ax.set_facecolor(BG)
            ax.text(0.5, 0.5, "NO DATA / load failed", color=FG, ha="center", va="center", fontsize=16)
            ax.set_axis_off()
            self.draw_idle()
            return

        dfs = bars.tail(600)   # 限制可见K线数量，保证流畅
        self.bars = dfs
        gs = self.fig.add_gridspec(2, 1, height_ratios=[4, 1], hspace=0.06)
        ax = self.fig.add_subplot(gs[0])
        axv = self.fig.add_subplot(gs[1], sharex=ax)
        self.ax = ax
        self.axv = axv
        for a in (ax, axv):
            a.set_facecolor(BG)
            a.grid(True, color=GRID, lw=0.6, alpha=0.7)
            for s in a.spines.values():
                s.set_color(GRID)
            a.tick_params(colors=FG, labelsize=9)

        n = len(dfs)
        x = np.arange(n)
        o, h, l, c = (dfs[k].values for k in ("open", "high", "low", "close"))
        up = c >= o
        # 蜡烛（Collection 批量绘制，避免数百个独立图元）
        wick_segs = [[(x[i], l[i]), (x[i], h[i])] for i in range(n)]
        wick_cols = [UP if up[i] else DOWN for i in range(n)]
        ax.add_collection(LineCollection(wick_segs, colors=wick_cols, linewidths=0.8))
        rects, rcols = [], []
        for i in range(n):
            if up[i]:
                y0, hgt = o[i], c[i] - o[i]
            else:
                y0, hgt = c[i], o[i] - c[i]
            rects.append(Rectangle((x[i] - 0.31, y0), 0.62, max(hgt, 1e-6)))
            rcols.append(UP if up[i] else DOWN)
        ax.add_collection(PatchCollection(rects, facecolors=rcols, edgecolors=rcols,
                                          linewidths=0.3, antialiaseds=False))
        # 均线
        for w in MA_WINDOWS:
            if n >= w:
                ma = dfs["close"].rolling(w).mean().values
                ax.plot(x, ma, color=MA_COLORS[w], lw=1.1, label=f"MA{w}", alpha=0.95)
        ax.legend(loc="upper left", fontsize=9, facecolor="#161b22", edgecolor=GRID,
                  labelcolor=FG, ncol=4, framealpha=0.8)
        # 成交量（Collection）
        vv = dfs["volume"].values.astype(float)
        vrects = [Rectangle((x[i] - 0.31, 0), 0.62, float(vv[i])) for i in range(n)]
        axv.add_collection(PatchCollection(vrects, facecolors=rcols, edgecolors=rcols,
                                           linewidths=0, antialiaseds=False))
        pad = (float(h.max()) - float(l.min())) * 0.05 or 1.0
        ax.set_ylim(float(l.min()) - pad, float(h.max()) + pad)
        axv.set_ylim(0, float(vv.max()) * 1.1 if vv.max() > 0 else 1)
        axv.set_ylabel("Volume", color=FG, fontsize=9)
        ax.set_ylabel("Price", color=FG, fontsize=9)

        # x 轴刻度
        k = 8
        step = max(1, n // k)
        ticks = list(range(0, n, step))
        fmt = "%H:%M" if self.win.tf.endswith("m") else ("%m-%d" if self.win.tf in ("1d", "1w") else "%Y-%m")
        labels = [dfs.index[i].strftime(fmt) for i in ticks]
        axv.set_xticks(ticks)
        axv.set_xticklabels(labels, color=FG, fontsize=9)
        ax.tick_params(labelbottom=False)

        ax.set_xlim(-1, n)
        if old_view:
            xl, yl = old_view
            # 不做右边界限位：允许查看右侧无数据区域
            ax.set_xlim(float(xl[0]), float(xl[1]))
            ax.set_ylim(yl)
        price = self.win.last_price or c[-1]
        ax.set_title(f"{self.win.symbol}   {self.win.tf}   Last {price:.2f}", color=FG,
                     fontsize=12, loc="left")

        # 已保存的线
        self._sel_line_artist = None
        self._sel_handles = []
        for i, ln in enumerate(self.win.get_lines()):
            self._render_line(ax, ln, price, i)

        # 实时价线
        self.live_line = ax.axhline(price, color="#ffffff", lw=0.8, ls=":", alpha=0.6)

        self.fig.subplots_adjust(left=0.06, right=0.99, top=0.94, bottom=0.06)
        self.draw_idle()

    def _render_line(self, ax, ln, price, idx=None):
        selected = (idx is not None and idx == self.selected)
        lw = 2.6 if selected else 1.3
        if ln["type"] == "h":
            col = self._classify(ln["y"])
            line = ax.axhline(ln["y"], color=col, lw=lw, ls="--", alpha=0.95)
            ax.text(ax.get_xlim()[1], ln["y"], f" {ln['y']:.2f}",
                    color=col, fontsize=9, va="center", ha="left")
            if selected:
                x0, x1 = ax.get_xlim()
                h1, = ax.plot([x0], [ln["y"]], marker="o", ms=9, color="#ffffff",
                              markeredgecolor=col, markeredgewidth=2, zorder=10)
                h2, = ax.plot([x1], [ln["y"]], marker="o", ms=9, color="#ffffff",
                              markeredgecolor=col, markeredgewidth=2, zorder=10)
                self._sel_line_artist = line
                self._sel_handles = [h1, h2]
        else:
            x1 = self.dt2x(pd.to_datetime(ln["x1"]))
            x2 = self.dt2x(pd.to_datetime(ln["x2"]))
            y1, y2 = ln["y1"], ln["y2"]
            if abs(x2 - x1) < 1e-9:
                return
            ynow = y1 + (y2 - y1) * (self.dt2x(self.bars.index[-1]) - x1) / (x2 - x1)
            col = self._classify(ynow)
            line, = ax.plot([x1, x2], [y1, y2], color=col, lw=lw, ls="--", alpha=0.95)
            if selected:
                h1, = ax.plot([x1], [y1], marker="o", ms=9, color="#ffffff",
                              markeredgecolor=col, markeredgewidth=2, zorder=10)
                h2, = ax.plot([x2], [y2], marker="o", ms=9, color="#ffffff",
                              markeredgecolor=col, markeredgewidth=2, zorder=10)
                self._sel_line_artist = line
                self._sel_handles = [h1, h2]

    def _line_endpoints(self, ln):
        if ln["type"] == "h":
            x0, x1 = self.ax.get_xlim()
            return [(x0, ln["y"]), (x1, ln["y"])]
        return [(self.dt2x(pd.to_datetime(ln["x1"])), ln["y1"]),
                (self.dt2x(pd.to_datetime(ln["x2"])), ln["y2"])]

    def _nearest_handle(self, event):
        if self.selected is None or self.ax is None:
            return None, None, None
        arr = self.win.get_lines()
        if not (0 <= self.selected < len(arr)):
            return None, None, None
        pts = self._line_endpoints(arr[self.selected])
        trans = self.ax.transData
        best_i, best_d = None, 1e18
        for i, (x, y) in enumerate(pts):
            px, py = trans.transform((x, y))
            d = ((event.x - px) ** 2 + (event.y - py) ** 2) ** 0.5
            if d < best_d:
                best_i, best_d = i, d
        return self.selected, best_i, best_d

    def update_live(self, price, pct):
        if self.live_line is not None:
            try:
                self.live_line.set_ydata([price, price])
            except Exception:
                pass
        if self.win.bars is not None and len(self.win.bars):
            self.fig.axes[0].set_title(f"{self.win.symbol}   {self.win.tf}   Last {price:.2f}  ({pct:+.2f}%)",
                                       color=FG, fontsize=12, loc="left")
        self._request_draw()

    # --- 交互画线 ---
    def on_click(self, event):
        if event.inaxes is None or self.bars is None:
            return
        if event.button == 3:  # 右键退出画线
            self._clear_preview()
            self.mode = None
            self.p1 = None
            self.win.on_mode_cleared()
            self.win.set_status("已退出画线模式")
            return
        # 中键：任意模式平移
        if event.button == 2:
            if event.xdata is not None and event.ydata is not None and self.ax is not None:
                self._press = (event.xdata, event.ydata, self.ax.get_xlim(), self.ax.get_ylim())
                self._panning = True
            return

        # 左键：无论什么模式，先判断是否点中线 / 端点
        if event.button == 1:
            # 1) 拖动已选中线的端点（容差 18px）
            if self.selected is not None:
                _, hi, hd = self._nearest_handle(event)
                if hi is not None and hd is not None and hd <= 18:
                    self._drag_idx = self.selected
                    self._drag_handle = hi
                    self._drag_pushed = False
                    self._move_idx = None
                    self.win.set_status("拖动端点调整位置（松开保存）")
                    return
            # 2) 点中线 → 选中（任何模式都生效）
            idx, dist = self._nearest_line(event)
            if idx is not None and dist is not None and dist <= 16:
                self._clear_preview()
                self.p1 = None
                self.selected = idx
                arr = self.win.get_lines()
                ln = arr[idx]
                self._move_idx = idx
                self._move_start = (event.xdata, event.ydata)
                self._move_px = (event.x, event.y)
                self._move_pushed = False
                if ln["type"] == "h":
                    self._move_orig = {"y": ln["y"]}
                else:
                    self._move_orig = {
                        "x1": self.dt2x(pd.to_datetime(ln["x1"])), "y1": ln["y1"],
                        "x2": self.dt2x(pd.to_datetime(ln["x2"])), "y2": ln["y2"]}
                self.redraw()
                self.win.set_status(
                    f"已选中第 {idx + 1} 条线：拖动线体平移，拖两端圆点调整，点空白取消")
                return
            # 3) 普通模式点空白 → 取消选中并平移
            if self.mode is None:
                if self.selected is not None:
                    self.selected = None
                    self.redraw()
                if event.xdata is not None and event.ydata is not None and self.ax is not None:
                    self._press = (event.xdata, event.ydata, self.ax.get_xlim(), self.ax.get_ylim())
                    self._panning = True
                return
            # 4) 画线模式点空白 → 继续画（下面处理）

        if self.mode is None:
            return
        y = event.ydata
        if y is None:
            return
        if self.mode == "h":
            self.win.add_line({"type": "h", "y": round(float(y), 4)})
            self.win.set_status("已添加水平线")
        elif self.mode == "trend":
            if self.p1 is None:
                self.p1 = (self.x2dt(event.xdata), float(y))
                self.win.set_status("趋势线：移动鼠标预览，点击第二个点完成（右键取消）")
            else:
                self._clear_preview(redraw=False)
                x1, y1 = self.p1
                self.win.add_line({"type": "trend", "x1": x1.isoformat(),
                                   "y1": round(y1, 4), "x2": self.x2dt(event.xdata).isoformat(),
                                   "y2": round(float(y), 4)})
                self.p1 = None
                self.win.set_status("已添加趋势线")

    def _nearest_line(self, event):
        """返回 (线索引, 像素距离)。用于删除线命中测试。"""
        if self.ax is None:
            return None, None
        try:
            trans = self.ax.transData
            px, py = event.x, event.y
            best_i, best_d = None, 1e18
            for i, ln in enumerate(self.win.get_lines()):
                if ln["type"] == "h":
                    _, dy = trans.transform((self.ax.get_xlim()[0], ln["y"]))
                    d = abs(py - dy)
                else:
                    x1 = self.dt2x(pd.to_datetime(ln["x1"]))
                    x2 = self.dt2x(pd.to_datetime(ln["x2"]))
                    X1, Y1 = trans.transform((x1, ln["y1"]))
                    X2, Y2 = trans.transform((x2, ln["y2"]))
                    d = self._dist_point_seg(px, py, X1, Y1, X2, Y2)
                if d < best_d:
                    best_i, best_d = i, d
            return best_i, best_d
        except Exception:
            return None, None

    @staticmethod
    def _dist_point_seg(px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
        t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        cx, cy = x1 + t * dx, y1 + t * dy
        return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5

    # --- 缩放/平移 ---
    def on_scroll(self, event):
        if self.ax is None or self.axv is None or self.bars is None:
            return
        if event.inaxes not in (self.ax, self.axv) or event.xdata is None:
            return
        factor = 0.93 if event.button == "up" else 1.075  # 降低滚轮灵敏度
        xlim = self.ax.get_xlim()
        xdata = event.xdata
        left = xdata - (xdata - xlim[0]) * factor
        right = left + (xlim[1] - xlim[0]) * factor
        n = len(self.bars)
        if right - left < 8:
            return
        if right - left > n * 4:
            cx = (left + right) / 2
            left, right = cx - n * 2, cx + n * 2
        self._user_view = True
        self.ax.set_xlim(left, right)
        self.axv.set_xlim(left, right)
        self._autoscale_y(left, right)
        self._request_draw()

    def on_motion(self, event):
        # 整体平移选中线（拖动线体）
        if self._move_idx is not None and event.xdata is not None and event.ydata is not None:
            if not self._move_pushed:
                px, py = self._move_px
                if abs(event.x - px) < 3 and abs(event.y - py) < 3:
                    return
                self.win.push_undo()
                self._move_pushed = True
            arr = self.win.get_lines()
            if 0 <= self._move_idx < len(arr):
                ln = arr[self._move_idx]
                dx = float(event.xdata) - float(self._move_start[0])
                dy = float(event.ydata) - float(self._move_start[1])
                if ln["type"] == "h":
                    ln["y"] = round(self._move_orig["y"] + dy, 4)
                    if self._sel_line_artist is not None:
                        self._sel_line_artist.set_ydata([ln["y"], ln["y"]])
                    for h in self._sel_handles:
                        h.set_ydata([ln["y"]])
                else:
                    nx1 = self._move_orig["x1"] + dx
                    nx2 = self._move_orig["x2"] + dx
                    ln["x1"] = self.x2dt(nx1).isoformat()
                    ln["y1"] = round(self._move_orig["y1"] + dy, 4)
                    ln["x2"] = self.x2dt(nx2).isoformat()
                    ln["y2"] = round(self._move_orig["y2"] + dy, 4)
                    if self._sel_line_artist is not None:
                        self._sel_line_artist.set_data([nx1, nx2], [ln["y1"], ln["y2"]])
                    for h, (hx, hy) in zip(self._sel_handles, [(nx1, ln["y1"]), (nx2, ln["y2"])]):
                        h.set_data([hx], [hy])
                self._request_draw()
            return
        # 拖动选中线的端点
        if self._drag_idx is not None and event.xdata is not None and event.ydata is not None:
            if not getattr(self, "_drag_pushed", False):
                self.win.push_undo()
                self._drag_pushed = True
            arr = self.win.get_lines()
            if 0 <= self._drag_idx < len(arr):
                ln = arr[self._drag_idx]
                x, y = float(event.xdata), float(event.ydata)
                if ln["type"] == "h":
                    ln["y"] = round(y, 4)
                    if self._sel_line_artist is not None:
                        self._sel_line_artist.set_ydata([y, y])
                    for h in self._sel_handles:
                        h.set_ydata([y])
                else:
                    if self._drag_handle == 0:
                        ln["x1"] = self.x2dt(x).isoformat(); ln["y1"] = round(y, 4)
                    else:
                        ln["x2"] = self.x2dt(x).isoformat(); ln["y2"] = round(y, 4)
                    pts = self._line_endpoints(ln)
                    if self._sel_line_artist is not None:
                        self._sel_line_artist.set_data([pts[0][0], pts[1][0]],
                                                       [pts[0][1], pts[1][1]])
                    for h, (hx, hy) in zip(self._sel_handles, pts):
                        h.set_data([hx], [hy])
                self._request_draw()
            return
        # 拖拽平移（X + Y 同时）
        if self._panning and self._press is not None \
                and event.xdata is not None and event.ydata is not None:
            x0, y0, xlim, ylim = self._press
            dx = x0 - event.xdata
            dy = y0 - event.ydata
            self._user_view = True
            self.ax.set_xlim(xlim[0] + dx, xlim[1] + dx)
            self.ax.set_ylim(ylim[0] + dy, ylim[1] + dy)
            self.axv.set_xlim(xlim[0] + dx, xlim[1] + dx)
            self._request_draw()
            return
        # 画线预览（橡皮筋）
        if self.mode and self.ax is not None and event.inaxes in (self.ax, self.axv):
            self._update_preview(event)

    def _clear_preview(self, redraw: bool = True):
        line = getattr(self, "preview_line", None)
        if line is not None:
            try:
                line.remove()
            except Exception:
                pass
            self.preview_line = None
            if redraw:
                self.draw_idle()

    def _update_preview(self, event):
        if self.ax is None:
            return
        self._clear_preview(redraw=False)
        if self.mode == "h" and event.ydata is not None:
            self.preview_line = self.ax.axhline(event.ydata, color="#ffffff",
                                                lw=1.0, ls="-.", alpha=0.75)
        elif self.mode == "trend" and self.p1 is not None \
                and event.xdata is not None and event.ydata is not None:
            x1 = self.dt2x(self.p1[0])
            self.preview_line, = self.ax.plot([x1, event.xdata], [self.p1[1], event.ydata],
                                              color="#ffffff", lw=1.0, ls="-.", alpha=0.85)
        self._request_draw()

    def on_release(self, event):
        self._panning = False
        self._press = None
        if self._drag_idx is not None:
            _save_lines(self.win._all_lines)
            self._drag_idx = None
            self._drag_handle = None
            self._drag_pushed = False
        if self._move_idx is not None:
            if getattr(self, "_move_pushed", False):
                _save_lines(self.win._all_lines)
                self.win.set_status("已保存线条平移")
            self._move_idx = None
            self._move_pushed = False
            self.win.set_status("已保存线条调整")

    def _autoscale_y(self, left, right):
        if self.bars is None or len(self.bars) == 0 or self.ax is None:
            return
        n = len(self.bars)
        i0 = max(0, int(np.floor(left)))
        i1 = min(n, int(np.ceil(right)) + 1)
        if i1 <= i0:
            return
        seg = self.bars.iloc[i0:i1]
        if seg.empty:
            return
        lo, hi = float(seg["low"].min()), float(seg["high"].max())
        pad = (hi - lo) * 0.05 or 1.0
        self.ax.set_ylim(lo - pad, hi + pad)
        vmax = float(seg["volume"].max())
        self.axv.set_ylim(0, vmax * 1.1 if vmax > 0 else 1)

    def reset_view(self):
        if self.bars is None or self.ax is None:
            return
        n = len(self.bars)
        self._user_view = False
        self.ax.set_xlim(-1, n)
        self.axv.set_xlim(-1, n)
        self._autoscale_y(-1, n)
        self.draw_idle()


# ---------------- 主窗口 ----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, symbol="SOXX", quote_ms=1000, source="em", okx_inst=None):
        super().__init__()
        self.symbol = symbol.upper()
        self.tf = "1m"
        self.quote_ms = int(quote_ms)
        self.source = source
        self.okx_inst = okx_inst or (self.symbol + "-USDT")
        self.okx_bases = None
        self._okx_q = None
        self._okx_busy = False
        self._okx_lock = threading.Lock()
        self._okx_next_try = 0.0
        self.bars = None
        self.last_price = None
        self._all_lines = _load_lines()
        self._undo_stack = []
        self._redo_stack = []
        self.setWindowTitle("US ETF 看盘 · 分时/多周期")
        self.resize(1400, 860)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        v = QtWidgets.QVBoxLayout(central)
        v.setContentsMargins(6, 6, 6, 6)

        # 工具栏
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("代码"))
        self.sym_edit = QtWidgets.QLineEdit(self.symbol)
        self.sym_edit.setFixedWidth(90)
        bar.addWidget(self.sym_edit)
        self.btn_load = QtWidgets.QPushButton("加载")
        bar.addWidget(self.btn_load)
        bar.addWidget(QtWidgets.QLabel("周期"))
        self.tf_box = QtWidgets.QComboBox()
        self.tf_box.addItems(["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1M"])
        bar.addWidget(self.tf_box)
        bar.addSpacing(12)
        self.btn_h = QtWidgets.QPushButton("水平线")
        self.btn_t = QtWidgets.QPushButton("趋势线")
        bar.addWidget(self.btn_h); bar.addWidget(self.btn_t)
        # 画线相关按钮：可切换高亮，再点一次关闭
        _mode_btn_style = ("QPushButton:checked{background:#4d9fff;color:#fff;"
                           "border:1px solid #4d9fff;font-weight:700;}")
        for _b in (self.btn_h, self.btn_t):
            _b.setCheckable(True)
            _b.setStyleSheet(_mode_btn_style)
        bar.addSpacing(12)
        self.chk_auto = QtWidgets.QCheckBox("自动刷新")
        self.chk_auto.setChecked(True)
        bar.addWidget(self.chk_auto)
        self.chk_night = QtWidgets.QCheckBox("夜盘(盘前/盘后)")
        bar.addWidget(self.chk_night)
        self.btn_reset = QtWidgets.QPushButton("重置视图")
        bar.addWidget(self.btn_reset)
        bar.addStretch(1)
        self.lbl = QtWidgets.QLabel("加载中…")
        self.lbl.setStyleSheet("color:#d7dde5;font-size:13px;")
        bar.addWidget(self.lbl)
        v.addLayout(bar)

        # 画布
        self.canvas = ChartCanvas(self)
        v.addWidget(self.canvas, 1)

        # 事件
        self.btn_load.clicked.connect(self.reload)
        self.tf_box.currentTextChanged.connect(self.on_tf)
        self.btn_h.clicked.connect(lambda: self.set_mode("h"))
        self.btn_t.clicked.connect(lambda: self.set_mode("trend"))
        self.btn_reset.clicked.connect(lambda: self.canvas.reset_view())
        QShortcut(QtGui.QKeySequence("Delete"), self, activated=self.delete_selected)
        QShortcut(QtGui.QKeySequence("Backspace"), self, activated=self.delete_selected)
        QShortcut(QtGui.QKeySequence("Ctrl+Z"), self, activated=self.undo)
        QShortcut(QtGui.QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QtGui.QKeySequence("Ctrl+Shift+Z"), self, activated=self.redo)
        self.chk_night.toggled.connect(lambda _: self.reload())
        self.sym_edit.returnPressed.connect(self.reload)

        # 定时器
        self.timer_q = QtCore.QTimer(self)   # 实时价 1.5s
        self.timer_q.timeout.connect(self.tick_quote)
        self.timer_b = QtCore.QTimer(self)   # K线 30s
        self.timer_b.timeout.connect(self.refresh_bars)
        self.timer_n = QtCore.QTimer(self)   # 夜盘逐笔累计 10s
        self.timer_n.timeout.connect(self.refresh_night)
        self.timer_q.start(max(300, self.quote_ms))
        self.timer_b.start(15000)
        self.timer_n.start(10000)

        QtCore.QTimer.singleShot(100, self.reload)

    # --- 状态 ---
    def set_status(self, msg):
        self.lbl.setText(msg)

    # --- 画线集合 ---
    def _key(self):
        return f"{self.symbol}|{self.tf}"

    def get_lines(self):
        return self._all_lines.get(self._key(), [])

    def add_line(self, ln):
        self.push_undo()
        self.canvas.selected = None
        self._all_lines.setdefault(self._key(), []).append(ln)
        _save_lines(self._all_lines)
        self.canvas.redraw()

    def clear_lines(self):
        self.push_undo()
        self._all_lines[self._key()] = []
        _save_lines(self._all_lines)
        self.canvas.redraw()
        self.set_status("已清除当前周期的线")

    def remove_line(self, idx):
        arr = self._all_lines.get(self._key(), [])
        if 0 <= idx < len(arr):
            self.push_undo()
            arr.pop(idx)
            _save_lines(self._all_lines)
            self.canvas.selected = None
            self.canvas.redraw()
            self.set_status("已删除该线（Ctrl+Z 可撤销）")
            return True
        return False

    def _snapshot(self):
        return json.loads(json.dumps(self._all_lines, ensure_ascii=False))

    def push_undo(self):
        self._undo_stack.append(self._snapshot())
        if len(self._undo_stack) > 200:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self):
        if not self._undo_stack:
            self.set_status("没有可撤销的操作")
            return
        self._redo_stack.append(self._snapshot())
        self._all_lines = self._undo_stack.pop()
        _save_lines(self._all_lines)
        self.canvas.selected = None
        self.canvas.redraw()
        self.set_status("已撤销 (Ctrl+Z)")

    def redo(self):
        if not self._redo_stack:
            self.set_status("没有可重做的操作")
            return
        self._undo_stack.append(self._snapshot())
        self._all_lines = self._redo_stack.pop()
        _save_lines(self._all_lines)
        self.canvas.selected = None
        self.canvas.redraw()
        self.set_status("已重做 (Ctrl+Y)")

    def delete_selected(self):
        idx = self.canvas.selected
        if idx is None:
            self.set_status("先点击选中一条线，再按 Delete")
            return
        self.remove_line(idx)

    def _sync_mode_buttons(self, mode):
        self.btn_h.setChecked(mode == "h")
        self.btn_t.setChecked(mode == "trend")

    def on_mode_cleared(self):
        self._sync_mode_buttons(None)

    def set_mode(self, mode):
        # 再点同一个按钮 → 关闭画线模式
        if self.canvas.mode == mode:
            mode = None
        self.canvas._clear_preview()
        self.canvas.mode = mode
        self.canvas.p1 = None
        self._sync_mode_buttons(mode)
        if mode == "h":
            self.set_status("水平线：点击图上放置（再点按钮或右键退出）")
        elif mode == "trend":
            self.set_status("趋势线：点第一个点→移动预览→点第二个点（再点按钮退出）")
        else:
            self.set_status("已退出画线模式（左键拖拽平移 / 滚轮缩放）")

    # --- 加载 ---
    def reload(self):
        self.symbol = self.sym_edit.text().strip().upper() or "SOXX"
        self.tf = self.tf_box.currentText()
        self.set_status(f"加载 {self.symbol} {self.tf} …")
        QtWidgets.QApplication.processEvents()
        night = self.chk_night.isChecked() and self.tf.endswith("m")
        bars = None
        for attempt in range(2):
            bars = night_bars(self.symbol, self.tf) if night else load_bars(self.symbol, self.tf)
            if bars is not None and not bars.empty:
                break
            self.set_status(f"加载失败，重试 {attempt + 1}/2 …")
            QtWidgets.QApplication.processEvents()
            time.sleep(0.5)
        used_cache = False
        if not night and (bars is None or bars.empty):
            bars = cache_load(self.symbol, self.tf)
            used_cache = bars is not None and not bars.empty
        if bars is None or bars.empty:
            self.set_status(f"{self.symbol} 数据获取失败（网络/接口），稍后自动重试")
            return
        self.bars = bars
        self.canvas.bars = bars
        self.canvas._user_view = False
        self.canvas.selected = None
        q = fetch_quote(self.symbol)
        self.last_price = q["price"] if q else float(bars["close"].iloc[-1])
        self.canvas.redraw()
        tag = "夜盘" if night else ""
        self.set_status(f"{self.symbol} {tag} {self.tf} · {len(bars)}根K"
                        + ("（缓存数据，联网后自动更新）" if used_cache else " · 已加载"))

    def on_tf(self, tf):
        self.tf = tf
        self.reload()

    def refresh_bars(self):
        if not self.chk_auto.isChecked():
            return
        try:
            night = self.chk_night.isChecked() and self.tf.endswith("m")
            bars = night_bars(self.symbol, self.tf) if night else load_bars(self.symbol, self.tf)
            if bars is not None and not bars.empty:
                self.bars = bars
                self.canvas.bars = bars
                self.canvas.redraw()
        except Exception:
            pass

    def refresh_night(self):
        if not self.chk_auto.isChecked() or not self.chk_night.isChecked():
            return
        if not self.tf.endswith("m"):
            return
        try:
            bars = night_bars(self.symbol, self.tf)
            if bars is not None and not bars.empty:
                self.bars = bars
                self.canvas.bars = bars
                self.canvas.redraw()
                self.set_status(f"{self.symbol} 夜盘 {self.tf} · {len(bars)}根K · 逐笔累计中")
        except Exception:
            pass

    def _fetch_okx_async(self):
        with self._okx_lock:
            if self._okx_busy:
                return
            self._okx_busy = True
        inst = self.okx_inst
        bases = self.okx_bases

        def work():
            try:
                q = fetch_quote_okx(inst, bases=bases)
                if q is not None:
                    self._okx_q = (time.time(), q)
            except Exception:
                pass
            finally:
                with self._okx_lock:
                    self._okx_busy = False

        threading.Thread(target=work, daemon=True).start()

    def tick_quote(self):
        if not self.chk_auto.isChecked():
            return
        q = None
        if getattr(self, "source", "em") == "okx":
            node = self._okx_q
            if node is not None and time.time() - node[0] < 5:
                q = node[1]
            elif time.time() >= self._okx_next_try:
                self._okx_next_try = time.time() + 5
                self._fetch_okx_async()
        if not q:
            q = fetch_quote(self.symbol)
        if not q:
            return
        changed = (q["price"] != self.last_price)
        self.last_price = q["price"]
        # 用实时价更新最新一根K线（close/high/low），实现秒级蜡烛更新
        if changed and self.bars is not None and len(self.bars):
            try:
                i = self.bars.index[-1]
                p = float(q["price"])
                hi = float(self.bars.at[i, "high"])
                lo = float(self.bars.at[i, "low"])
                self.bars.at[i, "close"] = p
                self.bars.at[i, "high"] = max(hi, p)
                self.bars.at[i, "low"] = min(lo, p)
                self.canvas.bars = self.bars
                self.canvas.redraw()
            except Exception:
                self.canvas.update_live(q["price"], q["pct"])
        else:
            self.canvas.update_live(q["price"], q["pct"])
        ts = time.strftime("%H:%M:%S")
        src = "OKX" if q.get("name") == "OKX" else "东财"
        self.set_status(f"{q['symbol']} {q['name'][:18]}  现价 {q['price']:.2f}  "
                        f"{q['pct']:+.2f}%  更新 {ts}（{src}）")


def main():
    ap = argparse.ArgumentParser(description="US ETF 分时/多周期看盘器")
    ap.add_argument("--symbol", default="SOXX")
    ap.add_argument("--interval", type=int, default=1000, help="报价刷新毫秒（默认1000）")
    ap.add_argument("--source", choices=["em", "okx"], default="em",
                    help="实时价来源：em=东方财富（默认），okx=欧易")
    ap.add_argument("--okx-inst", default=None, help="OKX instId，如 SOXX-USDT / SPYx-USDT")
    ap.add_argument("--okx-base", default=None, help="OKX API 基址（默认 https://www.okx.com，可填镜像）")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow(args.symbol, quote_ms=args.interval, source=args.source, okx_inst=args.okx_inst)
    if args.okx_base:
        w.okx_bases = [args.okx_base.rstrip("/")]
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
