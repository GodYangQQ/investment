"""A股公司评分：最新业绩 + 回购（行业内百分位，0-100）。

用法：
    from core.scoring.company_score import board_scores, load_scores
    board_scores(name="半导体", top=10)
    board_scores(codes=["688110", "300672"], top=10)
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CACHE_FILE = ROOT / "data" / "company_scores.json"
CACHE_TTL = 6 * 3600
_LOCK = threading.Lock()


def _code6(s) -> str:
    return str(s).strip().zfill(6)


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _pct(s: pd.Series, higher: bool = True) -> pd.Series:
    r = s.rank(pct=True, na_option="keep") * 100
    return r if higher else 100 - r


def fetch_earnings() -> pd.DataFrame:
    import akshare as ak

    for d in ("20260930", "20260630"):
        try:
            df = ak.stock_yjbb_em(date=d)
            if df is not None and len(df):
                df = df.copy()
                df["报告期"] = d
                return df
        except Exception:
            continue
    return pd.DataFrame()


def fetch_repurchase() -> pd.DataFrame:
    import akshare as ak

    try:
        rep = ak.stock_repurchase_em()
    except Exception:
        return pd.DataFrame()
    if rep is None or rep.empty:
        return pd.DataFrame()
    rep = rep.copy()
    rep["代码"] = rep["股票代码"].map(_code6)
    for c in ("已回购金额", "计划回购金额区间-上限", "已回购股份数量"):
        rep[c] = _num(rep[c]).fillna(0) if c in rep.columns else 0
    rep["最新公告日期"] = rep["最新公告日期"].astype(str)
    rep = rep.sort_values("最新公告日期")
    agg = (
        rep.groupby("代码")
        .agg(
            已回购金额=("已回购金额", "sum"),
            计划上限=("计划回购金额区间-上限", "sum"),
            已回购股份=("已回购股份数量", "sum"),
            最新公告日=("最新公告日期", "last"),
            实施进度=("实施进度", "last"),
        )
        .reset_index()
    )
    agg["执行率"] = (agg["已回购金额"] / agg["计划上限"].replace(0, np.nan)).clip(upper=1.5)
    return agg


def build_scores() -> pd.DataFrame:
    yj = fetch_earnings()
    if yj is None or yj.empty:
        return pd.DataFrame()
    yj = yj.copy()
    yj["代码"] = yj["股票代码"].map(_code6)
    yj = yj.rename(
        columns={
            "股票简称": "名称",
            "营业总收入-营业总收入": "营收",
            "营业总收入-同比增长": "营收同比",
            "净利润-净利润": "净利",
            "净利润-同比增长": "净利同比",
            "每股收益": "EPS",
            "净资产收益率": "ROE",
            "销售毛利率": "毛利率",
            "每股经营现金流量": "每股现金流",
            "所处行业": "行业",
        }
    )
    for c in ("营收", "营收同比", "净利", "净利同比", "EPS", "ROE", "毛利率", "每股现金流"):
        yj[c] = _num(yj[c]) if c in yj.columns else np.nan
    yj = yj[~yj["名称"].astype(str).str.contains("ST|退", na=False)]

    rep = fetch_repurchase()
    m = yj.merge(rep, on="代码", how="left")
    for c in ("已回购金额", "计划上限", "已回购股份", "执行率"):
        m[c] = m[c].fillna(0) if c in m.columns else 0
    m["回购/净利"] = np.where(m["净利"] > 0, m["已回购金额"] / m["净利"], np.nan)
    m["回购/净利"] = pd.Series(m["回购/净利"]).clip(lower=0, upper=2.0)
    m["现金含量"] = np.where(m["EPS"] > 0, m["每股现金流"] / m["EPS"], np.nan)
    m["现金含量"] = pd.Series(m["现金含量"]).clip(-10, 10)

    rows = []
    for _ind, g in m.groupby("行业"):
        g = g.copy()
        g["s_growth"] = 0.5 * _pct(g["营收同比"]) + 0.5 * _pct(g["净利同比"])
        g["s_profit"] = 0.5 * _pct(g["ROE"]) + 0.5 * _pct(g["毛利率"])
        g["s_quality"] = _pct(g["现金含量"])
        g["s_buyback"] = 0.6 * _pct(g["回购/净利"]) + 0.4 * _pct(g["执行率"])
        for c in ("s_growth", "s_profit", "s_quality", "s_buyback"):
            g[c] = g[c].fillna(50)
        g["总分"] = (
            0.32 * g["s_growth"]
            + 0.28 * g["s_profit"]
            + 0.18 * g["s_quality"]
            + 0.22 * g["s_buyback"]
        )
        rows.append(g)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def load_scores(force: bool = False) -> pd.DataFrame:
    with _LOCK:
        if not force:
            try:
                with open(CACHE_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                if time.time() - float(d.get("ts", 0)) < CACHE_TTL and d.get("rows"):
                    return pd.DataFrame(d["rows"])
            except Exception:
                pass
        df = build_scores()
        if df is None or df.empty:
            return pd.DataFrame()
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            rows = json.loads(df.to_json(orient="records", force_ascii=False))
            payload = {
                "ts": time.time(),
                "report": str(df["报告期"].iloc[0]) if "报告期" in df.columns else "",
                "rows": rows,
            }
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception:
            pass
        return df


def _records(g: pd.DataFrame, top: int) -> list[dict]:
    g = g.sort_values("总分", ascending=False).head(top)
    out = []
    for _, r in g.iterrows():

        def f(key, nd=1):
            v = r.get(key)
            return round(float(v), nd) if pd.notna(v) else None

        out.append(
            {
                "code": str(r["代码"]),
                "name": str(r["名称"]),
                "score": f("总分"),
                "growth": f("s_growth"),
                "profit": f("s_profit"),
                "quality": f("s_quality"),
                "buyback": f("s_buyback"),
                "revenue_yoy": f("营收同比"),
                "profit_yoy": f("净利同比"),
                "roe": f("ROE"),
                "gross_margin": f("毛利率"),
                "buyback_amount": round(float(r["已回购金额"]) / 1e8, 2),
                "buyback_plan": round(float(r["计划上限"]) / 1e8, 2),
                "exec_rate": round(float(r["执行率"]) * 100, 1),
                "buyback_to_profit": (
                    round(float(r["回购/净利"]) * 100, 1) if pd.notna(r.get("回购/净利")) else None
                ),
                "report": str(r.get("报告期") or ""),
            }
        )
    return out


def board_scores(name: str = "", codes: list[str] | None = None, top: int = 10) -> list[dict]:
    df = load_scores()
    if df is None or df.empty:
        return []
    codes = [str(c).strip().zfill(6) for c in (codes or []) if str(c).strip()]
    if codes:
        g = df[df["代码"].isin(codes)]
        if len(g) >= 5:
            return _records(g, top)
    if name:
        nm = str(name).strip()
        industries = [x for x in df["行业"].dropna().astype(str).unique()]
        exact = [x for x in industries if x == nm]
        cand = exact or [x for x in industries if nm and (nm in x or x in nm)]
        if cand:
            return _records(df[df["行业"] == cand[0]], top)
    return []


def scores_ready() -> bool:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return bool(d.get("rows")) and time.time() - float(d.get("ts", 0)) < CACHE_TTL
    except Exception:
        return False
