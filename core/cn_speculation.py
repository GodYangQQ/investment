#!/usr/bin/env python3
"""
A股短线炒作 pipeline
====================
世界/国内大事件 → 炒作逻辑链 → 题材模块(概念板块) → 模块内前3龙头

设计：
  1) 事件来自 Google News（中英）+ 知识库关键词匹配
  2) 事件类别映射到候选"模块"（数据在 data/cn_event_chains.json）
  3) 每个模块用腾讯行情/K线对候选股按"炒作强度"实时排序，取前3

用法:
    from cn_speculation import run_pipeline
    result = run_pipeline(per_query=6, top_stocks=3)
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

import world_events as we
import global_events as ge
import cn_data as cd

log = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAINS_PATH = os.path.join(ROOT_DIR, "data", "cn_event_chains.json")

_STOCK_METRIC_CACHE: dict[str, dict] = {}

# 全球级显著性门槛：命中这些"强信号"词才算重大事件（排除日常食品召回等噪音）
STRONG_TERMS = [
    "pandemic", "epidemic", "plague", "bubonic", "pneumonic", "sars", "mers",
    "ebola", "cholera", "dengue", "nipah", "polio", "measles", "diphtheria",
    "public health emergency", "health emergency", "who declares", "global health",
    "el nino", "el niño", "la nina", "la niña", "enso", "famine", "locust",
    "food crisis", "crop failure", "drought", "wildfire", "megafire",
    "earthquake", "tsunami", "hurricane", "typhoon", "cyclone", "volcanic",
    "war", "invasion", "missile", "nuclear", "armed conflict", "ceasefire",
    "sanction", "export control", "entity list", "chip ban", "tariff",
    "opec", "oil supply", "gas supply", "pipeline", "lng",
    "shipping", "port strike", "blockade", "canal", "freight",
    "cyberattack", "ransomware", "blackout", "grid outage",
    "interest rate", "rate cut", "rate hike", "federal reserve", "recession",
    "data center", "gpu demand", "ai capex", "chip shortage",
    "疫苗", "疫情", "传染病", "鼠疫", "非典", "霍乱", "埃博拉", "登革热",
    "地震", "台风", "飓风", "洪水", "干旱", "野火", "火山", "海啸",
    "战争", "冲突", "制裁", "出口管制", "航运", "运价", "停产", "断电",
]


def _kw_match(text_low: str, kw: str) -> bool:
    """ASCII 关键词用词边界匹配（避免 war 命中 warns），中文用子串。"""
    kw = kw.lower()
    if _has_cjk(kw):
        return kw in text_low
    return re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", text_low) is not None


def _is_significant(title: str) -> bool:
    low = title.lower()
    return any(_kw_match(low, t) for t in STRONG_TERMS)


def load_cn_chains() -> dict:
    with open(CHAINS_PATH, encoding="utf-8") as f:
        return json.load(f)["events"]


def _has_cjk(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s))


def _build_global_queries(chains: dict, limit: int = 26) -> list[str]:
    """从各类别的英文关键词生成 GDELT 全球检索词。"""
    qs = []
    for cat, spec in chains.items():
        for kw in spec.get("keywords", []):
            if not _has_cjk(kw) and kw.lower() not in [q.lower() for q in qs]:
                qs.append(kw)
                break
    return qs[:limit]


def _timespan_days(timespan: str) -> int:
    m = re.match(r"(\d+)d", str(timespan))
    return int(m.group(1)) if m else 14


def scan_cn_events(chains: dict, per_query: int = 6, use_global: bool = True,
                   timespan: str = "14d", recency_only: bool = True) -> list[dict]:
    """
    事件扫描：优先全球权威源（GDELT/WHO/CDC/FAO/USGS），
    再按 CN 知识库关键词分类。散户向的 Google News 仅作兜底。
    """
    events = []
    seen = set()

    cutoff = datetime.now(timezone.utc) - timedelta(days=_timespan_days(timespan))

    def _ingest(items: list[dict]):
        for it in items:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            # 时间窗过滤：丢弃陈旧存档（官方RSS常混入多年前内容）
            pub = it.get("published")
            if recency_only and pub is not None and pub < cutoff:
                continue
            # 全球显著性门槛：非重大事件直接丢弃
            if not _is_significant(title):
                continue
            key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", title.lower())[:80]
            if key in seen:
                continue
            low = title.lower()
            for cat, spec in chains.items():
                if any(_kw_match(low, k) for k in spec.get("keywords", [])):
                    seen.add(key)
                    events.append({
                        "category": cat, "category_cn": spec["name_cn"],
                        "title": title, "source": it.get("source", ""),
                        "link": it.get("link", ""), "published": it.get("published"),
                        "authority": it.get("authority", "other"),
                        "country": it.get("country", ""),
                    })
                    break

    if use_global:
        queries = _build_global_queries(chains)
        items = ge.fetch_global_items(queries=queries, timespan=timespan, per_query=20)
        _ingest(items)

    # 兜底/补充：中文关键词走 Google News（仅当全球源命中偏少）
    if len(events) < 20:
        for cat, spec in chains.items():
            for q in list(dict.fromkeys(spec.get("keywords", [])))[:3]:
                lang = "zh" if _has_cjk(q) else "en"
                _ingest(we.fetch_google_news(q, lang=lang, n=per_query))

    a_rank = {"official": 0, "tier1": 1, "other": 2}
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    events.sort(key=lambda e: (a_rank.get(e.get("authority"), 3),
                               -(e.get("published") or epoch).timestamp()))
    return events


def _stock_metrics(code: str, quote: Optional[dict] = None) -> dict:
    """用腾讯行情/K线计算炒作强度相关指标。"""
    if code in _STOCK_METRIC_CACHE:
        return _STOCK_METRIC_CACHE[code]
    q = quote or cd.get_quote(code)
    df = cd.get_kline(code, 40)
    m = {
        "code": code, "name": q.get("name", ""), "price": q.get("price"),
        "pct": q.get("pct_change", 0.0), "turnover": q.get("turnover", 0.0),
        "pe": q.get("pe", 0.0),
        "ret5": None, "ret20": None, "vol_ratio": None, "consec_limit": 0,
    }
    if df is not None and len(df) >= 6:
        c = np.asarray(df["close"].values, dtype=float)
        v = np.asarray(df["volume"].values, dtype=float)
        m["ret5"] = float(c[-1] / c[-6] - 1) * 100 if c[-6] else None
        if len(c) >= 21:
            m["ret20"] = float(c[-1] / c[-21] - 1) * 100 if c[-21] else None
        seg = v[-6:-1] if len(v) >= 6 else v
        avg5 = float(sum(seg) / len(seg)) if len(seg) else 0.0
        m["vol_ratio"] = float(v[-1] / avg5) if avg5 else None
        limit = 19.8 if str(code).startswith(("30", "68")) else 9.8
        # 连续涨停（近5日）
        consec = 0
        for i in range(len(c) - 1, 0, -1):
            if c[i - 1] and (c[i] / c[i - 1] - 1) * 100 >= limit * 0.98:
                consec += 1
            else:
                break
        m["consec_limit"] = consec
    _STOCK_METRIC_CACHE[code] = m
    return m


def spec_score(m: dict) -> float:
    """炒作强度评分（短线情绪）。"""
    pct = m.get("pct") or 0.0
    vr = m.get("vol_ratio") or 1.0
    consec = m.get("consec_limit") or 0
    ret5 = m.get("ret5") or 0.0
    ret20 = m.get("ret20") or 0.0
    limit = 19.8 if str(m.get("code", "")).startswith(("30", "68")) else 9.8
    score = pct * 2.0 + min(vr, 4.0) * 5.0 + consec * 8.0 + ret5 * 0.8 + ret20 * 0.2
    if pct >= limit * 0.98:
        score += 6
    if (m.get("turnover") or 0) < 1.0:
        score -= 3  # 缩量/停牌
    return round(score, 1)


def rank_module_stocks(module: dict, top: int = 3, expand: bool = False) -> list[dict]:
    """模块内候选股按炒作强度排序取前 top。"""
    pool = {s["code"]: s["name"] for s in module.get("leaders", [])}
    if expand:
        for kw in module.get("board_kw", []):
            for s in cd.get_concept_cons_em(kw):
                if s.get("code") and not cd.is_excluded(s["code"], s.get("name", "")):
                    pool.setdefault(s["code"], s.get("name", ""))
    codes = [c for c in pool if not cd.is_excluded(c, pool.get(c, ""))]
    if not codes:
        return []
    quotes = cd.get_quotes_batch(codes)
    rows = []
    for c in codes:
        q = quotes.get(c)
        live_name = (q or {}).get("name", "") or pool.get(c, "")
        if cd.is_excluded(c, live_name):  # 用实时名过滤 ST/退市
            continue
        m = _stock_metrics(c, q)
        m["name"] = live_name or m.get("name", "")
        m["score"] = spec_score(m)
        m["reasons"] = module.get("logic", "")
        rows.append(m)
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:top]


def run_pipeline(per_query: int = 6, top_modules: int = 10, top_stocks: int = 3,
                 only_category: Optional[str] = None, only_module: Optional[str] = None,
                 expand_pool: bool = False, use_global: bool = True,
                 timespan: str = "14d") -> dict:
    """执行完整 pipeline，返回 {events, modules(含 top 股票)}."""
    chains = load_cn_chains()

    # Stage 1: 事件 → 模块（按事件热度 + 权威度排序）
    events = scan_cn_events(chains, per_query=per_query, use_global=use_global, timespan=timespan)
    cat_hits: dict[str, list] = {}
    for ev in events:
        cat_hits.setdefault(ev["category"], []).append(ev)

    modules = []
    cats = [only_category] if only_category else list(cat_hits.keys())
    for cat in cats:
        spec = chains.get(cat)
        if not spec:
            continue
        hits = cat_hits.get(cat, [])
        for mod in spec["modules"]:
            if only_module and only_module not in mod["name"]:
                continue
            modules.append({
                "category": cat, "category_cn": spec["name_cn"],
                "name": mod["name"], "logic": mod.get("logic", ""),
                "board_kw": mod.get("board_kw", []),
                "leaders": mod.get("leaders", []),
                "event_hits": len(hits),
                "samples": [e["title"] for e in hits[:2]],
            })

    # 去重模块（不同事件可能命中同一模块）
    dedup: dict[str, dict] = {}
    for m in modules:
        key = m["name"]
        if key in dedup:
            dedup[key]["event_hits"] += m["event_hits"]
            dedup[key]["categories"].append(m["category_cn"])
            for s in m["samples"]:
                if s not in dedup[key]["samples"]:
                    dedup[key]["samples"].append(s)
        else:
            m["categories"] = [m["category_cn"]]
            dedup[key] = m
    modules = list(dedup.values())

    # Stage 2: 每个模块取前 top_stocks
    for m in modules:
        m["top_stocks"] = rank_module_stocks(m, top=top_stocks, expand=expand_pool)
        best = m["top_stocks"][0]["score"] if m["top_stocks"] else 0
        # 权威度加权：官方源命中 > 一级媒体 > 其他
        auths = [e.get("authority") for e in cat_hits.get(m["category"], [])]
        auth_bonus = 6 if "official" in auths else (3 if "tier1" in auths else 0)
        m["authority_bonus"] = auth_bonus
        hits_capped = min(m["event_hits"], 15)  # 封顶，避免事件数淹没有效性
        m["module_score"] = round(hits_capped * 3 + best + auth_bonus, 1)

    modules.sort(key=lambda x: x["module_score"], reverse=True)
    return {"events": events, "event_cats": cat_hits, "modules": modules[:top_modules]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    res = run_pipeline(per_query=4, top_modules=6)
    print(f"命中 {len(res['events'])} 条事件, {len(res['modules'])} 个模块\n")
    for m in res["modules"]:
        print(f"## {m['name']}  [{m['category_cn']}]  模块分 {m['module_score']}  事件×{m['event_hits']}")
        print(f"   逻辑: {m['logic']}")
        for s in m["top_stocks"]:
            print(f"   - {s['code']} {s['name']}  {s['pct']:+.1f}%  炒作分{s['score']}  连板{s['consec_limit']}  量比{s['vol_ratio']}")
