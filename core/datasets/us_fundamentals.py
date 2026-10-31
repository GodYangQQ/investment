"""美股个股财报（SEC XBRL，官方免费，无需 key）。

数据来源：https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
从中抽取最近 N 个季度的营收 / 净利润 / 稀释 EPS，并计算营收同比。
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
FACTS_DIR = DATA_DIR / "us_sec_facts"
TICKERS_FILE = DATA_DIR / "us_sec_tickers.json"

SEC_HEADERS = {
    # SEC 要求带联系方式的 UA，浏览器 UA 会被 403
    "User-Agent": "godyang-investment-research godyang666@qq.com",
    "Accept-Encoding": "gzip, deflate",
}
TIMEOUT = 30
FACTS_TTL = 86400  # 财报数据一天更新一次足够

# 不同公司用的营收科目不一样，按优先级尝试
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]


def _get(url: str):
    import requests

    last = None
    for _ in range(3):
        try:
            return requests.get(url, headers=SEC_HEADERS, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    raise last


def ticker_map(ttl_days: int = 7) -> dict[str, dict]:
    """ticker → {cik, name}；优先用 news_feed 写好的缓存文件。"""
    now = time.time()
    if TICKERS_FILE.exists() and now - TICKERS_FILE.stat().st_mtime < ttl_days * 86400:
        try:
            raw = json.loads(TICKERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    else:
        raw = (_get("https://www.sec.gov/files/company_tickers.json").json() or {})
        try:
            TICKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
            TICKERS_FILE.write_text(json.dumps(raw), encoding="utf-8")
        except Exception:
            pass
    out: dict[str, dict] = {}
    for _, v in (raw or {}).items():
        tk = str(v.get("ticker") or "").upper()
        if tk:
            out[tk] = {
                "cik": str(v.get("cik_str") or "").zfill(10),
                "name": str(v.get("title") or tk),
            }
    return out


def company_facts(ticker: str, ttl: int = FACTS_TTL) -> dict:
    """抓公司全部 XBRL facts，本地缓存（AMD 约 3.6MB）。"""
    info = ticker_map().get(ticker.upper())
    if not info:
        raise ValueError(f"未知 ticker: {ticker}")
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = FACTS_DIR / f"CIK{info['cik']}.json"
    now = time.time()
    if path.exists() and now - path.stat().st_mtime < ttl:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    r = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{info['cik']}.json")
    d = r.json() or {}
    try:
        path.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass
    return d


def _pick_concept(gaap: dict, tags: list[str], unit: str) -> list[dict]:
    """按优先级合并多个科目（不同公司用的 tag 不同，如 NetIncomeLoss / ProfitLoss）。"""
    out: list[dict] = []
    seen: set[tuple] = set()
    for tag in tags:
        node = gaap.get(tag) or {}
        for x in (node.get("units") or {}).get(unit) or []:
            key = (x.get("start"), x.get("end"), x.get("val"))
            if key in seen:
                continue
            seen.add(key)
            out.append(x)
    return out


def _quarterly(rows: list[dict], min_days: int = 60, max_days: int = 110) -> list[dict]:
    """只保留单季度（约 90 天）数据，同一报告期取最新一次申报。"""
    by_end: dict[str, dict] = {}
    for x in rows:
        if x.get("form") not in ("10-Q", "10-K", "6-K", "20-F"):
            continue
        s, e = str(x.get("start") or ""), str(x.get("end") or "")
        if not s or not e:
            continue
        try:
            days = (date.fromisoformat(e) - date.fromisoformat(s)).days
        except Exception:
            continue
        if not (min_days <= days <= max_days):
            continue
        prev = by_end.get(e)
        if prev is None or str(x.get("filed") or "") >= str(prev.get("filed") or ""):
            by_end[e] = x
    return [by_end[k] for k in sorted(by_end, reverse=True)]


def _by_end(rows: list[dict], min_days: int = 60, max_days: int = 110) -> dict[str, dict]:
    return {str(x["end"]): x for x in _quarterly(rows, min_days, max_days)}


def _annual(rows: list[dict]) -> dict[str, dict]:
    """全年数据（10-K/20-F，约 365 天）。"""
    out: dict[str, dict] = {}
    for x in rows:
        s, e = str(x.get("start") or ""), str(x.get("end") or "")
        if not s or not e:
            continue
        try:
            days = (date.fromisoformat(e) - date.fromisoformat(s)).days
        except Exception:
            continue
        if not (350 <= days <= 380):
            continue
        prev = out.get(e)
        if prev is None or str(x.get("filed") or "") >= str(prev.get("filed") or ""):
            out[e] = x
    return out


def _fill_q4(quarterly: dict[str, dict], annual: dict[str, dict]) -> None:
    """10-K 通常只有全年数：用「全年 - 前三季」补出 Q4。"""
    for end, a in annual.items():
        if end in quarterly or a.get("val") is None:
            continue
        try:
            fy_end = date.fromisoformat(end)
        except Exception:
            continue
        qs = []
        for e, q in quarterly.items():
            try:
                gap = (fy_end - date.fromisoformat(e)).days
            except Exception:
                continue
            if 0 < gap < 300:
                qs.append(q)
        if len(qs) != 3 or any(q.get("val") is None for q in qs):
            continue
        quarterly[end] = {
            "start": str(a.get("start") or ""),
            "end": end,
            "val": float(a["val"]) - sum(float(q["val"]) for q in qs),
            "fy": a.get("fy"),
            "fp": "Q4",
            "form": "10-K",
            "filed": a.get("filed"),
            "derived": True,
        }


def us_earnings(ticker: str, quarters: int = 8) -> dict:
    """最近 N 个季度的营收 / 净利润 / EPS + 同比。"""
    tk = ticker.upper().strip()
    info = ticker_map().get(tk)
    if not info:
        raise ValueError(f"未知 ticker: {ticker}")
    facts = company_facts(tk)
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}

    rev_rows = _pick_concept(gaap, REVENUE_TAGS, "USD")
    ni_rows = _pick_concept(gaap, NET_INCOME_TAGS, "USD")
    eps_rows = _pick_concept(gaap, EPS_TAGS, "USD/shares")

    rev = _by_end(rev_rows)
    ni = _by_end(ni_rows)
    eps = _by_end(eps_rows, min_days=0, max_days=120)
    _fill_q4(rev, _annual(rev_rows))
    _fill_q4(ni, _annual(ni_rows))
    _fill_q4(eps, _annual(eps_rows))

    ends = sorted(set(rev) | set(ni) | set(eps), reverse=True)[: max(1, quarters)]
    out = []
    for e in ends:
        r = rev.get(e) or {}
        n = ni.get(e) or {}
        p = eps.get(e) or {}
        # 同比：去年同一季度（报告期相差 350~380 天）
        yoy = None
        try:
            d = date.fromisoformat(e)
            for e2, r2 in rev.items():
                gap = (d - date.fromisoformat(e2)).days
                if 350 <= gap <= 380 and r2.get("val"):
                    yoy = round((float(r.get("val") or 0) / float(r2["val"]) - 1) * 100, 1)
                    break
        except Exception:
            pass
        out.append(
            {
                "period": e,
                "fy": r.get("fy") or n.get("fy"),
                "fp": r.get("fp") or n.get("fp"),
                "form": r.get("form") or n.get("form") or p.get("form"),
                "filed": r.get("filed") or n.get("filed") or p.get("filed"),
                "revenue": r.get("val"),
                "net_income": n.get("val"),
                "eps": p.get("val"),
                "revenue_yoy_pct": yoy,
            }
        )
    return {"ticker": tk, "name": info["name"], "cik": info["cik"], "quarters": out}


def fmt_money(v) -> str:
    if v is None:
        return "-"
    try:
        v = float(v)
    except Exception:
        return "-"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:.1f}M"
    return f"{sign}${v:,.0f}"


def format_earnings(d: dict) -> str:
    lines = [f"{d['ticker']} · {d['name']}（CIK {d['cik']}）", ""]
    lines.append(f"{'报告期':<10} {'类型':<5} {'营收':>9} {'同比':>7} {'净利润':>9} {'EPS':>7}")
    for q in d.get("quarters") or []:
        yoy = q.get("revenue_yoy_pct")
        eps = q.get("eps")
        yoy_s = f"{yoy:+.1f}%" if yoy is not None else "-"
        eps_s = f"{eps:.2f}" if eps is not None else "-"
        lines.append(
            f"{q['period']:<10} {str(q.get('fp') or ''):<5} "
            f"{fmt_money(q.get('revenue')):>9} {yoy_s:>7} "
            f"{fmt_money(q.get('net_income')):>9} {eps_s:>7}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    for t in sys.argv[1:] or ["AMD"]:
        print(format_earnings(us_earnings(t)))
        print()
