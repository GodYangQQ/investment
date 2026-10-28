#!/usr/bin/env python3
"""
均线策略实验：到底哪条均线最值得参考？
=======================================
条件 X（大级别趋势过滤）：MA_X 向上（默认 X=45，斜率看近 k 日）
条件 Y（交易信号）：收盘上穿 MA_Y → 买入；收盘下穿 MA_Y → 卖出
对比：同一窗口「开盘买入、期末收盘卖出」的买入持有

用法:
    python experiments/ma_backtest.py --ticker SOXX                # 单组：X45 / Y21
    python experiments/ma_backtest.py --ticker SOXX --x 45 --y 21
    python experiments/ma_backtest.py --ticker SOXX --grid         # 网格搜索
    python experiments/ma_backtest.py --ticker SOXX,SOXL,SPY --grid
    python experiments/ma_backtest.py --ticker SOXX --x 45 --y 21 --plot

说明:
    - 信号在收盘计算，次日开盘成交，避免未来函数。
    - 默认只做多；空仓时收益为 0。
    - 1 年 = 252 个交易日；默认在最近 5 年里滚动测试所有 1 年窗口。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from core.datasets.us_data import fetch_daily_kline  # noqa: E402

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
WINDOW = 252
STEP = 21
FEE = 0.0005
SLOPE_K = 5


def load(ticker: str, days: int = 1600) -> pd.DataFrame:
    df = fetch_daily_kline(ticker, days=days).copy()
    df = df.sort_values("date").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "close"]).reset_index(drop=True)
    return df


def add_mas(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    df = df.copy()
    for p in periods:
        df[f"ma{p}"] = df["close"].rolling(p).mean()
    return df


@dataclass
class Result:
    total: float
    bench: float
    excess: float
    cagr: float
    maxdd: float
    sharpe: float
    trades: int
    winrate: float
    exposure: float
    bench_maxdd: float
    equity: list[float]


def run_strategy(df: pd.DataFrame, x: int, y: int, i0: int, i1: int,
                 slope_k: int = SLOPE_K, exit_on_x_down: bool = True,
                 fee: float = FEE, use_x: bool = True, buffer: float = 0.0,
                 slope_y: bool = False, vol_filter: bool = False) -> Result:
    close = df["close"].values
    open_ = df["open"].values
    ma_x = df[f"ma{x}"].values if use_x else np.ones(len(df))
    ma_y = df[f"ma{y}"].values
    vol = df["volume"].values
    vol_ma20 = df["volume"].rolling(20).mean().values

    value = 1.0
    pos = 0
    pending = None
    entry_px = 0.0
    entry_i = -1
    trades_pnl = []
    eq = []
    expo = 0

    for t in range(i0, i1 + 1):
        if t > i0:
            if pending == "sell" and pos == 1:
                value *= open_[t] / close[t - 1]
                value *= (1 - fee)
                pos = 0
                if entry_px:
                    trades_pnl.append(open_[t] / entry_px - 1)
                entry_px = 0.0
            elif pending == "buy" and pos == 0:
                value *= (1 - fee)
                value *= close[t] / open_[t]
                pos = 1
                entry_px = open_[t]
                entry_i = t
            else:
                if pos == 1:
                    value *= close[t] / close[t - 1]
            pending = None
        eq.append(value)
        if pos == 1:
            expo += 1

        # 收盘信号 → 次日开盘
        if t >= max((x if use_x else 0), y, slope_k) + 1 and t < i1:
            if np.isnan(ma_y[t]) or (use_x and (np.isnan(ma_x[t]) or np.isnan(ma_x[t - slope_k]))):
                continue
            x_ok = (ma_x[t] > ma_x[t - slope_k]) if use_x else True
            up, up_p = ma_y[t] * (1 + buffer), ma_y[t - 1] * (1 + buffer)
            dn, dn_p = ma_y[t] * (1 - buffer), ma_y[t - 1] * (1 - buffer)
            cross_up = close[t - 1] <= up_p and close[t] > up
            cross_down = close[t - 1] >= dn_p and close[t] < dn
            y_ok = (not slope_y) or (ma_y[t] > ma_y[t - slope_k])
            v_ok = (not vol_filter) or (vol_ma20[t] > 0 and vol[t] > 1.2 * vol_ma20[t])
            if pos == 0 and pending is None and x_ok and cross_up and y_ok and v_ok:
                pending = "buy"
            elif pos == 1 and pending is None and (cross_down or (exit_on_x_down and not x_ok)):
                pending = "sell"

    # 期末强制平仓（按收盘）
    if pos == 1:
        value *= (1 - fee)
        if entry_px:
            trades_pnl.append(close[i1] / entry_px - 1)
        eq[-1] = value
        pos = 0

    bench = (close[i1] / open_[i0]) * (1 - fee) ** 2
    bench_eq = close[i0:i1 + 1] / open_[i0]
    bench_maxdd = float(np.min(bench_eq / np.maximum.accumulate(bench_eq) - 1)) if len(bench_eq) else 0.0
    eq = np.array(eq, dtype=float)
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([])
    maxdd = float(np.min(eq / np.maximum.accumulate(eq) - 1)) if len(eq) else 0.0
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if len(rets) > 1 and np.std(rets) > 0 else 0.0
    years = max((i1 - i0 + 1) / 252.0, 1e-9)
    cagr = float(value ** (1 / years) - 1) if value > 0 else -1.0
    wins = sum(1 for p in trades_pnl if p > 0)
    winrate = wins / len(trades_pnl) if trades_pnl else 0.0
    return Result(
        total=float(value - 1), bench=float(bench - 1), excess=float(value - bench),
        cagr=cagr, maxdd=maxdd, sharpe=sharpe, trades=len(trades_pnl),
        winrate=winrate, exposure=expo / max(len(eq), 1),
        bench_maxdd=bench_maxdd, equity=eq.tolist(),
    )


def windows(n: int, years: int = 5, window: int = WINDOW, step: int = STEP) -> list[tuple[int, int]]:
    end = n - 1
    start = max(window + 5, n - years * 252)
    out = []
    s = start
    while s + window <= end:
        out.append((s, s + window - 1))
        s += step
    if not out:
        out = [(max(window + 5, 0), end)]
    return out


def evaluate(df: pd.DataFrame, x: int, y: int, wins: list[tuple[int, int]],
             exit_on_x_down: bool = True, fee: float = FEE, use_x: bool = True,
             buffer: float = 0.0, slope_y: bool = False, vol_filter: bool = False) -> dict:
    rows = [run_strategy(df, x, y, a, b, exit_on_x_down=exit_on_x_down, fee=fee, use_x=use_x,
                         buffer=buffer, slope_y=slope_y, vol_filter=vol_filter)
            for a, b in wins]
    excess = np.array([r.excess for r in rows])
    sharpe = np.array([r.sharpe for r in rows])
    dd = np.array([r.maxdd for r in rows])
    beat = np.mean(excess > 0)
    return {
        "x": x, "y": y,
        "mean_excess": excess.mean(),
        "median_excess": float(np.median(excess)),
        "beat_rate": beat,
        "mean_sharpe": sharpe.mean(),
        "mean_total": np.mean([r.total for r in rows]),
        "mean_bench": np.mean([r.bench for r in rows]),
        "worst_dd": dd.min(),
        "mean_dd": dd.mean(),
        "mean_trades": np.mean([r.trades for r in rows]),
        "mean_winrate": np.mean([r.winrate for r in rows]),
        "mean_exposure": np.mean([r.exposure for r in rows]),
        "worst_bench_dd": min(r.bench_maxdd for r in rows),
        "mean_bench_dd": np.mean([r.bench_maxdd for r in rows]),
        "windows": len(rows),
    }


def print_single(df: pd.DataFrame, ticker: str, x: int, y: int, years: int,
                 exit_on_x_down: bool, use_x: bool = True, buffer: float = 0.0,
                 slope_y: bool = False, vol_filter: bool = False) -> None:
    wins = windows(len(df), years)
    # 最近一个 1 年窗口
    a, b = wins[-1]
    r = run_strategy(df, x, y, a, b, exit_on_x_down=exit_on_x_down, use_x=use_x,
                     buffer=buffer, slope_y=slope_y, vol_filter=vol_filter)
    xlab = f"MA{x}" if use_x else "OFF"
    print(f"\n===== {ticker}  X={xlab}  Y=MA{y}  最近1年 ({df['date'].iloc[a].date()} ~ {df['date'].iloc[b].date()}) =====")
    print(f"  策略收益 : {r.total*100:+.1f}%   年化 {r.cagr*100:+.1f}%   最大回撤 {r.maxdd*100:.1f}%   夏普 {r.sharpe:.2f}")
    print(f"  买入持有 : {r.bench*100:+.1f}%   超额 {r.excess*100:+.1f}%   基准回撤 {r.bench_maxdd*100:.1f}%")
    print(f"  交易 {r.trades} 次   胜率 {r.winrate*100:.0f}%   在场 {r.exposure*100:.0f}%")

    agg = evaluate(df, x, y, wins, exit_on_x_down, use_x=use_x,
                   buffer=buffer, slope_y=slope_y, vol_filter=vol_filter)
    print(f"\n  近{agg['windows']}个滚动1年窗口平均：超额 {agg['mean_excess']*100:+.1f}%  "
          f"跑赢概率 {agg['beat_rate']*100:.0f}%  夏普 {agg['mean_sharpe']:.2f}  "
          f"策略最差回撤 {agg['worst_dd']*100:.1f}%  基准最差回撤 {agg['worst_bench_dd']*100:.1f}%  "
          f"平均交易 {agg['mean_trades']:.1f} 次")


def grid_search(df: pd.DataFrame, ticker: str, xs: list[int], ys: list[int],
                years: int, exit_on_x_down: bool, use_x: bool = True,
                buffer: float = 0.0, slope_y: bool = False, vol_filter: bool = False) -> pd.DataFrame:
    wins = windows(len(df), years)
    print(f"\n[{ticker}] 网格搜索：X={xs}  Y={ys[0]}..{ys[-1]}  窗口={len(wins)} 个滚动1年")
    rows = []
    total = len(xs) * len(ys)
    for i, x in enumerate(xs):
        for y in ys:
            rows.append(evaluate(df, x, y, wins, exit_on_x_down, use_x=use_x,
                                 buffer=buffer, slope_y=slope_y, vol_filter=vol_filter))
        print(f"  X=MA{x} 完成 ({i+1}/{len(xs)})", flush=True)
    res = pd.DataFrame(rows)
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, f"grid_{ticker}.csv")
    res.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  已保存: {path}")
    return res


def show_top(res: pd.DataFrame, ticker: str, n: int = 15) -> None:
    cols = ["x", "y", "mean_excess", "beat_rate", "mean_sharpe", "mean_total",
            "mean_bench", "worst_dd", "mean_trades", "mean_winrate", "mean_exposure"]
    for key, label in [("mean_excess", "按平均超额"), ("mean_sharpe", "按夏普")]:
        print(f"\n--- {ticker} {label} Top{n} ---")
        t = res.sort_values(key, ascending=False).head(n)
        for _, r in t.iterrows():
            xl = f"MA{int(r.x)}" if int(r.x) > 0 else "OFF"
            print(f"  X={xl:<5} Y=MA{int(r.y):<3} 超额 {r.mean_excess*100:+6.1f}%  "
                  f"跑赢 {r.beat_rate*100:3.0f}%  夏普 {r.mean_sharpe:4.2f}  "
                  f"策略 {r.mean_total*100:+6.1f}%  基准 {r.mean_bench*100:+6.1f}%  "
                  f"最差回撤 {r.worst_dd*100:5.1f}%  交易 {r.mean_trades:4.1f}")


def plot_equity(df: pd.DataFrame, ticker: str, x: int, y: int, years: int) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    wins = windows(len(df), years)
    a, b = wins[-1]
    r = run_strategy(df, x, y, a, b)
    dates = df["date"].iloc[a:b + 1]
    bench = df["close"].iloc[a:b + 1].values / df["open"].iloc[a] * (1 - FEE)
    fig, ax = plt.subplots(figsize=(11, 5), dpi=110)
    ax.plot(dates, r.equity, label=f"MA{x}/{y} strategy", color="#4d9fff", lw=1.6)
    ax.plot(dates, bench, label="buy & hold", color="#94a0ad", lw=1.2, ls="--")
    ax.set_title(f"{ticker}  X=MA{x} up  Y=MA{y} cross  ({dates.iloc[0].date()} ~ {dates.iloc[-1].date()})")
    ax.grid(alpha=0.25)
    ax.legend()
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, f"equity_{ticker}_X{x}_Y{y}.png")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="均线策略回测：哪条均线最值得参考")
    ap.add_argument("--ticker", default="SOXX", help="逗号分隔，如 SOXX,SOXL,SPY")
    ap.add_argument("--x", type=int, default=45, help="趋势过滤均线（默认45）")
    ap.add_argument("--y", type=int, default=21, help="交易信号均线（默认21）")
    ap.add_argument("--years", type=int, default=5, help="在最近N年内滚动测试（默认5）")
    ap.add_argument("--grid", action="store_true", help="网格搜索")
    ap.add_argument("--xs", default="20,30,45,60,90,120,150,200")
    ap.add_argument("--ys", default="5-45", help="如 5-45 或 5,10,21,45")
    ap.add_argument("--no-exit-x", action="store_true", help="X 转下时不平仓（只靠跌破MA_Y）")
    ap.add_argument("--no-x", action="store_true", help="去掉条件X，只保留条件Y（均线突破）")
    ap.add_argument("--buffer", type=float, default=0.0, help="突破缓冲带，如 0.01=1%%")
    ap.add_argument("--slope-y", action="store_true", help="入场还要求 MA_Y 本身向上")
    ap.add_argument("--vol-filter", action="store_true", help="入场还要求成交量 > 20日均量 1.2 倍")
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--days", type=int, default=1600)
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.ticker.split(",") if t.strip()]
    xs = [int(v) for v in args.xs.split(",") if v.strip()]
    if "-" in args.ys:
        a, b = args.ys.split("-")
        ys = list(range(int(a), int(b) + 1))
    else:
        ys = [int(v) for v in args.ys.split(",") if v.strip()]

    exit_on_x_down = not args.no_exit_x
    use_x = not args.no_x
    if use_x is False:
        xs = [0]
        if args.x == 45:
            args.x = 0
    all_res = []
    for ticker in tickers:
        try:
            df = load(ticker, args.days)
        except Exception as e:
            print(f"[{ticker}] 数据失败: {e}")
            continue
        need = max(max(xs), max(ys)) + 10
        if len(df) < WINDOW + need:
            print(f"[{ticker}] 数据不足: {len(df)} < {WINDOW + need}")
            continue
        periods = sorted({p for p in (xs + ys + [args.x, args.y]) if p > 0})
        df = add_mas(df, periods)

        if args.grid:
            res = grid_search(df, ticker, xs, ys, args.years, exit_on_x_down, use_x=use_x,
                              buffer=args.buffer, slope_y=args.slope_y, vol_filter=args.vol_filter)
            all_res.append(res)
            show_top(res, ticker)
        else:
            print_single(df, ticker, args.x, args.y, args.years, exit_on_x_down, use_x=use_x,
                         buffer=args.buffer, slope_y=args.slope_y, vol_filter=args.vol_filter)
            if args.plot:
                print("  图已保存:", plot_equity(df, ticker, args.x, args.y, args.years))

    if all_res:
        combined = pd.concat(all_res, ignore_index=True)
        os.makedirs(OUTDIR, exist_ok=True)
        path = os.path.join(OUTDIR, "grid_all_tickers.csv")
        combined.to_csv(path, index=False, encoding="utf-8-sig")
        # 跨标的综合（按 ticker 分组后再平均）
        agg = (combined.groupby(["x", "y"]).agg(
            mean_excess=("mean_excess", "mean"),
            mean_sharpe=("mean_sharpe", "mean"),
            beat_rate=("beat_rate", "mean"),
            worst_dd=("worst_dd", "min"),
            mean_trades=("mean_trades", "mean"),
        ).reset_index())
        print("\n===== 跨标的综合 Top15（平均超额）=====")
        t = agg.sort_values("mean_excess", ascending=False).head(15)
        for _, r in t.iterrows():
            xl = f"MA{int(r.x)}" if int(r.x) > 0 else "OFF"
            print(f"  X={xl:<5} Y=MA{int(r.y):<3} 平均超额 {r.mean_excess*100:+6.1f}%  "
                  f"跑赢 {r.beat_rate*100:3.0f}%  夏普 {r.mean_sharpe:4.2f}  最差回撤 {r.worst_dd*100:5.1f}%")
        print("\n===== 跨标的综合 Top10（平均夏普）=====")
        t = agg.sort_values("mean_sharpe", ascending=False).head(10)
        for _, r in t.iterrows():
            xl = f"MA{int(r.x)}" if int(r.x) > 0 else "OFF"
            print(f"  X={xl:<5} Y=MA{int(r.y):<3} 夏普 {r.mean_sharpe:4.2f}  "
                  f"平均超额 {r.mean_excess*100:+6.1f}%  最差回撤 {r.worst_dd*100:5.1f}%")
        print(f"\n  综合结果已保存: {path}")


if __name__ == "__main__":
    main()
