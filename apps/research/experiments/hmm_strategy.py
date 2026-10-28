#!/usr/bin/env python3
"""
西蒙斯式「隐马尔可夫市场状态」策略（HMM regime timing）
======================================================
灵感来自 Renaissance 传闻用 Baum–Welch/HMM 识别隐藏市场状态：
  拟合一个 2 状态高斯 HMM，把每天归类为「牛/平静」或「熊/高波动」，
  用「已实现状态概率」决定持有杠杆还是空仓。

诚实说明：这只是「HMM 择时」的简化版，远非 Medallion（那需要数千标的+
日内+海量另类数据）。这里只验证「HMM 识别 regime → 做多/空仓」这个逻辑
在 SOXL/SOXX 上有没有用。

零未来函数：滚动+扩张窗口重拟合，只用 t 之前的数据。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.datasets.us_data import fetch_daily_kline  # noqa: E402
from cn_backtest import backtest  # noqa: E402


class GaussianHMM:
    """1维高斯观测、离散隐状态 HMM，Baum–Welch(EM) 拟合。"""

    def __init__(self, n_states=2, n_iter=60, tol=1e-4, seed=0):
        self.K = n_states
        self.n_iter = n_iter
        self.tol = tol
        self.rng = np.random.default_rng(seed)

    def _emission(self, x):
        # x: (T,1) -> (T,K) log emission
        x = np.asarray(x, dtype=float).reshape(-1, 1)
        T = x.shape[0]
        logb = np.empty((T, self.K))
        for k in range(self.K):
            m, v = self.mu[k], self.var[k]
            logb[:, k] = -0.5 * np.log(2 * np.pi * v) - (x[:, 0] - m) ** 2 / (2 * v)
        return logb

    def _forward(self, logb):
        T = logb.shape[0]
        alpha = np.zeros((T, self.K))
        scale = np.zeros(T)
        alpha[0] = self.pi * np.exp(logb[0])
        scale[0] = alpha[0].sum()
        alpha[0] /= scale[0]
        for t in range(1, T):
            alpha[t] = np.exp(logb[t]) * (alpha[t - 1] @ self.A)
            scale[t] = alpha[t].sum()
            alpha[t] /= scale[t]
        return alpha, scale

    def _backward(self, logb, scale):
        T = logb.shape[0]
        beta = np.zeros((T, self.K))
        beta[-1] = 1.0
        for t in range(T - 2, -1, -1):
            beta[t] = self.A @ (np.exp(logb[t + 1]) * beta[t + 1])
            beta[t] /= scale[t]
        return beta

    def fit(self, X):
        X = np.asarray(X, dtype=float).reshape(-1, 1)
        T = X.shape[0]
        # 初始化：按分位数分两段估计均值
        lo = X[:T // 2].mean() if T > 2 else X.mean()
        hi = X[T // 2:].mean() if T > 2 else X.mean()
        self.mu = np.array([lo, hi])
        self.var = np.array([X.var() + 1e-6, X.var() + 1e-6])
        self.A = np.full((self.K, self.K), 0.5 / (self.K - 1))
        np.fill_diagonal(self.A, 0.9)
        self.A /= self.A.sum(axis=1, keepdims=True)
        self.pi = np.full(self.K, 1.0 / self.K)

        prev = -np.inf
        for _ in range(self.n_iter):
            logb = self._emission(X)
            alpha, scale = self._forward(logb)
            beta = self._backward(logb, scale)
            gamma = alpha * beta
            gamma /= gamma.sum(axis=1, keepdims=True)
            # xi
            xi = np.zeros((T - 1, self.K, self.K))
            for t in range(T - 1):
                xi[t] = (alpha[t][:, None] * self.A) * (np.exp(logb[t + 1]) * beta[t + 1])[None, :]
                xi[t] /= xi[t].sum()
            # M-step
            self.pi = gamma[0]
            self.A = xi.sum(axis=0) / gamma[:-1].sum(axis=0)[:, None]
            self.A /= self.A.sum(axis=1, keepdims=True)
            for k in range(self.K):
                g = gamma[:, k]
                self.mu[k] = (g * X[:, 0]).sum() / g.sum()
                self.var[k] = (g * (X[:, 0] - self.mu[k]) ** 2).sum() / g.sum() + 1e-6
            # 状态按均值排序（0=低均值, 1=高均值）
            order = np.argsort(self.mu)
            self.mu = self.mu[order]
            self.var = self.var[order]
            self.A = self.A[np.ix_(order, order)]
            # log-likelihood
            ll = np.log(scale).sum()
            if abs(ll - prev) < self.tol:
                break
            prev = ll
        return self

    def filtered_last(self, X):
        """因果：给定序列 X(0..T-1)，返回最后一刻各状态后验概率。"""
        logb = self._emission(np.asarray(X, dtype=float).reshape(-1, 1))
        alpha, _ = self._forward(logb)
        return alpha[-1]


def hmm_signal(logret, train_n=400, refit_every=20):
    """返回 0/1 信号序列（1=持有）。用扩张窗口重拟合 + 滤波后验概率。"""
    T = len(logret)
    sig = np.zeros(T)
    model = None
    good_state = 1  # 高均值状态
    for t in range(T):
        if t < train_n:
            continue
        if model is None or (t - train_n) % refit_every == 0:
            model = GaussianHMM(n_states=2).fit(logret[:t])
        p = model.filtered_last(logret[:t])
        prob_good = p[good_state] if good_state == 1 else p[good_state]
        sig[t] = 1 if prob_good >= 0.5 else 0
    return sig


def main():
    for lev, base in [("SOXL", "SOXX")]:
        dlev = fetch_daily_kline(lev, days=1600).set_index("date").sort_index()
        dbase = fetch_daily_kline(base, days=1600).set_index("date").sort_index()
        idx = dlev.index.intersection(dbase.index)
        dlev, dbase = dlev.loc[idx], dbase.loc[idx]

        close = dlev["close"]
        open_ = dlev["open"]
        logret = np.log(close / close.shift(1)).replace([np.inf, -np.inf], np.nan).fillna(0).values

        sig = hmm_signal(logret)

        df = pd.DataFrame({"open": open_.values, "close": close.values})
        r = backtest(df, pd.Series(sig), 0, len(df) - 1)
        print(f"\n===== {lev} HMM择时 (2状态, 训练{400}根, 每{20}根重拟合) =====")
        print(f"  策略收益 {r.total*100:+.1f}%   买入持有 {r.bench*100:+.1f}%   超额 {r.excess*100:+.1f}%")
        print(f"  策略回撤 {r.maxdd*100:.1f}%   基准回撤 {r.bench_maxdd*100:.1f}%   "
              f"交易 {r.trades}次  胜率 {r.winrate*100:.0f}%  在场 {r.exposure*100:.0f}%")

        # 逐年
        d = df.copy()
        d["year"] = dlev.index.year
        print("  逐年(策略/基准):")
        for y, g in d.groupby("year"):
            i0, i1 = g.index[0], g.index[-1]
            if i1 - i0 < 5:
                continue
            ry = backtest(df, pd.Series(sig), i0, i1)
            print(f"    {y}: 策略 {ry.total*100:+6.1f}%  基准 {ry.bench*100:+6.1f}%  超额 {ry.excess*100:+6.1f}%")


if __name__ == "__main__":
    main()
