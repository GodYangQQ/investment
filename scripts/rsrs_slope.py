#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
RSRS 斜率 / RSRS 分查看器

用法:
    python scripts/rsrs_slope.py                # 计算所有大盘指数右偏修正分并排序（默认）
    python scripts/rsrs_slope.py --indices      # SOXX/SMH/QQQ/SPY/^SOX
    python scripts/rsrs_slope.py --pool         # 埋伏股票池：按 rsrs.py 选股逻辑排序并给出 RSRS 分与信号
    python scripts/rsrs_slope.py NVDA            # 单只股票

指数用法（RSRS 本用于指数择时）:
    python scripts/rsrs_slope.py SOXX           # 别名: SOXX/QQQ/SPY/SMH
    python scripts/rsrs_slope.py --index QQQ

斜率计算逻辑（对齐 scripts/rsrs.py 聚宽 RSRS 择时）：
    用最近 N(=18) 个交易日的高价对低价做 OLS 回归: high = a + slope * low
    当前斜率   = 回归系数 beta
    标准分     = (最新斜率 - 最近 M(=1100) 个斜率的均值) / 标准差
    右偏修正分 = 标准分 * 最新 beta * 最新 R²   （即 "rsrs分"）
    择时信号   : 右偏修正分 > 0.7 买入, < -0.7 卖出, 其余观望

价值选股逻辑（对齐 rsrs.py trade_func）：
    PB>0 且 ROE>0，价值分 = rank(PB) + rank(1/ROE)，分数越低越靠前
    排名在 data/埋伏标的.csv 股票池内计算；PB 取自
    data/soxx_score_intermediate.csv，ROE 取自 yfinance

行情数据源: yfinance
'''
import argparse
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
POOL_FILE = ROOT / "data" / "my_pool.csv"
PB_FILE = ROOT / "data" / "soxx_score_intermediate.csv"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

N = 18      # 回归窗口
M = 1100    # 计算标准分的历史样本长度
BUY = 0.7
SELL = -0.7
WORKERS = 4  # 并发抓取线程数

socket.setdefaulttimeout(30)  # 防止个别网络请求无限挂起

# 指数别名 -> (新浪代码, 名称)
INDEX_ALIASES = {
    "SOXX": ("SOXX", "iShares Semiconductor ETF"),
    "SMH": ("SMH", "VanEck Semiconductor ETF"),
    "QQQ": ("QQQ", "Invesco QQQ"),
    "SPY": ("SPY", "SPDR S&P 500"),
    "^SOX": ("^SOX", "PHLX Semiconductor Index"),
    "SOX": ("^SOX", "PHLX Semiconductor Index"),
}

BROAD_INDICES = [
    ("SOXX", "iShares Semiconductor ETF"),
    ("SMH", "VanEck Semiconductor ETF"),
    ("QQQ", "Invesco QQQ"),
    ("SPY", "SPDR S&P 500"),
    ("^SOX", "PHLX Semiconductor Index"),
]

INDEX_NAMES = dict(BROAD_INDICES)


# ============================================================================
# 行情抓取
# ============================================================================

def _index_symbol(code: str) -> str:
    return INDEX_ALIASES.get(code.upper(), (code, code))[0]


def _hist_hl(symbol: str) -> pd.DataFrame:
    sys.path.insert(0, str(ROOT / "core"))
    from us_data import fetch_daily_kline
    df = fetch_daily_kline(symbol, days=1200)
    out = df[["date", "high", "low"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    return out.set_index("date").sort_index().astype(float).dropna()


def fetch_stock_history(symbol: str) -> pd.DataFrame:
    return _hist_hl(symbol)


def fetch_index_history(sina_symbol: str) -> pd.DataFrame:
    return _hist_hl(sina_symbol)


def all_indices() -> pd.DataFrame:
    """大盘指数列表。"""
    return pd.DataFrame(BROAD_INDICES, columns=["代码", "名称"])


def fetch_name(symbol: str) -> str:
    try:
        sys.path.insert(0, str(ROOT / "core"))
        from us_data import fetch_realtime_quote
        from universe import get_soxx_universe
        for s in get_soxx_universe(live=False):
            if s["code"].upper() == str(symbol).upper():
                return s["name"]
        return fetch_realtime_quote(symbol).get("name") or ""
    except Exception:
        return str(symbol)


# ============================================================================
# RSRS 计算
# ============================================================================

def rsrs_series(highs: pd.Series, lows: pd.Series) -> pd.DataFrame:
    """滚动 OLS 回归 high = a + beta*low，返回每期的斜率(beta)与R²。

    用滚动和的解析公式向量化计算，结果与逐窗 statsmodels OLS 完全一致。
    """
    x, y = lows, highs
    sx = x.rolling(N).sum()
    sy = y.rolling(N).sum()
    sxx = (x * x).rolling(N).sum()
    sxy = (x * y).rolling(N).sum()
    syy = (y * y).rolling(N).sum()
    denom = N * sxx - sx * sx
    cov = N * sxy - sx * sy
    beta = cov / denom
    r2 = cov * cov / (denom * (N * syy - sy * sy))
    return pd.DataFrame({"beta": beta, "r2": r2}).dropna()


def rsrs_metrics(hist: pd.DataFrame) -> dict | None:
    """计算 RSRS 指标。返回 None 表示数据不足。"""
    if len(hist) < N:
        return None
    highs = pd.Series(hist["high"].to_numpy(), index=hist.index)
    lows = pd.Series(hist["low"].to_numpy(), index=hist.index)
    series = rsrs_series(highs, lows)
    if series.empty:
        return None

    beta = float(series["beta"].iloc[-1])
    r2 = float(series["r2"].iloc[-1])
    section = series["beta"].tail(M)
    mu = float(section.mean())
    sigma = float(section.std(ddof=0))
    zscore = (float(section.iloc[-1]) - mu) / sigma if sigma > 0 else float("nan")
    return {
        "beta": beta, "r2": r2, "mu": mu, "sigma": sigma,
        "zscore": zscore, "rightdev": zscore * beta * r2,
    }


def _signal(score: float) -> str:
    if score != score:  # NaN
        return "-"
    if score > BUY:
        return "买入"
    if score < SELL:
        return "卖出"
    return "观望"


def _parallel_map(func, codes: list[str]) -> dict:
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        values = list(pool.map(func, codes))
    return dict(zip(codes, values))


def _rightdev_for_stock(code: str) -> float:
    for _ in range(2):
        try:
            metrics = rsrs_metrics(fetch_stock_history(code))
            if metrics:
                return metrics["rightdev"]
        except Exception:  # noqa: BLE001
            pass
    return float("nan")


def _rightdev_for_index(sina_symbol: str) -> float:
    for _ in range(2):
        try:
            metrics = rsrs_metrics(fetch_index_history(sina_symbol))
            if metrics:
                return metrics["rightdev"]
        except Exception:  # noqa: BLE001
            pass
    return float("nan")


def _roe_for(code: str) -> float:
    """抓取最新加权 ROE(%)，带重试。失败返回 NaN。"""
    from core.fundamental_filter import fetch_single_fundamentals
    for _ in range(3):
        try:
            val = fetch_single_fundamentals(code).get("roe")
            if val is not None and val == val:
                return float(val)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return float("nan")


# ============================================================================
# 输出
# ============================================================================

def _print_detail(code: str, name: str, hist: pd.DataFrame, metrics: dict) -> None:
    rightdev = metrics["rightdev"]
    if rightdev > BUY:
        signal = f"买入信号（右偏修正分 > {BUY}）"
    elif rightdev < SELL:
        signal = f"卖出/空仓信号（右偏修正分 < {SELL}）"
    else:
        signal = "观望（阈值之间）"

    print(f"证券代码  : {code}")
    print(f"证券名称  : {name}")
    print(f"数据截至  : {str(hist.index[-1])[:10]}（共 {len(hist)} 个交易日）")
    print(f"回归窗口  : 最近 {N} 日（{str(hist.index[-N])[:10]} ~ {str(hist.index[-1])[:10]}）")
    print("-" * 40)
    print(f"当前 RSRS 斜率 (beta): {metrics['beta']:.6f}")
    print(f"回归决定系数 R²      : {metrics['r2']:.6f}")
    print(f"斜率均值 (近{M}期)   : {metrics['mu']:.6f}")
    print(f"斜率标准差 (近{M}期) : {metrics['sigma']:.6f}")
    print(f"RSRS 标准分          : {metrics['zscore']:.4f}")
    print(f"右偏修正标准分       : {rightdev:.4f}")
    print(f"择时信号             : {signal}")


def run_indices() -> None:
    """计算美股半导体相关指数右偏修正分。"""
    idx = all_indices()
    codes = idx["代码"].tolist()
    scores = _parallel_map(_rightdev_for_index, codes)
    idx["rsrs分"] = idx["代码"].map(scores)
    idx = idx.sort_values("rsrs分", ascending=False, na_position="last", kind="stable")

    print("指数代码\t指数名称\trsrs分\t信号")
    for _, row in idx.iterrows():
        raw = row["rsrs分"]
        score = float("nan") if raw != raw else float(raw)
        score_str = "-" if _signal(score) == "-" else f"{score:.4f}"
        print(f"{row['代码']}\t{row['名称']}\t{score_str}\t{_signal(score)}")


def run_index(sina_symbol: str, name: str = "") -> None:
    hist = fetch_index_history(sina_symbol)
    metrics = rsrs_metrics(hist)
    if metrics is None:
        print(f"{sina_symbol} 数据不足（{len(hist)} 个交易日 < N={N}），无法计算")
        sys.exit(1)
    _print_detail(sina_symbol, name or INDEX_NAMES.get(sina_symbol, "-"), hist, metrics)


# ============================================================================
# 股票池价值选股
# ============================================================================

def load_pool() -> pd.DataFrame:
    df = pd.read_csv(POOL_FILE, dtype={"代码": str})
    codes = df["代码"].astype(str).tolist()
    names = df["名称"].astype(str).tolist()
    return pd.DataFrame({"代码": codes, "名称": names})


def load_pb() -> dict:
    df = pd.read_csv(PB_FILE, dtype={"代码": str})
    codes = df["代码"].astype(str).tolist()
    values = np.asarray(pd.to_numeric(df["PB"], errors="coerce"), dtype=float).tolist()
    return dict(zip(codes, values))


def compute_value_table(pool: pd.DataFrame) -> pd.DataFrame:
    """在给定股票池内按 rsrs.py trade_func 逻辑计算价值分与排名。"""
    codes = pool["代码"].astype(str).tolist()
    pb_map = load_pb()
    roe_map = _parallel_map(_roe_for, codes)

    df = pool.copy()
    df["PB"] = df["代码"].map(pb_map)
    df["ROE"] = df["代码"].map(roe_map)

    valid = (df["PB"] > 0) & (df["ROE"] > 0)
    df["point"] = np.nan
    df.loc[valid, "point"] = df.loc[valid, "PB"].rank() + (1.0 / df.loc[valid, "ROE"]).rank()
    df = df.sort_values("point", na_position="last", kind="stable").reset_index(drop=True)

    rank = np.full(len(df), np.nan)
    n_valid = int(df["point"].notna().sum())
    rank[:n_valid] = np.arange(1, n_valid + 1)
    df["价值排名"] = rank
    return df


def run_pool() -> None:
    df = compute_value_table(load_pool())
    codes = df["代码"].astype(str).tolist()
    rsrs_map = _parallel_map(_rightdev_for_stock, codes)
    df["rsrs分"] = df["代码"].map(rsrs_map)

    print("股票代码\t股票名称\trsrs分\t信号")
    for _, row in df.iterrows():
        raw = row["rsrs分"]
        score = float("nan") if raw != raw else float(raw)
        score_str = "-" if _signal(score) == "-" else f"{score:.4f}"
        print(f"{row['代码']}\t{row['名称']}\t{score_str}\t{_signal(score)}")


def run_single(symbol: str) -> None:
    hist = fetch_stock_history(symbol)
    metrics = rsrs_metrics(hist)
    if metrics is None:
        print(f"{symbol} 上市时间过短（{len(hist)} 个交易日 < N={N}），无法计算")
        sys.exit(1)
    _print_detail(symbol, fetch_name(symbol) or "-", hist, metrics)

    # 价值选股计算值（PB、ROE、池内价值分与排名）
    pool = load_pool()
    if symbol in set(pool["代码"].astype(str)):
        table = compute_value_table(pool)
        row = table[table["代码"].astype(str) == symbol].iloc[0]
        pb, roe = float(row["PB"]), float(row["ROE"])
        if row["point"] != row["point"]:
            value_str = f"PB={pb:.2f}  ROE={roe:.2f}%  价值分=-（PB/ROE 不满足 >0）"
        else:
            value_str = (f"PB={pb:.2f}  ROE={roe:.2f}%  "
                         f"价值分={float(row['point']):.1f}（池内第{int(row['价值排名'])}/{len(table)}）")
    else:
        pb = float(load_pb().get(symbol, float("nan")))
        roe = float(_roe_for(symbol))
        pb_str = "-" if pb != pb else f"{pb:.2f}"
        roe_str = "-" if roe != roe else f"{roe:.2f}%"
        value_str = f"PB={pb_str}  ROE={roe_str}（不在埋伏池内，无池内排名）"
    print(f"价值选股             : {value_str}")


def main() -> None:
    parser = argparse.ArgumentParser(description="RSRS 斜率 / RSRS 分查看器")
    parser.add_argument("symbol", nargs="?", help="股票代码，或指数别名/代码")
    parser.add_argument("--indices", action="store_true", help="计算 SOXX/QQQ/SPY 等右偏修正分")
    parser.add_argument("--pool", action="store_true", help="对 SOXX 池按选股逻辑排序")
    parser.add_argument("--index", action="store_true", help="把 symbol 当作 ETF/指数代码")
    args = parser.parse_args()

    if args.pool:
        run_pool()
    elif args.indices:
        run_indices()
    elif args.symbol:
        if args.index:
            run_index(_index_symbol(args.symbol), INDEX_NAMES.get(_index_symbol(args.symbol), "-"))
        elif args.symbol in INDEX_ALIASES:
            run_index(*INDEX_ALIASES[args.symbol])
        else:
            run_single(args.symbol)
    else:
        run_indices()


if __name__ == "__main__":
    main()
