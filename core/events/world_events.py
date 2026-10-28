#!/usr/bin/env python3
"""
世界大事件 → 逻辑链 → 候选标的
================================
采集当前全球重大事件（灾害/疫情/地缘/政策等），分类后经知识库逻辑链映射到
可能受影响的美股标的。

数据源（均免费、无需 key）:
  - Google News RSS （按事件关键词检索最新新闻）
  - USGS            （近一周显著地震，带震级）

用法:
    from core.events.world_events import scan_events, map_to_targets
    events = scan_events()
    targets = map_to_targets(events)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests

log = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAINS_PATH = os.path.join(ROOT_DIR, "data", "event_chains.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}

_GOOGLE_NEWS = "https://news.google.com/rss/search"

# 用于扫描的检索词：类别 → (英文query, 中文query)
SCAN_QUERIES = [
    ("earthquake", "earthquake magnitude", "地震"),
    ("hurricane_typhoon", "hurricane OR typhoon landfall", "台风 登陆"),
    ("flood", "flooding disaster", "洪水 灾害"),
    ("drought_el_nino", "El Nino OR drought", "厄尔尼诺 干旱"),
    ("wildfire", "wildfire", "野火 山火"),
    ("heatwave_coldwave", "heatwave OR cold wave record", "极端高温 寒潮"),
    ("pandemic_outbreak", "disease outbreak epidemic", "疫情 传染病"),
    ("war_geopolitics", "war conflict military escalation", "冲突 战争 升级"),
    ("sanctions_chipban", "export control sanction chip", "出口管制 制裁 芯片"),
    ("shipping_disruption", "port strike shipping disruption canal", "港口 罢工 航运中断"),
    ("energy_supply", "OPEC oil gas supply disruption", "原油 天然气 供给 减产"),
    ("cyber_attack", "cyberattack ransomware grid outage", "网络攻击 停电"),
    ("rate_fed", "Federal Reserve interest rate inflation", "美联储 降息 加息 通胀"),
    ("ai_capex", "AI data center capex GPU demand", "AI 算力 数据中心 资本开支"),
]

_CACHE: dict[str, list] = {}


def load_chains() -> dict:
    with open(CHAINS_PATH, encoding="utf-8") as f:
        return json.load(f)["events"]


def _get(url: str, params: Optional[dict] = None, retries: int = 2) -> Optional[str]:
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    log.warning("fetch failed %s: %s", url, last)
    return None


def fetch_google_news(query: str, lang: str = "en", n: int = 8) -> list[dict]:
    """Google News RSS 检索。lang: en | zh"""
    if lang == "zh":
        params = {"q": query, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    else:
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    cache_key = f"{lang}:{query}"
    if cache_key in _CACHE:
        return _CACHE[cache_key][:n]
    text = _get(_GOOGLE_NEWS, params=params)
    if not text:
        _CACHE[cache_key] = []
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        _CACHE[cache_key] = []
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate") or ""
        src_node = item.find("source")
        source = (src_node.text if src_node is not None else "") or ""
        when = None
        try:
            when = parsedate_to_datetime(pub)
        except Exception:
            pass
        if title and link:
            items.append({"title": title, "link": link, "source": source, "published": when})
    _CACHE[cache_key] = items
    return items[:n]


def fetch_usgs_quakes(min_mag: float = 5.0, days: str = "week") -> list[dict]:
    """USGS 显著地震。"""
    url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_{days}.geojson"
    text = _get(url)
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        mag = props.get("mag")
        if mag is None or mag < min_mag:
            continue
        place = props.get("place") or ""
        when = None
        if props.get("time"):
            when = datetime.fromtimestamp(props["time"] / 1000, tz=timezone.utc)
        out.append({
            "title": f"M{mag:.1f} earthquake - {place}",
            "link": props.get("url", ""),
            "source": "USGS",
            "published": when,
            "magnitude": mag,
            "place": place,
        })
    out.sort(key=lambda x: (x.get("magnitude") or 0), reverse=True)
    return out


def _classify(title: str, chains: dict) -> list[tuple[str, str]]:
    """返回 [(category, region), ...]"""
    low = title.lower()
    hits = []
    for cat, spec in chains.items():
        for kw in spec.get("keywords", []):
            if kw.lower() in low:
                region = ""
                for reg, terms in (spec.get("regions") or {}).items():
                    if any(t.lower() in low for t in terms):
                        region = reg
                        break
                hits.append((cat, region))
                break
    return hits


def scan_events(chains: Optional[dict] = None, per_query: int = 8,
                include_zh: bool = True, include_quakes: bool = True) -> list[dict]:
    """扫描当前世界大事件。"""
    chains = chains or load_chains()
    events = []
    seen = set()

    for cat, q_en, q_zh in SCAN_QUERIES:
        news = fetch_google_news(q_en, lang="en", n=per_query)
        if include_zh:
            news += fetch_google_news(q_zh, lang="zh", n=max(3, per_query // 2))
        for it in news:
            title = it["title"]
            key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", title.lower())[:80]
            if key in seen:
                continue
            seen.add(key)
            cats = _classify(title, chains)
            if not cats:
                continue
            for c, region in cats:
                events.append({
                    "category": c,
                    "category_cn": chains[c]["name_cn"],
                    "region": region,
                    "title": title,
                    "source": it.get("source", ""),
                    "link": it.get("link", ""),
                    "published": it.get("published"),
                })

    if include_quakes:
        for q in fetch_usgs_quakes(min_mag=5.0):
            cats = _classify(q["title"], chains) or [("earthquake", "")]
            for c, region in cats:
                events.append({
                    "category": c,
                    "category_cn": chains[c]["name_cn"],
                    "region": region,
                    "title": q["title"],
                    "source": "USGS",
                    "link": q["link"],
                    "published": q.get("published"),
                    "magnitude": q.get("magnitude"),
                })

    # 按时间倒排（无时间排后面）
    events.sort(key=lambda e: e.get("published") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return events


def map_to_targets(events: list[dict], chains: Optional[dict] = None) -> list[dict]:
    """
    事件 → 逻辑链 → 候选标的。聚合多事件证据，输出排序后的候选。
    返回 [{ticker, direction, score, reasons:[...], categories:[...]}, ...]
    """
    chains = chains or load_chains()
    agg: dict[tuple, dict] = {}

    def add(ticker, direction, why, cat, cat_cn):
        key = (ticker, direction)
        node = agg.setdefault(key, {
            "ticker": ticker, "direction": direction, "score": 0,
            "categories": [], "reasons": [],
        })
        if cat not in node["categories"]:
            node["categories"].append(cat)
            node["score"] += 1
        reason = f"[{cat_cn}] {why}"
        if reason not in node["reasons"]:
            node["reasons"].append(reason)

    for ev in events:
        cat = ev["category"]
        spec = chains.get(cat)
        if not spec:
            continue
        for item in spec.get("long", []):
            add(item["ticker"], "long", item["why"], cat, spec["name_cn"])
        for item in spec.get("short", []):
            add(item["ticker"], "short", item["why"], cat, spec["name_cn"])

    results = list(agg.values())
    # 半导体相关加分
    semi_cats = {c for c, s in chains.items() if s.get("semi")}
    for r in results:
        if set(r["categories"]) & semi_cats:
            r["score"] += 0.5
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def summarize_categories(events: list[dict], chains: Optional[dict] = None) -> list[dict]:
    chains = chains or load_chains()
    counts: dict[str, dict] = {}
    for ev in events:
        c = ev["category"]
        node = counts.setdefault(c, {
            "category": c, "name_cn": chains.get(c, {}).get("name_cn", c),
            "count": 0, "latest": None, "samples": [],
        })
        node["count"] += 1
        if node["latest"] is None or (ev.get("published") and ev["published"] > node["latest"]):
            node["latest"] = ev.get("published")
        if len(node["samples"]) < 2:
            node["samples"].append(ev["title"])
    return sorted(counts.values(), key=lambda x: x["count"], reverse=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    evs = scan_events(per_query=5)
    print(f"扫描到 {len(evs)} 条相关事件\n")
    for c in summarize_categories(evs):
        print(f"- {c['name_cn']} x{c['count']}")
        for s in c["samples"]:
            print(f"    · {s[:90]}")
    print("\n候选标的 Top 15:")
    for t in map_to_targets(evs)[:15]:
        print(f"  {t['ticker']:<6} {'多' if t['direction']=='long' else '空'}  分{t['score']:.1f}  {t['reasons'][0]}")
