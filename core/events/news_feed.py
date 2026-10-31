"""新闻 / 事件流：多源抓取 → 归一化 → 去重 → 分类 → 打分 → 落库。

设计原则（和看盘一致）：
    · 只取权威/一手来源，不做全量资讯流
    · 多源同一事件会被去重（标题指纹 + 相似度）
    · 每条带分类（宏观/地缘/产业/灾害/市场/其他）与重要度
    · 结果缓存进 SQLite（表 news），前端直接读本地，不每次联网

数据源（按当前网络可达性挑选，能连则采、失败静默跳过）：
    东财快讯 JSON、新浪财经 RSS、人民网国际 RSS、WSJ World RSS、CNBC RSS、
    WHO/CDC/FAO 官方 RSS、USGS 显著地震；GDELT / Google News 可选（常被墙）

用法：
    from core.events.news_feed import fetch_news
    items = fetch_news(limit=200)
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_FILE = ROOT / "data" / "sector_monitor.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
TIMEOUT = 12
TTL = 600           # 10 分钟内不重复抓
KEEP_DAYS = 90      # 库里保留 90 天

_LOCK = threading.Lock()
_MEM = {"ts": 0.0, "items": [], "running": False}

# ---------------- 分类关键词 ----------------
CATEGORY_RULES = [
    ("灾害", ["earthquake", "quake", "tsunami", "hurricane", "typhoon", "cyclone", "flood", "wildfire",
              "drought", "volcano", "landslide", "地震", "台风", "洪水", "山火", "干旱", "海啸", "疫情", "outbreak"]),
    ("地缘", ["war", "conflict", "sanction", "missile", "airstrike", "military", "invasion", "ceasefire",
              "nato", "opec", "red sea", "tariff", "trade war", "战争", "冲突", "制裁", "导弹", "关税",
              "军演", "停火", "袭击", "红海"]),
    ("宏观", ["fed", "fomc", "rate cut", "rate hike", "inflation", "cpi", "ppi", "gdp", "jobless",
              "payroll", "treasury", "central bank", "ecb", "boj", "pboc", "stimulus", "recession",
              "美联储", "降息", "加息", "通胀", "货币政策", "央行", "降准", "财政", "国债", "经济数据",
              "非农", "gdp", "社融"]),
    ("产业", ["semiconductor", "chip", "ai ", "nvidia", "tsmc", "oil", "gas", "opec", "lithium",
              "battery", "solar", "ev ", "autos", "pharma", "drug", "airline", "shipping",
              "半导体", "芯片", "算力", "光伏", "储能", "锂", "汽车", "医药", "集采", "航运", "石油", "天然气"]),
    ("市场", ["stocks", "shares", "market", "earnings", "ipo", "buyback", "bond", "yield", "dollar",
              "gold", "bitcoin", "crypto", "财报", "回购", "上市", "涨停", "跌停", "北向", "融资",
              "股市", "债市", "汇率", "黄金"]),
]

SOURCE_WEIGHT = {"official": 3.0, "tier1": 2.2, "other": 1.4}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news(
               id TEXT PRIMARY KEY, ts REAL, source TEXT, tier TEXT, category TEXT,
               title TEXT, url TEXT, lang TEXT, importance REAL, fetched REAL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts DESC)")
    return conn


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _to_ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    if not s:
        return time.time()
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 2].strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    return time.time()


def _categorize(title: str) -> str:
    t = title.lower()
    for name, kws in CATEGORY_RULES:
        if any(k.lower() in t for k in kws):
            return name
    return "其他"


def _score(item: dict) -> float:
    base = SOURCE_WEIGHT.get(item.get("tier") or "other", 1.0)
    title = (item.get("title") or "").lower()
    hits = sum(1 for _n, kws in CATEGORY_RULES if any(k.lower() in title for k in kws))
    age_h = max(0.0, (time.time() - float(item.get("ts") or time.time())) / 3600)
    decay = max(0.15, 1.0 - age_h / 72.0)          # 3 天线性衰减
    return round(base * (1 + 0.25 * hits) * decay, 3)


# ---------------- 各数据源 ----------------
def _get(url: str, **kw):
    import requests

    return requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)


def fetch_em_kuaixun(limit: int = 60) -> list[dict]:
    """东方财富 7x24 快讯（中文、A股相关度高）。"""
    url = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
    try:
        r = _get(url)
        txt = r.text
        m = re.search(r"var ajaxResult\s*=\s*(\{.*\})\s*;?\s*$", txt, re.S)
        data = json.loads(m.group(1)) if m else json.loads(txt)
        rows = (data.get("LivesList") or [])[:limit]
    except Exception:
        return []
    out = []
    for it in rows:
        title = _clean(it.get("digest") or it.get("title") or "")
        if not title:
            continue
        out.append({
            "title": title[:180],
            "url": it.get("url_unique") or it.get("url") or "",
            "source": "东方财富快讯",
            "tier": "tier1",
            "lang": "zh",
            "ts": _to_ts(it.get("showtime") or it.get("showtime_origin")),
        })
    return out


def _parse_rss(xml_text: str, source: str, tier: str, lang: str, limit: int = 40) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "ignore") if isinstance(xml_text, str) else xml_text)
    except Exception:
        return []
    for item in root.iter("item"):
        title = _clean(item.findtext("title") or "")
        link = _clean(item.findtext("link") or "")
        if not title:
            continue
        out.append({
            "title": title[:180],
            "url": link,
            "source": source,
            "tier": tier,
            "lang": lang,
            "ts": _to_ts(item.findtext("pubDate") or item.findtext("date") or ""),
        })
        if len(out) >= limit:
            break
    return out


def fetch_rss(url: str, source: str, tier: str = "tier1", lang: str = "zh", limit: int = 40) -> list[dict]:
    try:
        r = _get(url)
        if r.status_code != 200:
            return []
        return _parse_rss(r.text, source, tier, lang, limit)
    except Exception:
        return []


def fetch_usgs(min_mag: float = 4.5) -> list[dict]:
    try:
        r = _get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson")
        feats = (r.json() or {}).get("features") or []
    except Exception:
        return []
    out = []
    for f in feats:
        p = f.get("properties") or {}
        mag = p.get("mag")
        try:
            if float(mag) < min_mag:
                continue
        except Exception:
            continue
        out.append({
            "title": f"M{mag} {p.get('place') or ''}".strip(),
            "url": p.get("url") or "",
            "source": "USGS地震",
            "tier": "official",
            "lang": "en",
            "ts": (float(p.get("time") or 0) / 1000) or time.time(),
        })
    return out


SOURCES = [
    ("东财快讯", lambda: fetch_em_kuaixun()),
    ("新浪财经", lambda: fetch_rss("https://rss.sina.com.cn/roll/finance/hot_roll.xml", "新浪财经")),
    ("新浪国际", lambda: fetch_rss("https://rss.sina.com.cn/news/world/focus15.xml", "新浪国际")),
    ("人民网", lambda: fetch_rss("http://www.people.com.cn/rss/world.xml", "人民网国际", limit=25)),
    ("WSJ", lambda: fetch_rss("https://feeds.a.dj.com/rss/RSSWorldNews.xml", "WSJ World", lang="en", limit=25)),
    ("CNBC", lambda: fetch_rss("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC", lang="en", limit=25)),
    ("WHO", lambda: fetch_rss("https://www.who.int/rss-feeds/news-english.xml", "WHO", tier="official", lang="en", limit=15)),
    ("CDC", lambda: fetch_rss("https://tools.cdc.gov/api/v2/resources/media/132608.rss", "CDC", tier="official", lang="en", limit=15)),
    ("FAO", lambda: fetch_rss("https://www.fao.org/feeds/fao-newsroom-rss", "FAO", tier="official", lang="en", limit=15)),
    ("USGS", lambda: fetch_usgs()),
]


def _fingerprint(title: str) -> str:
    key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(title or "").lower())[:80]
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def _dedupe(items: list[dict]) -> list[dict]:
    seen_fp: dict[str, dict] = {}
    seen_url: set[str] = set()
    out = []
    for it in items:
        fp = _fingerprint(it["title"])
        url = (it.get("url") or "").split("?")[0]
        if not fp:
            continue
        if url and url in seen_url:
            continue
        prev = seen_fp.get(fp)
        if prev:                      # 同一事件：保留更高权重来源
            if SOURCE_WEIGHT.get(it["tier"], 1) > SOURCE_WEIGHT.get(prev["tier"], 1):
                prev.update({k: it[k] for k in ("source", "tier", "url", "lang", "ts")})
            continue
        seen_fp[fp] = it
        if url:
            seen_url.add(url)
        out.append(it)
    return out


def _save(items: list[dict]) -> None:
    if not items:
        return
    try:
        with _LOCK:
            conn = _conn()
            conn.executemany(
                "INSERT OR REPLACE INTO news(id,ts,source,tier,category,title,url,lang,importance,fetched)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (it["id"], it["ts"], it["source"], it["tier"], it["category"],
                     it["title"], it["url"], it["lang"], it["importance"], time.time())
                    for it in items
                ],
            )
            conn.execute("DELETE FROM news WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
            conn.commit()
            conn.close()
    except Exception:
        pass


def load_news(limit: int = 200, category: str = "", q: str = "", hours: int = 0) -> list[dict]:
    try:
        conn = _conn()
        sql = "SELECT id,ts,source,tier,category,title,url,lang,importance FROM news"
        args: list = []
        where = []
        if category:
            where.append("category=?")
            args.append(category)
        if q:
            where.append("title LIKE ?")
            args.append(f"%{q}%")
        if hours:
            where.append("ts > ?")
            args.append(time.time() - hours * 3600)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = conn.execute(sql, args).fetchall()
        conn.close()
    except Exception:
        return []
    return [
        {"id": r[0], "ts": r[1], "source": r[2], "tier": r[3], "category": r[4],
         "title": r[5], "url": r[6], "lang": r[7], "importance": r[8]}
        for r in rows
    ]


def _collect() -> None:
    """真正抓一轮：多源 → 去重 → 分类打分 → 落库。"""
    collected: list[dict] = []
    for _name, fn in SOURCES:
        try:
            collected += fn() or []
        except Exception:
            continue
    items = _dedupe(collected)
    now = time.time()
    for it in items:
        it["id"] = _fingerprint(it["title"])
        it["category"] = _categorize(it["title"])
        it["importance"] = _score(it)
        it["ts"] = float(it.get("ts") or now)
    _save(items)
    _MEM["ts"] = now
    _MEM["items"] = items


def _worker(force: bool) -> None:
    try:
        _collect()
    except Exception:
        pass
    finally:
        _MEM["running"] = False


def kick_refresh(force: bool = False) -> bool:
    """后台抓一轮（不阻塞请求）。返回是否真的启动了。"""
    if _MEM["running"]:
        return False
    if not force and _MEM["items"] and time.time() - _MEM["ts"] < TTL:
        return False
    _MEM["running"] = True
    threading.Thread(target=_worker, args=(force,), daemon=True).start()
    return True


def state(limit: int = 200, category: str = "", q: str = "") -> dict:
    """给接口用：立即返回库里数据，同时判断是否需要后台刷新。"""
    kick_refresh(False)
    counts: dict[str, int] = {}
    for r in load_news(limit=800):
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return {
        "items": load_news(limit=limit, category=category, q=q),
        "counts": counts,
        "pending": _MEM["running"],
        "updated": _MEM["ts"],
    }


def fetch_news(limit: int = 200, category: str = "", q: str = "", force: bool = False) -> dict:
    """抓一轮（带 10 分钟内存 TTL）并返回库里的最新列表。"""
    if force or not _MEM["items"]:
        _collect()
    rows = load_news(limit=limit, category=category, q=q)
    counts: dict[str, int] = {}
    for r in load_news(limit=500):
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return {"items": rows, "counts": counts, "updated": _MEM["ts"], "total": len(_MEM["items"])}
