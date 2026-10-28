#!/usr/bin/env python3
"""
择时策略大全：横评互联网上能找到的择时方法
==========================================
统一口径：信号在收盘计算、次日开盘成交、手续费 0.05%、只做多、对比同标的买入持有。
每个策略在 20+ 个多样资产（美股 ETF + A股指数）上回测，报告平均超额 + 平均回撤。

策略分类：
  趋势类      ma_cross / dual_ma / triple_ma / macd / turtle / supertrend / psar /
              adx / ichimoku / kama / hull / bb_breakout / keltner_breakout
  均值回归类  rsi / bb_revert / stochastic / cci / williams_r / zscore
  动量类      ts_momentum / roc / n_day_high
  波动类      atr_chandelier
  A股特色     rsrs
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from core.datasets.us_data import fetch_daily_kline  # noqa: E402
from core.datasets.price_data import fetch_daily  # noqa: E402

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
FEE = 0.0005

# 多样资产池：美股权益 + 美股债券 + 美股杠杆 + A股指数
UNIVERSE = [
    # 美股权益
    "SPY", "QQQ", "SOXX", "SMH", "XLK", "VGT", "XSD", "PSI",
    # 美股债券/信用
    "TLT", "IEF", "SHY", "LQD", "HYG", "BND", "AGG",
    # 美股杠杆
    "SOXL", "TQQQ", "UPRO",
    # A股指数
    "sh000001", "sh000300", "sz399006", "sh000905", "sh000016", "sz399001",
]

CLASS = {}
for _s in ["SPY", "QQQ", "SOXX", "SMH", "XLK", "VGT", "XSD", "PSI"]:
    CLASS[_s] = "US_equity"
for _s in ["TLT", "IEF", "SHY", "LQD", "HYG", "BND", "AGG"]:
    CLASS[_s] = "US_bond"
for _s in ["SOXL", "TQQQ", "UPRO"]:
    CLASS[_s] = "US_lev"
for _s in ["sh000001", "sh000300", "sz399006", "sh000905", "sh000016", "sz399001"]:
    CLASS[_s] = "A_index"


def load_asset(sym: str, days: int = 1500) -> pd.DataFrame:
    if sym.startswith(("sh", "sz", "bj")):
        rows, _ = fetch_daily(sym, days)
        if not rows:
            raise RuntimeError(f"empty A-share data {sym}")
        df = pd.DataFrame(rows).rename(columns={"t": "date", "o": "open", "h": "high",
                                                "l": "low", "c": "close", "v": "volume"})
        df["date"] = pd.to_datetime(df["date"])
    else:
        df = fetch_daily_kline(sym, days=days).copy()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").dropna(subset=["open", "close"]).reset_index(drop=True)


# ---------------- 指标 ----------------
def sma(s, n):
    return s.rolling(n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def wma(s, n):
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df, n=14):
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def bollinger(s, n=20, k=2.0):
    m = sma(s, n)
    sd = s.rolling(n).std()
    return m + k * sd, m - k * sd


def keltner(df, n=20, k=2.0):
    m = ema(df["close"], n)
    a = atr(df, n)
    return m + k * a, m - k * a


def macd(s, fast=12, slow=26, sig=9):
    line = ema(s, fast) - ema(s, slow)
    return line, ema(line, sig)


def adx(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / a
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), plus_di, minus_di


def psar(df, af0=0.02, af_max=0.2):
    high, low = df["high"].values, df["low"].values
    n = len(df)
    sar = np.zeros(n)
    trend = np.ones(n)  # 1 long, -1 short
    af = af0
    ep = high[0]
    sar[0] = low[0]
    trend[0] = 1
    for i in range(1, n):
        sar[i] = sar[i - 1] + af * (ep - sar[i - 1])
        if trend[i - 1] == 1:
            sar[i] = min(sar[i], low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < sar[i]:
                trend[i] = -1
                sar[i] = ep
                ep = low[i]
                af = af0
            else:
                trend[i] = 1
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af0, af_max)
        else:
            sar[i] = max(sar[i], high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > sar[i]:
                trend[i] = 1
                sar[i] = ep
                ep = high[i]
                af = af0
            else:
                trend[i] = -1
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af0, af_max)
    return pd.Series(sar, index=df.index), pd.Series(trend, index=df.index)


def stochastic(df, n=14):
    ll = df["low"].rolling(n).min()
    hh = df["high"].rolling(n).max()
    k = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    return k, k.rolling(3).mean()


def cci(df, n=20):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    m = tp.rolling(n).mean()
    md = tp.rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - m) / (0.015 * md)


def williams_r(df, n=14):
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    return -100 * (hh - df["close"]) / (hh - ll).replace(0, np.nan)


def hull(s, n=21):
    return wma(2 * wma(s, n // 2) - wma(s, n), int(np.sqrt(n)))


def kama(s, n=10, fast=2, slow=30):
    change = (s - s.shift(n)).abs()
    vol = s.diff().abs().rolling(n).sum()
    er = (change / vol.replace(0, np.nan)).fillna(0)
    sc = (er * (2 / (fast + 1) - 2 / (slow + 1)) + 2 / (slow + 1)) ** 2
    out = pd.Series(index=s.index, dtype=float)
    if len(s) > n:
        out.iloc[n] = s.iloc[:n + 1].mean()
        for i in range(n + 1, len(s)):
            out.iloc[i] = out.iloc[i - 1] + sc.iloc[i] * (s.iloc[i] - out.iloc[i - 1])
    return out


def ichimoku(df, tenkan=9, kijun=26, senkou_b=52):
    conv = (df["high"].rolling(tenkan).max() + df["low"].rolling(tenkan).min()) / 2
    base = (df["high"].rolling(kijun).max() + df["low"].rolling(kijun).min()) / 2
    lead_a = ((conv + base) / 2).shift(kijun)
    lead_b = (df["high"].rolling(senkou_b).max() + df["low"].rolling(senkou_b).min() / 2).shift(kijun)
    return conv, base, lead_a, lead_b


def rsrs(df, n=18, m=600):
    """RSRS 阻力支撑相对强度：low ~ a + b*high 的斜率 b，标准化后取信号。"""
    x = df["high"]
    y = df["low"]
    mx = x.rolling(n).mean()
    my = y.rolling(n).mean()
    cov = (x * y).rolling(n).mean() - mx * my
    var = x.rolling(n).var()
    beta = cov / var
    mu = beta.rolling(m).mean()
    sd = beta.rolling(m).std()
    return ((beta - mu) / sd.replace(0, np.nan)).fillna(0)


# ---------------- 信号（0/1，收盘决策） ----------------
def _state(sig_cond, df):
    """由逐日 0/1 条件构造持仓状态（含缓冲切换，避免反复）。直接返回 cond。"""
    return sig_cond.astype(int)


def s_ma_cross(df, y=42, buffer=0.0):
    c, m = df["close"], sma(df["close"], y)
    up, dn = m * (1 + buffer), m * (1 - buffer)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(m.iloc[i]):
            continue
        if pos == 0 and c.iloc[i - 1] <= up.iloc[i - 1] and c.iloc[i] > up.iloc[i]:
            pos = 1
        elif pos == 1 and c.iloc[i - 1] >= dn.iloc[i - 1] and c.iloc[i] < dn.iloc[i]:
            pos = 0
        sig.iloc[i] = pos
    return sig


def s_dual_ma(df, fast=20, slow=50):
    f, s = sma(df["close"], fast), sma(df["close"], slow)
    return ((f > s) & s.notna()).astype(int)


def s_triple_ma(df, a=20, b=50, c=200):
    ca = df["close"]
    return ((ca > sma(ca, a)) & (sma(ca, a) > sma(ca, b)) & (sma(ca, b) > sma(ca, c))).astype(int)


def s_macd(df, fast=12, slow=26, sig=9):
    line, signal = macd(df["close"], fast, slow, sig)
    return ((line > signal) & signal.notna()).astype(int)


def s_turtle(df, entry=20, exit_n=10):
    hi = df["high"].rolling(entry).max().shift(1)
    lo = df["low"].rolling(exit_n).min().shift(1)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if np.isnan(hi.iloc[i]) or np.isnan(lo.iloc[i]):
            continue
        if pos == 0 and df["close"].iloc[i] > hi.iloc[i]:
            pos = 1
        elif pos == 1 and df["close"].iloc[i] < lo.iloc[i]:
            pos = 0
        sig.iloc[i] = pos
    return sig


def s_supertrend(df, n=10, mult=3.0):
    a = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    st = pd.Series(np.nan, index=df.index)
    trend = pd.Series(1, index=df.index)
    for i in range(1, len(df)):
        if np.isnan(a.iloc[i]):
            continue
        prev = st.iloc[i - 1]
        if pd.isna(prev):
            trend.iloc[i] = 1 if df["close"].iloc[i] > lower.iloc[i] else -1
        elif trend.iloc[i - 1] == 1:
            trend.iloc[i] = -1 if df["close"].iloc[i] < prev else 1
        else:
            trend.iloc[i] = 1 if df["close"].iloc[i] > prev else -1
        if trend.iloc[i] == 1:
            st.iloc[i] = max(lower.iloc[i], prev) if not pd.isna(prev) else lower.iloc[i]
        else:
            st.iloc[i] = min(upper.iloc[i], prev) if not pd.isna(prev) else upper.iloc[i]
    return (trend > 0).astype(int)


def s_psar(df):
    _, trend = psar(df)
    return (trend > 0).astype(int)


def s_adx(df, n=14, thresh=20):
    adx_v, plus_di, minus_di = adx(df, n)
    return ((plus_di > minus_di) & (adx_v > thresh)).astype(int)


def s_ichimoku(df):
    conv, base, lead_a, lead_b = ichimoku(df)
    cloud_up = lead_a > lead_b
    return ((df["close"] > lead_a) & (df["close"] > lead_b) & (conv > base) & lead_a.notna()).astype(int)


def s_kama(df, n=10):
    k = kama(df["close"], n)
    return ((df["close"] > k) & k.notna()).astype(int)


def s_hull(df, n=21):
    h = hull(df["close"], n)
    return ((df["close"] > h) & h.notna()).astype(int)


def s_bb_breakout(df, n=20, k=2.0):
    up, _ = bollinger(df["close"], n, k)
    return ((df["close"] > up) & up.notna()).astype(int)


def s_keltner_breakout(df, n=20, k=2.0):
    up, _ = keltner(df, n, k)
    return ((df["close"] > up) & up.notna()).astype(int)


def s_rsi(df, n=14, low=30, high=70):
    r = rsi(df["close"], n)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if pd.isna(r.iloc[i]):
            continue
        if pos == 0 and r.iloc[i] < low:
            pos = 1
        elif pos == 1 and r.iloc[i] > high:
            pos = 0
        sig.iloc[i] = pos
    return sig


def s_bb_revert(df, n=20, k=2.0):
    up, dn = bollinger(df["close"], n, k)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if pd.isna(up.iloc[i]):
            continue
        if pos == 0 and df["close"].iloc[i] < dn.iloc[i]:
            pos = 1
        elif pos == 1 and df["close"].iloc[i] > up.iloc[i]:
            pos = 0
        sig.iloc[i] = pos
    return sig


def s_stochastic(df, n=14, low=20, high=80):
    k, _ = stochastic(df, n)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if pd.isna(k.iloc[i]):
            continue
        if pos == 0 and k.iloc[i] < low:
            pos = 1
        elif pos == 1 and k.iloc[i] > high:
            pos = 0
        sig.iloc[i] = pos
    return sig


def s_cci(df, n=20, low=-100, high=100):
    c = cci(df, n)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if pd.isna(c.iloc[i]):
            continue
        if pos == 0 and c.iloc[i] < low:
            pos = 1
        elif pos == 1 and c.iloc[i] > high:
            pos = 0
        sig.iloc[i] = pos
    return sig


def s_williams_r(df, n=14, low=-80, high=-20):
    wr = williams_r(df, n)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if pd.isna(wr.iloc[i]):
            continue
        if pos == 0 and wr.iloc[i] < low:
            pos = 1
        elif pos == 1 and wr.iloc[i] > high:
            pos = 0
        sig.iloc[i] = pos
    return sig


def s_zscore(df, n=20, low=-2.0, high=2.0):
    m = sma(df["close"], n)
    sd = df["close"].rolling(n).std()
    z = (df["close"] - m) / sd.replace(0, np.nan)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if pd.isna(z.iloc[i]):
            continue
        if pos == 0 and z.iloc[i] < low:
            pos = 1
        elif pos == 1 and z.iloc[i] > high:
            pos = 0
        sig.iloc[i] = pos
    return sig


def s_ts_momentum(df, mom=126):
    c = df["close"]
    return ((c > c.shift(mom)) & c.shift(mom).notna()).astype(int)


def s_roc(df, n=12):
    roc = df["close"].pct_change(n)
    return ((roc > 0) & roc.notna()).astype(int)


def s_n_day_high(df, n=55):
    hi = df["high"].rolling(n).max().shift(1)
    return ((df["close"] > hi) & hi.notna()).astype(int)


def s_atr_chandelier(df, n=22, mult=3.0):
    m = df["close"].rolling(n).max()
    stop = m - mult * atr(df, n)
    return ((df["close"] > stop) & stop.notna()).astype(int)


def s_rsrs(df, n=18, thresh=0.7):
    r = rsrs(df, n)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(1, len(df)):
        if pd.isna(r.iloc[i]):
            continue
        if pos == 0 and r.iloc[i] > thresh:
            pos = 1
        elif pos == 1 and r.iloc[i] < -thresh:
            pos = 0
        sig.iloc[i] = pos
    return sig


STRATEGIES = {
    # 趋势类
    "ma_cross_42_b1%":      lambda d: s_ma_cross(d, 42, 0.01),
    "dual_ma_20_50":        lambda d: s_dual_ma(d, 20, 50),
    "dual_ma_50_200":       lambda d: s_dual_ma(d, 50, 200),
    "triple_ma":            lambda d: s_triple_ma(d),
    "macd_12_26_9":         lambda d: s_macd(d),
    "turtle_20_10":         lambda d: s_turtle(d, 20, 10),
    "supertrend_10_3":      lambda d: s_supertrend(d),
    "psar":                 lambda d: s_psar(d),
    "adx_14_20":            lambda d: s_adx(d),
    "ichimoku":             lambda d: s_ichimoku(d),
    "kama_10":              lambda d: s_kama(d, 10),
    "hull_21":              lambda d: s_hull(d, 21),
    "bb_breakout_20_2":     lambda d: s_bb_breakout(d),
    "keltner_breakout":     lambda d: s_keltner_breakout(d),
    # 均值回归类
    "rsi_30_70":            lambda d: s_rsi(d),
    "bb_revert_20_2":       lambda d: s_bb_revert(d),
    "stochastic_20_80":     lambda d: s_stochastic(d),
    "cci_100":              lambda d: s_cci(d),
    "williams_r":           lambda d: s_williams_r(d),
    "zscore_2":             lambda d: s_zscore(d),
    # 动量类
    "ts_mom_126":           lambda d: s_ts_momentum(d, 126),
    "ts_mom_252":           lambda d: s_ts_momentum(d, 252),
    "roc_12":               lambda d: s_roc(d, 12),
    "n_day_high_55":        lambda d: s_n_day_high(d, 55),
    # 波动类
    "atr_chandelier":       lambda d: s_atr_chandelier(d),
    # A股特色
    "rsrs_0.7":             lambda d: s_rsrs(d),
}


# ---------------- 回测 ----------------
def backtest(df, sig, fee=FEE):
    close, open_ = df["close"].values, df["open"].values
    value = 1.0
    pos = 0
    entry = 0.0
    eq = [1.0]
    for t in range(1, len(df)):
        want = int(sig.iloc[t - 1]) if not pd.isna(sig.iloc[t - 1]) else 0
        if want == 1 and pos == 0:
            value *= (1 - fee)
            value *= close[t] / open_[t]
            pos = 1
            entry = open_[t]
        elif want == 0 and pos == 1:
            value *= open_[t] / close[t - 1]
            value *= (1 - fee)
            pos = 0
        elif pos == 1:
            value *= close[t] / close[t - 1]
        eq.append(value)
    if pos == 1:
        value *= (1 - fee)
        eq[-1] = value
    bench = (close[-1] / open_[0]) * (1 - fee) ** 2
    eq = np.array(eq)
    bench_eq = close / open_[0]
    bench_dd = float(np.min(bench_eq / np.maximum.accumulate(bench_eq) - 1))
    maxdd = float(np.min(eq / np.maximum.accumulate(eq) - 1))
    return {
        "total": value - 1, "bench": bench - 1, "excess": value - bench,
        "maxdd": maxdd, "bench_dd": bench_dd,
        "exposure": float(np.mean(sig.fillna(0).values)),
    }


def main():
    rows = []
    print("加载资产 ...", flush=True)
    data = {}
    for sym in UNIVERSE:
        try:
            df = load_asset(sym)
            if len(df) < 300:
                print(f"  [{sym}] 数据不足 {len(df)}")
                continue
            data[sym] = df
            print(f"  {sym}: {len(df)} 根  {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
        except Exception as e:
            print(f"  [{sym}] 失败: {str(e)[:50]}")
    print(f"\n共 {len(data)} 个资产，{len(STRATEGIES)} 个策略\n")

    for name, fn in STRATEGIES.items():
        exs, dds, bdds, tots, bencs, beats = [], [], [], [], [], 0
        per = {c: {"ex": [], "dd": [], "bdd": []} for c in set(CLASS.values())}
        for sym, df in data.items():
            try:
                sig = fn(df)
                r = backtest(df, sig)
                exs.append(r["excess"])
                dds.append(r["maxdd"])
                bdds.append(r["bench_dd"])
                tots.append(r["total"])
                bencs.append(r["bench"])
                cl = CLASS.get(sym, "other")
                per[cl]["ex"].append(r["excess"])
                per[cl]["dd"].append(r["maxdd"])
                per[cl]["bdd"].append(r["bench_dd"])
                if r["excess"] > 0:
                    beats += 1
            except Exception as e:
                print(f"  [{name}] on {sym} 错误: {str(e)[:60]}")
        n = len(exs)
        if n == 0:
            continue
        rows.append({
            "strategy": name,
            "n_assets": n,
            "mean_excess": float(np.mean(exs)),
            "median_excess": float(np.median(exs)),
            "beat_rate": beats / n,
            "mean_total": float(np.mean(tots)),
            "mean_bench": float(np.mean(bencs)),
            "mean_dd": float(np.mean(dds)),
            "mean_bench_dd": float(np.mean(bdds)),
            "worst_dd": float(np.min(dds)),
            "ex_us_eq": float(np.mean(per["US_equity"]["ex"])) if per["US_equity"]["ex"] else 0,
            "ex_a_idx": float(np.mean(per["A_index"]["ex"])) if per["A_index"]["ex"] else 0,
            "ex_us_lev": float(np.mean(per["US_lev"]["ex"])) if per["US_lev"]["ex"] else 0,
            "dd_us_eq": float(np.mean(per["US_equity"]["dd"])) if per["US_equity"]["dd"] else 0,
            "dd_a_idx": float(np.mean(per["A_index"]["dd"])) if per["A_index"]["dd"] else 0,
        })
        print(f"  {name:<22} 全市场超额 {np.mean(exs)*100:+6.1f}%  "
              f"[美股权益 {np.mean(per['US_equity']['ex'])*100:+6.1f}% | A股 {np.mean(per['A_index']['ex'])*100:+6.1f}%]  "
              f"回撤 {np.mean(dds)*100:5.1f}%")

    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(OUTDIR, "timing_universe.csv"), index=False, encoding="utf-8-sig")
    print(f"\n已保存: {os.path.join(OUTDIR, 'timing_universe.csv')}\n")

    # 综合排序：超额为主，回撤为辅
    print("===== 按平均超额排序（含回撤）=====")
    for _, r in res.sort_values("mean_excess", ascending=False).iterrows():
        print(f"  {r.strategy:<22} 超额 {r.mean_excess*100:+6.1f}%  跑赢 {r.beat_rate*100:3.0f}%  "
              f"策略回撤 {r.mean_dd*100:5.1f}%  基准回撤 {r.mean_bench_dd*100:5.1f}%  最差 {r.worst_dd*100:5.1f}%")

    print("\n===== 分市场看：美股权益 vs A股 的超额 =====")
    for _, r in res.sort_values("mean_excess", ascending=False).iterrows():
        print(f"  {r.strategy:<22} 美股权益 {r.ex_us_eq*100:+6.1f}%  A股 {r.ex_a_idx*100:+6.1f}%  "
              f"杠杆 {r.ex_us_lev*100:+6.1f}%")

    print("\n===== 按回撤排序（含超额）=====")
    for _, r in res.sort_values("mean_dd", ascending=True).iterrows():
        print(f"  {r.strategy:<22} 回撤 {r.mean_dd*100:5.1f}%  超额 {r.mean_excess*100:+6.1f}%  "
              f"跑赢 {r.beat_rate*100:3.0f}%")


if __name__ == "__main__":
    main()
