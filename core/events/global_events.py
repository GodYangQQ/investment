#!/usr/bin/env python3
"""
全球权威事件层
==============
只采集"全球级别/权威来源"的事件信号，不取散户向资讯。

来源:
  1) GDELT 2.0 DOC API  —— 覆盖 65 种语言、全球主流媒体的事件数据库（可查 tone/国家/域名）
  2) 官方机构 RSS        —— WHO / CDC / FAO 等
  3) USGS                —— 全球显著地震（权威）
  4) Google News         —— 仅限权威域名（site:）兜底
  5) Bluesky 公共 API    —— "大佬发声"（WHO/UN/NOAA 等账号，能连则用）

用法:
    from core.events.global_events import fetch_global_items
    items = fetch_global_items(["pandemic", "el nino", "plague"])
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}

# 权威域名分级
OFFICIAL_DOMAINS = {
    "who.int", "cdc.gov", "ecdc.europa.eu", "fao.org", "wmo.int", "un.org",
    "reliefweb.int", "worldbank.org", "imf.org", "noaa.gov", "usgs.gov",
    "nasa.gov", "nih.gov", "europa.eu", "oecd.org", "iea.org", "unhcr.org",
}
TIER1_MEDIA = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "bloomberg.com",
    "ft.com", "wsj.com", "nytimes.com", "washingtonpost.com", "cnbc.com",
    "nature.com", "science.org", "thelancet.com", "nejm.org", "aljazeera.com",
    "dw.com", "france24.com", "nhk.or.jp", "scmp.com", "economist.com",
    "xinhuanet.com", "news.cn", "cctv.com", "globaltimes.cn",
}
AUTHORITY_DOMAINS = OFFICIAL_DOMAINS | TIER1_MEDIA

# 官方 RSS（能连则采，失败跳过）
OFFICIAL_FEEDS = [
    ("WHO", "https://www.who.int/rss-feeds/news-english.xml", "health"),
    ("CDC", "https://tools.cdc.gov/api/v2/resources/media/132608.rss", "health"),
    ("FAO", "https://www.fao.org/feeds/fao-newsroom-rss", "food"),
]

_GLOBAL_QUERIES_DEFAULT = [
    "pandemic", "epidemic outbreak", "plague", "cholera", "el nino",
    "la nina", "famine", "locust", "wildfire", "flood disaster",
    "earthquake", "hurricane", "tropical cyclone", "drought",
    "war conflict", "sanctions export control", "semiconductor shortage",
    "oil supply OPEC", "shipping crisis", "cyberattack infrastructure",
    "AI data center capex", "interest rate federal reserve",
]

_last_gdelt = [0.0]
_cache: dict[str, list] = {}


def _throttle_gdelt(min_gap: float = 5.5):
    gap = time.time() - _last_gdelt[0]
    if gap < min_gap:
        time.sleep(min_gap - gap)
    _last_gdelt[0] = time.time()


_NAME_AUTHORITY = {
    "reuters": "tier1", "associated press": "tier1", "ap": "tier1", "apnews": "tier1",
    "bbc": "tier1", "bbc news": "tier1", "bloomberg": "tier1",
    "financial times": "tier1", "the wall street journal": "tier1", "wsj": "tier1",
    "the new york times": "tier1", "cnbc": "tier1", "al jazeera": "tier1",
    "deutsche welle": "tier1", "france 24": "tier1", "scmp": "tier1",
    "world health organization": "official", "who": "official",
    "united nations": "official", "un news": "official",
    "centers for disease control and prevention": "official", "cdc": "official",
    "fao": "official", "noaa": "official", "usgs": "official", "ecdc": "official",
}


def authority_of(domain: str) -> str:
    d = (domain or "").lower().lstrip("www.").strip()
    for od in OFFICIAL_DOMAINS:
        if d == od or d.endswith("." + od):
            return "official"
    for td in TIER1_MEDIA:
        if d == td or d.endswith("." + td):
            return "tier1"
    if d in _NAME_AUTHORITY:
        return _NAME_AUTHORITY[d]
    return "other"


def _parse_seendate(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


_gdelt_failures = [0]
_GDELT_DISABLED = [False]


def _parse_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def fetch_gdelt(query: str, timespan: str = "14d", max_records: int = 25) -> list[dict]:
    """GDELT DOC API（全球多语种）。失败自动熔断，避免拖慢整体。"""
    key = f"gdelt:{query}:{timespan}"
    if key in _cache:
        return _cache[key]
    if _GDELT_DISABLED[0]:
        return []
    out = []
    _throttle_gdelt()
    try:
        r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc",
                         params={"query": query, "mode": "artlist",
                                 "maxrecords": str(max_records), "format": "json",
                                 "timespan": timespan, "sort": "hybridrel"},
                         headers=HEADERS, timeout=12)
        if r.status_code == 200:
            arts = (r.json() or {}).get("articles", []) or []
            for a in arts:
                out.append({
                    "title": a.get("title") or "",
                    "link": a.get("url") or "",
                    "domain": a.get("domain") or "",
                    "language": a.get("language") or "",
                    "country": a.get("sourcecountry") or "",
                    "published": _parse_seendate(a.get("seendate") or ""),
                    "source": f"GDELT:{a.get('domain','')}",
                    "authority": authority_of(a.get("domain") or ""),
                })
            _gdelt_failures[0] = 0
        else:
            _gdelt_failures[0] += 1
    except Exception as e:
        _gdelt_failures[0] += 1
        log.warning("gdelt fail %s: %s", query, str(e)[:70])
    if _gdelt_failures[0] >= 2:
        _GDELT_DISABLED[0] = True
        log.warning("GDELT 连续失败，本次运行熔断跳过")
    _cache[key] = out
    return out


def fetch_rss(url: str, source_name: str, retries: int = 3) -> list[dict]:
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                time.sleep(1)
                continue
            root = ET.fromstring(r.content)
            items = []
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = it.findtext("pubDate") or ""
                when = None
                try:
                    when = parsedate_to_datetime(pub)
                except Exception:
                    pass
                if title:
                    items.append({
                        "title": title, "link": link, "domain": source_name.lower(),
                        "source": source_name, "published": when,
                        "authority": "official", "language": "en", "country": "",
                    })
            return items
        except Exception:
            time.sleep(1)
    return []


def fetch_official_rss() -> list[dict]:
    out = []
    for name, url, _hint in OFFICIAL_FEEDS:
        out += fetch_rss(url, name)
    return out


def fetch_usgs(min_mag: float = 5.5, days: str = "week") -> list[dict]:
    url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_{days}.geojson"
    for _ in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            data = r.json()
            out = []
            for feat in data.get("features", []):
                p = feat.get("properties", {})
                mag = p.get("mag")
                if mag is None or mag < min_mag:
                    continue
                when = datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc) if p.get("time") else None
                out.append({
                    "title": f"M{mag:.1f} earthquake - {p.get('place','')}",
                    "link": p.get("url", ""), "domain": "usgs.gov", "source": "USGS",
                    "published": when, "authority": "official", "language": "en",
                    "country": "", "magnitude": mag,
                })
            return out
        except Exception:
            time.sleep(1)
    return []


def fetch_google_authoritative(query: str, n: int = 8, lang: str = "en") -> list[dict]:
    """Google News，仅权威域名兜底。"""
    sites = " OR ".join(f"site:{d}" for d in sorted(AUTHORITY_DOMAINS)[:12])
    q = f"({query}) ({sites})"
    params = ({"q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"} if lang == "zh"
              else {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    key = f"gnews-auth:{lang}:{query}"
    if key in _cache:
        return _cache[key]
    out = []
    try:
        r = requests.get("https://news.google.com/rss/search", params=params, headers=HEADERS, timeout=20)
        root = ET.fromstring(r.content)
        for it in list(root.iter("item"))[:n]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = it.findtext("pubDate") or ""
            src = it.find("source")
            dom = (src.text if src is not None else "") or ""
            when = None
            try:
                when = parsedate_to_datetime(pub)
            except Exception:
                pass
            out.append({"title": title, "link": link, "domain": dom, "source": dom,
                        "published": when, "authority": authority_of(dom),
                        "language": lang, "country": ""})
    except Exception as e:
        log.warning("google auth fail: %s", str(e)[:80])
    _cache[key] = out
    return out


def fetch_bluesky(actors: list[str], limit: int = 5) -> list[dict]:
    """大佬发声（Bluesky 公共 API）。连不上则空。"""
    out = []
    for actor in actors:
        try:
            r = requests.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                             params={"actor": actor, "limit": limit}, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            for item in (r.json() or {}).get("feed", []):
                post = item.get("post", {})
                rec = post.get("record", {})
                text = rec.get("text") or ""
                if not text:
                    continue
                when = None
                if rec.get("createdAt"):
                    try:
                        when = datetime.fromisoformat(rec["createdAt"].replace("Z", "+00:00"))
                    except Exception:
                        pass
                out.append({"title": text[:200], "link": "", "domain": f"bsky:{actor}",
                            "source": f"Bluesky@{actor}", "published": when,
                            "authority": "official", "language": "en", "country": ""})
        except Exception:
            continue
    return out


DEFAULT_ACTORS = ["who.int", "un.org", "noaa.gov", "ecdc.europa.eu", "fao.org"]


def fetch_global_items(queries: Optional[list[str]] = None, timespan: str = "14d",
                       include_gdelt: bool = True, include_official: bool = True,
                       include_usgs: bool = True, include_google: bool = True,
                       include_bluesky: bool = False, per_query: int = 20) -> list[dict]:
    """聚合全球权威事件条目（未分类，交给上层 KB 分类）。"""
    queries = queries or _GLOBAL_QUERIES_DEFAULT
    items = []
    # 1) 官方机构 RSS（快、权威）
    if include_official:
        items += fetch_official_rss()
    # 2) 全球显著地震（权威）
    if include_usgs:
        items += fetch_usgs()
    # 3) 大佬发声（Bluesky，能连则用）
    if include_bluesky:
        items += fetch_bluesky(DEFAULT_ACTORS)
    # 4) 全球权威媒体（Google News 限定权威域名）
    if include_google:
        for q in queries[:8]:
            items += fetch_google_authoritative(q, n=6)
    # 5) GDELT 全球库（尽力而为，失败熔断）
    if include_gdelt:
        for q in queries[:10]:
            items += fetch_gdelt(q, timespan=timespan, max_records=per_query)
            if _GDELT_DISABLED[0]:
                break

    # 去重 + 排序（权威优先，其次时间）
    seen = set()
    uniq = []
    for it in items:
        key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (it.get("title") or "").lower())[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    rank = {"official": 0, "tier1": 1, "other": 2}
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    uniq.sort(key=lambda x: (rank.get(x.get("authority"), 3),
                             -(x.get("published") or epoch).timestamp()))
    return uniq


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    items = fetch_global_items(queries=["plague", "el nino", "pandemic", "cholera"],
                               include_bluesky=True)
    print(f"全球权威事件条目: {len(items)}")
    for it in items[:30]:
        when = it["published"].strftime("%m-%d") if it.get("published") else "??"
        print(f"[{it['authority']:<8}] {when} {it['source'][:22]:<22} {it['title'][:70]}")
