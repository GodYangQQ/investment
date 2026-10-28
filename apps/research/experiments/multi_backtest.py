#!/usr/bin/env python3
"""
多资产量化策略引擎：寻找能跑赢买入持有的策略
============================================
结论背景：单资产纯择时（多/空仓）在长牛上几乎不可能跑赢持有。
能跑赢的路径：加杠杆、跨资产轮动、控波动。本引擎统一回测这几类：

  momentum_rotation : 横截面动量轮动（每月选前 K 个最强资产持有）
  dual_momentum     : 双动量（Antonacci：股票动量 > 债券动量才持有股票，否则债券/现金）
  trend_leverage    : 趋势 + 杠杆（基础资产站上 MA 持 3x，跌破转现金/1x）
  vol_target        : 波动率目标（逆波动率缩放仓位）

执行口径与 ma_backtest.py 一致：
  - 信号在收盘 t 计算，次日 t+1 开盘成交（无未来函数）
  - 换手按 |Δ权重| 收手续费 0.05%
  - 只做多；滚动 1 年窗口对比同一窗口「基准资产买入持有」

用法:
    python experiments/multi_backtest.py --bench SOXX
    python experiments/multi_backtest.py --bench SOXX --search   # 全策略参数扫描
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
WINDOW, STEP, FEE = 252, 21, 0.0005

# 可用的资产池（已探测过 Nasdaq API 可拿）
UNIVERSE = {
    "equity":  ["SOXX", "QQQ", "SPY", "SMH", "XLK", "VGT", "XSD", "PSI"],
    "bond":    ["TLT", "IEF", "SHY", "LQD", "HYG", "BND", "AGG", "TIP"],
    "lev3":    ["SOXL", "TQQQ", "UPRO"],
    "lev_neg": ["SOXS", "SQQQ", "SPXU", "TBT", "TMV"],
    "lev2":    ["UBT"],
}

BASE_TO_LEV = {"SOXX": "SOXL", "QQQ": "TQQQ", "SPY": "UPRO"}
BASE_TO_INV = {"SOXX": "SOXS", "QQQ": "SQQQ", "SPY": "SPXU"}


# ---------------- 数据对齐 ----------------
def load_aligned(tickers: list[str], days: int = 1600) -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    closes, opens = {}, {}
    for t in tickers:
        df = fetch_daily_kline(t, days=days).copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["open"] = pd.to_numeric(df["open"], errors="coerce")
        closes[t] = df["close"]
        opens[t] = df["open"]
    close_mat = pd.DataFrame(closes).dropna(how="all")
    open_mat = pd.DataFrame(opens).dropna(how="all")
    idx = close_mat.index.intersection(open_mat.index)
    close_mat = close_mat.loc[idx].ffill()
    open_mat = open_mat.loc[idx].ffill()
    # 去掉前端 NaN（保证动量窗口干净）
    first_valid = close_mat.notna().values.argmax(axis=0)
    start = int(first_valid.max())
    close_mat = close_mat.iloc[start:].reset_index(drop=True)
    open_mat = open_mat.iloc[start:].reset_index(drop=True)
    dates = idx[start:]
    return close_mat, open_mat, dates


def ma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


# ---------------- 目标权重函数（t 收盘决策 → t+1 开盘执行） ----------------
def targets_momentum(close_mat: pd.DataFrame, top_k: int = 2, mom: int = 126,
                     abs_filter: bool = False, safe: str | None = None) -> list[np.ndarray]:
    """每月末按过去 mom 日动量排序，持前 top_k 个等权；可选绝对动量过滤（动量<=0 全现金）。"""
    n = len(close_mat)
    momo = close_mat / close_mat.shift(mom) - 1.0
    tickers = list(close_mat.columns)
    out = []
    last_rebal = -1
    for t in range(n):
        if t == 0:
            out.append(np.zeros(len(tickers)))
            continue
        if t - last_rebal < 21 and last_rebal >= 0:
            out.append(out[-1].copy())
            continue
        row = momo.iloc[t].fillna(-np.inf).values
        w = np.zeros(len(tickers))
        if safe is not None and safe in tickers:
            # 双动量：股票动量 > safe 资产动量才持股票，否则持 safe
            si = tickers.index(safe)
            order = np.argsort(row)[::-1]
            picks = []
            for i in order:
                if np.isfinite(row[i]) and row[i] > 0:
                    if safe is not None and row[i] > row[si]:
                        picks.append(i)
                    elif safe is None:
                        picks.append(i)
            if not picks and safe is not None:
                picks = [si]
        else:
            order = np.argsort(row)[::-1]
            picks = [i for i in order if np.isfinite(row[i])][:top_k]
            if abs_filter:
                picks = [i for i in picks if row[i] > 0]
        if picks:
            for i in picks:
                w[i] = 1.0 / len(picks)
        out.append(w)
        last_rebal = t
    return out


def targets_dual_momentum(close_mat: pd.DataFrame, mom: int = 126,
                          eq_cols: list[str] | None = None, safe_col: str = "TLT",
                          use_cash: bool = False) -> list[np.ndarray]:
    eq_cols = eq_cols or UNIVERSE["equity"]
    """Antonacci 双动量：股票横截面最强者的动量 > 安全资产动量 → 全仓它，否则安全资产或现金。"""
    tickers = list(close_mat.columns)
    n = len(close_mat)
    momo = close_mat / close_mat.shift(mom) - 1.0
    out = []
    last_rebal = -1
    for t in range(n):
        if t == 0:
            out.append(np.zeros(len(tickers)))
            continue
        if t - last_rebal < 21 and last_rebal >= 0:
            out.append(out[-1].copy())
            continue
        w = np.zeros(len(tickers))
        eq_mom = {c: (momo.iloc[t][c] if pd.notna(momo.iloc[t][c]) else -np.inf) for c in eq_cols}
        best = max(eq_mom, key=eq_mom.get)
        safe_mom = momo.iloc[t][safe_col] if pd.notna(momo.iloc[t][safe_col]) else -np.inf
        if eq_mom[best] > safe_mom:
            w[tickers.index(best)] = 1.0
        elif not use_cash:
            w[tickers.index(safe_col)] = 1.0
        out.append(w)
        last_rebal = t
    return out


def targets_trend_leverage(close_mat: pd.DataFrame, base: str = "SOXX",
                           lev: str = "SOXL", ma_n: int = 42, ma_n_bench: int = 42,
                           exit_to: str = "cash") -> list[np.ndarray]:
    """基础资产站上 MA → 持 3x；跌破 → exit_to('cash'/'base')。"""
    tickers = list(close_mat.columns)
    n = len(close_mat)
    base_ma = close_mat[base].rolling(ma_n).mean()
    out = []
    for t in range(n):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(base_ma.iloc[t]):
            out.append(w)
            continue
        if close_mat[base].iloc[t] > base_ma.iloc[t]:
            w[tickers.index(lev)] = 1.0
        elif exit_to == "base":
            w[tickers.index(base)] = 1.0
        out.append(w)
    return out


def targets_vol_target(close_mat: pd.DataFrame, asset: str = "SOXX",
                       target_vol: float = 0.15, window: int = 63,
                       max_lev: float = 3.0) -> list[np.ndarray]:
    """逆波动率：仓位 = min(目标波动率/已实现波动率, max_lev)。"""
    tickers = list(close_mat.columns)
    n = len(close_mat)
    rets = close_mat[asset].pct_change()
    rv = rets.rolling(window).std() * np.sqrt(252)
    out = []
    ai = tickers.index(asset)
    for t in range(n):
        w = np.zeros(len(tickers))
        if t == 0 or pd.isna(rv.iloc[t]) or rv.iloc[t] == 0:
            out.append(w)
            continue
        lev = target_vol / rv.iloc[t]
        w[ai] = float(np.clip(lev, 0.0, max_lev))
        out.append(w)
    return out


def targets_fib_retrace(close_mat: pd.DataFrame, asset: str = "SOXX",
                        swing_n: int = 5, level: float = 0.618) -> list[np.ndarray]:
    """斐波那契回调入场过滤：识别摆动高低点，回调到 61.8% 附近且未破位则买入。
    这里仅作为入场信号（0/1），验证其单独是否有效。"""
    tickers = list(close_mat.columns)
    n = len(close_mat)
    ai = tickers.index(asset)
    c = close_mat[asset].values
    out = []
    pos = 0
    swing_high = swing_low = 0.0
    for t in range(n):
        w = np.zeros(len(tickers))
        if t == 0:
            out.append(w)
            continue
        # 简化摆动点：局部高低
        if t >= swing_n:
            lo = c[t - swing_n]
            hi = c[t - swing_n]
            for k in range(t - swing_n, min(t, n)):
                lo = min(lo, c[k])
                hi = max(hi, c[k])
            if c[t] > c[t - swing_n]:
                swing_high = max(swing_high, hi)
            else:
                swing_low = min(swing_low, lo) if swing_low else lo
        if swing_high > swing_low > 0:
            retrace = swing_high - level * (swing_high - swing_low)
            if pos == 0 and c[t] <= retrace and c[t] > swing_low:
                pos = 1
            elif pos == 1 and c[t] < swing_low:
                pos = 0
                swing_high = swing_low = 0.0
        if pos == 1:
            w[ai] = 1.0
        out.append(w)
    return out


# ---------------- 组合回测 ----------------
@dataclass
class PResult:
    total: float
    bench: float
    excess: float
    maxdd: float
    bench_maxdd: float
    sharpe: float
    trades: int
    exposure: float


def backtest_portfolio(close_mat: pd.DataFrame, open_mat: pd.DataFrame,
                       targets: list[np.ndarray], i0: int, i1: int,
                       bench_ticker: str, fee: float = FEE) -> PResult:
    n_assets = close_mat.shape[1]
    V = 1.0
    w = np.zeros(n_assets)  # 上一段持有的权重（相对 close[t-1]）
    eq = []
    expo = 0
    turnover_sum = 0.0
    for t in range(i0, i1 + 1):
        # 1) 隔夜跳空：close[t-1] -> open[t]，按旧权重 w 持有
        if t > i0:
            gap_ret = 0.0
            for a in range(n_assets):
                if w[a] > 0:
                    pc = close_mat.iloc[t - 1, a]
                    o = open_mat.iloc[t, a]
                    if pc and pc > 0 and not pd.isna(o) and o > 0:
                        gap_ret += w[a] * (o / pc - 1.0)
            V *= (1 + gap_ret)
        # 2) 开盘换仓：按 close[t-1] 决策 targets[t-1]，在 open[t] 成交
        target = targets[t - 1] if t > 0 else np.zeros(n_assets)
        turnover = float(np.abs(target - w).sum())
        V *= (1 - fee * turnover)
        turnover_sum += turnover
        w = target
        # 3) 日内：open[t] -> close[t]，按新权重 w 持有
        if t > i0:
            day_ret = 0.0
            for a in range(n_assets):
                if w[a] > 0:
                    o = open_mat.iloc[t, a]
                    c = close_mat.iloc[t, a]
                    if o and o > 0 and not pd.isna(c):
                        day_ret += w[a] * (c / o - 1.0)
            V *= (1 + day_ret)
        eq.append(V)
        expo += 1 if w.sum() > 0 else 0
    bench = (close_mat[bench_ticker].iloc[i1] / open_mat[bench_ticker].iloc[i0]) * (1 - fee) ** 2
    bench_close = close_mat[bench_ticker].iloc[i0:i1 + 1].values
    bench_open = open_mat[bench_ticker].iloc[i0]
    bench_eq = bench_close / bench_open
    bench_dd = float(np.min(bench_eq / np.maximum.accumulate(bench_eq) - 1))
    eq = np.array(eq)
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([])
    maxdd = float(np.min(eq / np.maximum.accumulate(eq) - 1)) if len(eq) else 0.0
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if len(rets) > 1 and np.std(rets) > 0 else 0.0
    return PResult(total=float(V - 1), bench=float(bench - 1), excess=float(V - bench),
                   maxdd=maxdd, bench_maxdd=bench_dd, sharpe=sharpe, trades=int(turnover_sum),
                   exposure=expo / max(len(eq), 1))


def windows(n: int, years: int = 5) -> list[tuple[int, int]]:
    end = n - 1
    start = max(WINDOW + 5, n - years * 252)
    out = []
    s = start
    while s + WINDOW <= end:
        out.append((s, s + WINDOW - 1))
        s += STEP
    return out or [(max(WINDOW + 5, 0), end)]


def evaluate(close_mat, open_mat, targets, wins, bench_ticker) -> dict:
    rs = [backtest_portfolio(close_mat, open_mat, targets, a, b, bench_ticker) for a, b in wins]
    ex = np.array([r.excess for r in rs])
    return {
        "mean_excess": ex.mean(), "median_excess": float(np.median(ex)),
        "beat_rate": float(np.mean(ex > 0)),
        "mean_sharpe": np.mean([r.sharpe for r in rs]),
        "mean_total": np.mean([r.total for r in rs]),
        "mean_bench": np.mean([r.bench for r in rs]),
        "worst_dd": min(r.maxdd for r in rs),
        "mean_bench_dd": np.mean([r.bench_maxdd for r in rs]),
        "mean_trades": np.mean([r.trades for r in rs]),
        "mean_exposure": np.mean([r.exposure for r in rs]),
    }


def _run(close_mat, open_mat, targets, bench_ticker, wins, label, verbose=True):
    r = evaluate(close_mat, open_mat, targets, wins, bench_ticker)
    r["strategy"] = label
    if verbose:
        print(f"  {label:<34} 超额 {r['mean_excess']*100:+7.1f}%  跑赢 {r['beat_rate']*100:3.0f}%  "
              f"夏普 {r['mean_sharpe']:4.2f}  策略 {r['mean_total']*100:+7.1f}%  "
              f"基准 {r['mean_bench']*100:+7.1f}%  回撤 {r['worst_dd']*100:5.1f}%  "
              f"在场 {r['mean_exposure']*100:3.0f}%")
    return r


def main() -> None:
    ap = argparse.ArgumentParser(description="多资产策略：寻找跑赢买入持有的方法")
    ap.add_argument("--bench", default="SOXX", help="基准资产（买入持有对照）")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--days", type=int, default=1600)
    ap.add_argument("--search", action="store_true", help="参数扫描")
    args = ap.parse_args()

    bench = args.bench.upper()
    # 组合资产池：股票 + 债券 + 杠杆（去掉反向，避免做空）
    pool = UNIVERSE["equity"] + UNIVERSE["bond"] + UNIVERSE["lev3"]
    # 去重、保序
    pool = list(dict.fromkeys(pool))
    print(f"加载 {len(pool)} 个资产 ...", flush=True)
    close_mat, open_mat, dates = load_aligned(pool, args.days)
    print(f"对齐后 {close_mat.shape[0]} 根日线，{close_mat.shape[1]} 个资产")
    wins = windows(len(close_mat), args.years)
    print(f"滚动 {len(wins)} 个1年窗口，基准={bench}，手续费{FEE*100:.2f}%\n")

    rows = []

    # ---- 1. 横截面动量轮动（含杠杆资产池） ----
    for top_k in (1, 2, 3):
        for mom in (63, 126, 252):
            for abs_f in (False, True):
                tgt = targets_momentum(close_mat, top_k=top_k, mom=mom, abs_filter=abs_f)
                rows.append(_run(close_mat, open_mat, tgt, bench, wins,
                                 f"mom_rot k={top_k} mom={mom} abs={abs_f}"))

    # ---- 2. 双动量 ----
    for mom in (63, 126, 252):
        for safe in ("TLT", "IEF", "SHY"):
            for cash in (False, True):
                tgt = targets_dual_momentum(close_mat, mom=mom, eq_cols=UNIVERSE["equity"],
                                            safe_col=safe, use_cash=cash)
                rows.append(_run(close_mat, open_mat, tgt, bench, wins,
                                 f"dual_mom mom={mom} safe={safe} cash={cash}"))

    # ---- 3. 趋势 + 杠杆 ----
    for base, lev in BASE_TO_LEV.items():
        for ma_n in (21, 42, 63, 100, 200):
            for exit_to in ("cash", "base"):
                tgt = targets_trend_leverage(close_mat, base=base, lev=lev, ma_n=ma_n, exit_to=exit_to)
                rows.append(_run(close_mat, open_mat, tgt, bench, wins,
                                 f"trend_lev {base}>{lev} MA{ma_n} exit={exit_to}"))

    # ---- 4. 波动率目标 ----
    for asset in ("SOXX", "QQQ", "SPY"):
        for tv in (0.10, 0.15, 0.20):
            for mx in (2.0, 3.0):
                tgt = targets_vol_target(close_mat, asset=asset, target_vol=tv, max_lev=mx)
                rows.append(_run(close_mat, open_mat, tgt, bench, wins,
                                 f"vol_target {asset} vol={tv} max={mx}"))

    # ---- 5. 斐波那契回调 ----
    for lv in (0.382, 0.5, 0.618):
        tgt = targets_fib_retrace(close_mat, asset=bench, level=lv)
        rows.append(_run(close_mat, open_mat, tgt, bench, wins,
                         f"fib_retrace {bench} lv={lv}"))

    res = pd.DataFrame(rows)
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, f"multi_{bench}.csv")
    res.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n已保存: {path}\n")

    print("===== Top 15 按平均超额 =====")
    for _, r in res.sort_values("mean_excess", ascending=False).head(15).iterrows():
        print(f"  {r.strategy:<34} 超额 {r.mean_excess*100:+7.1f}%  跑赢 {r.beat_rate*100:3.0f}%  "
              f"夏普 {r.mean_sharpe:4.2f}  回撤 {r.worst_dd*100:5.1f}%")

    print("\n===== Top 10 按夏普 =====")
    for _, r in res.sort_values("mean_sharpe", ascending=False).head(10).iterrows():
        print(f"  {r.strategy:<34} 夏普 {r.mean_sharpe:4.2f}  超额 {r.mean_excess*100:+7.1f}%  "
              f"跑赢 {r.beat_rate*100:3.0f}%  回撤 {r.worst_dd*100:5.1f}%")


if __name__ == "__main__":
    main()
