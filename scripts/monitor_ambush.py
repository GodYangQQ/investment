#!/usr/bin/env python3
"""
策略埋点监控：每5秒拉取实时价格，按距最近买点百分比排序。
自动读取 output/ 目录下最新的 strategy_YYYYMMDD.csv。
用法: python monitor_ambush.py [csv文件路径]
"""

import csv
import sys
import time
import os
import glob
import requests
import re
import unicodedata
from datetime import datetime

# 确保可以找到项目根目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")

import numpy as np


def find_latest_strategy() -> str | None:
    # 优先在 output/ 下找，再在当前目录找
    for search_dir in [OUTPUT_DIR, os.getcwd()]:
        pattern = os.path.join(search_dir, "strategy_*.csv")
        files = glob.glob(pattern)
        if files:
            files.sort()
            return files[-1]
    return None


def load_strategy(csv_path: str) -> list[dict]:
    with open(csv_path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def display_width(s: str) -> int:
    """Calculate display width: CJK chars = 2, ASCII = 1."""
    w = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def pad_str(s: str, width: int, align: str = "<") -> str:
    """Pad string to given display width."""
    dw = display_width(s)
    if dw >= width:
        return s
    padding = width - dw
    if align == "<":
        return s + " " * padding
    elif align == ">":
        return " " * padding + s
    else:
        left = padding // 2
        right = padding - left
        return " " * left + s + " " * right


def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """Fetch prices + hi/lo for all codes."""
    quotes = {}
    for code in codes:
        prefix = "sh" if code.startswith("6") else "sz"
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={prefix}{code}", timeout=3)
            r.encoding = "gbk"
            m = re.search(r'="(.+?)"', r.text)
            if m:
                f = m.group(1).split("~")
                price = float(f[3]) if len(f) > 3 and f[3] else 0
                prev_close = float(f[4]) if len(f) > 4 and f[4] else 0
                open_today = float(f[5]) if len(f) > 5 and f[5] else 0
                hi = float(f[41]) if len(f) > 41 and f[41] else 0
                lo = float(f[42]) if len(f) > 42 and f[42] else 0
                if price > 0:
                    quotes[code] = {"price": price, "prev": prev_close, "open": open_today, "hi": hi, "lo": lo}
        except Exception:
            pass
        time.sleep(0.06)
    return quotes


def fetch_kline(code: str, days: int = 30) -> list[float]:
    """Fetch daily closing prices for RSI calculation. Returns list of closes (oldest first)."""
    prefix = "sh" if code.startswith("6") else "sz"
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{days},qfq"
        r = requests.get(url, timeout=10)
        data = r.json()
        key = f"{prefix}{code}"
        klines = data.get("data", {}).get(key, {}).get("qfqday", [])
        if not klines:
            klines = data.get("data", {}).get(key, {}).get("day", [])
        if not klines:
            return []
        closes = [float(k[2]) for k in klines if len(k) >= 6]
        return closes
    except Exception:
        return []


def _rsi_from_closes(arr: np.ndarray, period: int = 14) -> float:
    """Compute RSI at the last point of the given close array."""
    diffs = np.diff(arr[-period - 1:])
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    if avg_loss == 0:
        return 100.0
    for i in range(period, len(diffs)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
    return round(float(100.0 - (100.0 / (1.0 + rs))), 1)


def calc_rsi_with_trend(closes: list[float], period: int = 14) -> tuple[float | None, float, float, str]:
    """Calculate RSI(14) with 5d slope + curvature (5d-20d). Returns (rsi, slope_5d, curvature, arrow_5d).
    curvature > 0 → RSI加速变化(上升加速或下跌加速)
    curvature < 0 → RSI趋势衰竭(涨速放缓或跌速收敛)"""
    need = period + 21
    if len(closes) < need + 1:
        return None, 0.0, 0.0, "—"
    arr = np.array(closes, dtype=float)
    n = len(arr)
    rsi_history = []
    for i in range(period, n):
        window = arr[i - period:i + 1]
        rsi_history.append(_rsi_from_closes(window, period))
    if len(rsi_history) < 21:
        return rsi_history[-1], 0.0, 0.0, "—"

    def _slope(days: int) -> float:
        if len(rsi_history) < days + 1:
            return 0.0
        return round((rsi_history[-1] - rsi_history[-days - 1]) / days, 1)

    current = rsi_history[-1]
    s5 = _slope(5)
    s20 = _slope(20)
    curv = round(s5 - s20, 1)
    if s5 > 0.2:
        arrow = "↑"
    elif s5 < -0.2:
        arrow = "↓"
    else:
        arrow = "→"
    return current, s5, curv, arrow


def rsi_display(rsi: float | None, arrow: str, s5: float, curv: float) -> str:
    """Format RSI: value + 5d slope + curvature. 15 chars. curv>0=加速, <0=衰竭."""
    if rsi is None:
        return "      —          "
    sign = "+" if curv >= 0 else ""
    return f"{rsi:5.1f} {arrow}{s5:+.1f} {sign}{curv:.1f} "


def parse_price(val: str) -> float | None:
    v = val.strip()
    if not v or v == "无":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def main():
    csv_file = sys.argv[1] if len(sys.argv) > 1 else find_latest_strategy()
    if not csv_file:
        print("未找到 strategy_*.csv")
        sys.exit(1)
    if not os.path.exists(csv_file):
        print(f"CSV not found: {csv_file}")
        sys.exit(1)

    stocks = load_strategy(csv_file)
    codes = [s["代码"] for s in stocks if s.get("代码")]

    # Pre-load historical data for RSI (cached across loops)
    rsi_cache: dict[str, tuple[float | None, float, float, str]] = {}
    for code in codes:
        closes = fetch_kline(code, days=60)
        rsi_cache[code] = calc_rsi_with_trend(closes)

    try:
        while True:
            quotes = fetch_quotes(codes)
            results = []
            stock_quotes = {}

            for s in stocks:
                code = s["代码"]
                name = s["名称"]
                q = quotes.get(code)
                if q is None:
                    continue
                price = q["price"]
                stock_quotes[code] = q

                breakout = parse_price(s["突破买入"])
                bottom = parse_price(s["触底买入"])

                best_buy = None
                best_dist = float("inf")
                best_type = ""

                for bp, bt in [(breakout, "突破"), (bottom, "触底")]:
                    if bp is None:
                        continue
                    dist = abs((bp - price) / price * 100)
                    if dist < best_dist:
                        best_dist = dist
                        best_buy = bp
                        best_type = bt

                if best_buy is None:
                    continue

                pct = (best_buy - price) / price * 100
                kb = s.get("留底仓", "否")
                kb_short = "留底" if kb and not kb.startswith("否") and kb != "-" else "-"

                results.append({
                    "code": code, "name": name, "price": price,
                    "bp": best_buy, "bt": best_type,
                    "pct": pct, "dist": best_dist,
                    "kb": kb_short,
                    "rsi": rsi_cache.get(code, (None, 0, "—")),
                })

            results.sort(key=lambda x: x["dist"])

            # ---- alerts ----
            alerts = []
            for s in stocks:
                code = s["代码"]
                name = s["名称"]
                q = quotes.get(code)
                if q is None:
                    continue
                price = q["price"]
                for key, label in [("半仓止盈", "半止盈"), ("全仓止盈", "全止盈"),
                                   ("半仓止损", "半止损"), ("全仓止损", "全止损")]:
                    tp = parse_price(s[key])
                    if tp is None:
                        continue
                    pct = (tp - price) / price * 100
                    abs_pct = abs(pct)
                    if abs_pct < 8:
                        alerts.append({
                            "code": code, "name": name, "price": price,
                            "tp": tp, "label": label, "pct": pct, "dist": abs_pct,
                        })
            alerts.sort(key=lambda x: x["dist"])

            os.system("clear" if os.name != "nt" else "cls")
            now = datetime.now().strftime("%H:%M:%S")

            # Get terminal width (fallback to 120)
            try:
                tw = os.get_terminal_size().columns - 1
            except OSError:
                tw = 120
            # Column display widths: rk=6, code=10, name=10, prev=9, open=9, hi=9, lo=9, sep=3, price=9, bp=9, bt=7, sep2=3, pct=9, kb=5, rsi=11
            cols = [6, 10, 10, 9, 9, 9, 9, 3, 9, 9, 7, 3, 9, 5, 15]
            total_min = sum(cols)
            if tw > total_min:
                extra = tw - total_min
                cols[1]  += extra * 3 // 27
                cols[2]  += extra * 4 // 27
                cols[0]  += extra * 1 // 27
                cols[3]  += extra * 1 // 27
                cols[4]  += extra * 1 // 27
                cols[5]  += extra * 1 // 27
                cols[6]  += extra * 1 // 27
                cols[8]  += extra * 2 // 27
                cols[9]  += extra * 2 // 27
                cols[10] += extra * 1 // 27
                cols[12] += extra * 2 // 27
                cols[13] += extra * 1 // 27
                cols[14] += extra * 2 // 27
            c = cols
            line_w = sum(c)

            def cell(s, w, align="<"):
                """Format a cell with CJK-aware padding to given display width."""
                return pad_str(str(s), w, align)

            print(f"  策略监控 | {now} | {csv_file}")
            print("=" * line_w)
            print("  ▶ 买点接近度 (距触发最近)")
            print("-" * line_w)

            hdr = (cell("", c[0]) + cell("代码", c[1]) + cell("名称", c[2]) +
                   cell("昨收", c[3], ">") + cell("今开", c[4], ">") +
                   cell("今高", c[5], ">") + cell("今低", c[6], ">") +
                   cell("│", c[7]) +
                   cell("现价", c[8], ">") + cell("目标", c[9], ">") + cell("类型", c[10]) +
                    cell("│", c[11]) +
                    cell("距离", c[12], ">") + cell("留底", c[13]) + cell("RSI 5dΔ 曲率", c[14]))
            print(hdr)
            print("-" * line_w)

            near = 0
            for rank, r in enumerate(results, 1):
                dist = r["dist"]
                m = "🔥" if dist < 3 else ("👀" if dist < 6 else "  ")
                if dist < 6:
                    near += 1
                q = stock_quotes.get(r["code"], {})
                prev = q.get("prev", 0)
                opn = q.get("open", 0)
                hi = q.get("hi", 0)
                lo = q.get("lo", 0)

                rsi_v, rsi_s5, rsi_curv, rsi_arrow = r["rsi"]
                print(
                    cell(f"{m}{rank:<3}", c[0]) +
                    cell(r['code'], c[1]) + cell(r['name'], c[2]) +
                    cell(f"{prev:.2f}", c[3], ">") + cell(f"{opn:.2f}", c[4], ">") +
                    cell(f"{hi:.2f}", c[5], ">") + cell(f"{lo:.2f}", c[6], ">") +
                    cell("│", c[7]) +
                    cell(f"{r['price']:.2f}", c[8], ">") + cell(f"{r['bp']:.2f}", c[9], ">") +
                    cell(r['bt'], c[10]) +
                    cell("│", c[11]) +
                    cell(f"{r['pct']:+.1f}%", c[12], ">") + cell(r['kb'], c[13]) +
                    cell(rsi_display(rsi_v, rsi_arrow, rsi_s5, rsi_curv), c[14])
                )

            print("-" * line_w)

            if alerts:
                print(f"  ⚠️  止盈/止损预警 (距触发价<8%)")
                print("-" * line_w)
                hdr2 = (cell("", c[0]) +
                        cell("代码", c[1]) + cell("名称", c[2]) +
                        cell("昨收", c[3], ">") + cell("今开", c[4], ">") +
                        cell("今高", c[5], ">") + cell("今低", c[6], ">") +
                        cell("│", c[7]) +
                        cell("现价", c[8], ">") + cell("触发价", c[9], ">") + cell("类型", c[10]) +
                        cell("│", c[11]) +
                   cell("距离", c[12], ">") + cell("留底", c[13]) + cell("RSI 5dΔ 曲率", c[14]))
                print(hdr2)
                print("-" * line_w)
                for a in alerts:
                    is_stop = "止损" in a["label"]
                    is_near = a["dist"] < 3
                    icon = "🛑" if (is_stop and is_near) else ("⚠️" if is_stop else "💡")
                    q = stock_quotes.get(a["code"], {})
                    prev = q.get("prev", 0)
                    opn = q.get("open", 0)
                    hi = q.get("hi", 0)
                    lo = q.get("lo", 0)
                    rk = f"{icon} "
                    rsi_v, rsi_s5, rsi_curv, rsi_arrow = rsi_cache.get(a["code"], (None, 0.0, 0.0, "—"))
                    print(
                        cell(rk, c[0]) +
                        cell(a['code'], c[1]) + cell(a['name'], c[2]) +
                        cell(f"{prev:.2f}", c[3], ">") + cell(f"{opn:.2f}", c[4], ">") +
                        cell(f"{hi:.2f}", c[5], ">") + cell(f"{lo:.2f}", c[6], ">") +
                        cell("│", c[7]) +
                        cell(f"{a['price']:.2f}", c[8], ">") + cell(f"{a['tp']:.2f}", c[9], ">") +
                        cell(a['label'], c[10]) +
                        cell("│", c[11]) +
                        cell(f"{a['pct']:+.1f}%", c[12], ">") + cell("-", c[13]) +
                        cell(rsi_display(rsi_v, rsi_arrow, rsi_s5, rsi_curv), c[14])
                    )
                print("-" * line_w)

            print(f"  买点<3%🔥: {near}只 | 预警⚠️: {len(alerts)}只 | 共 {len(results)}只")
            # RSI extreme summary
            rsi_warnings = []
            for r in results:
                rsi_v, rsi_s5, rsi_curv, rsi_arrow = r["rsi"]
                if rsi_v is None:
                    continue
                curv_str = f"曲{'+' if rsi_curv>=0 else ''}{rsi_curv:.1f}"
                if rsi_v >= 80:
                    rsi_warnings.append(f"{r['name']} {rsi_v:.0f}{rsi_arrow}5d{rsi_s5:+.0f}/{curv_str}")
                elif rsi_v <= 20:
                    rsi_warnings.append(f"{r['name']} {rsi_v:.0f}{rsi_arrow}5d{rsi_s5:+.0f}/{curv_str}")
                elif rsi_v >= 70 and rsi_arrow == "↓":
                    rsi_warnings.append(f"{r['name']} {rsi_v:.0f}{rsi_arrow}5d{rsi_s5:+.0f}/{curv_str}转弱")
                elif rsi_v <= 30 and rsi_arrow == "↑":
                    rsi_warnings.append(f"{r['name']} {rsi_v:.0f}{rsi_arrow}5d{rsi_s5:+.0f}/{curv_str}转强")
            if rsi_warnings:
                print(f"  RSI: {' | '.join(rsi_warnings)}")
            print("  [Ctrl+C] 退出")

            time.sleep(5)

    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
