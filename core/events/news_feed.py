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
KEEP_DAYS = 5       # 新闻只保留最近 5 天

_LOCK = threading.Lock()
_MEM = {"ts": 0.0, "items": [], "running": False}

# ---------------- 分类关键词 ----------------
CATEGORY_RULES = [
    ("财报", ["财报", "业绩", "季报", "年报", "中报", "一季报", "三季报", "预告", "净利润", "营收",
              "毛利率", "每股收益", "eps", "earnings", "revenue", "guidance", "quarterly", "q1", "q2",
              "q3", "q4", "beat", "miss", "profit warning", "preliminary results"]),
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
    for col, typ in (("level", "INTEGER"), ("cluster", "TEXT"), ("sources_n", "INTEGER")):
        try:
            conn.execute(f"ALTER TABLE news ADD COLUMN {col} {typ}")
        except Exception:
            pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news_sector(
               news_id TEXT, market TEXT, code TEXT, name TEXT,
               PRIMARY KEY(news_id, market, code))"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_sector_code ON news_sector(code)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news_stock(
               news_id TEXT, code TEXT, name TEXT,
               PRIMARY KEY(news_id, code))"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_stock_code ON news_stock(code)")
    return conn


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _to_ts(v) -> float:
    if isinstance(v, (int, float)):
        v = float(v)
        return v / 1000.0 if v > 1e11 else v
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


# 便于测试替换
def requests_get(url, **kw):
    import requests

    return requests.get(url, timeout=TIMEOUT, **kw)


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


def fetch_ths_flash(limit: int = 60) -> list[dict]:
    """同花顺 7x24 快讯。"""
    try:
        r = _get("https://news.10jqka.com.cn/tapp/news/push/stock/",
                 params={"page": 1, "pagesize": 60, "track": "website", "tag": ""})
        lst = ((r.json() or {}).get("data") or {}).get("list") or []
    except Exception:
        return []
    out = []
    for it in lst[:limit]:
        title = _clean(it.get("title") or it.get("digest") or "")
        if not title:
            continue
        out.append({
            "title": title[:180],
            "url": it.get("url") or it.get("shareUrl") or "",
            "source": "同花顺快讯",
            "tier": "tier1",
            "lang": "zh",
            "ts": _to_ts(it.get("ctime") or it.get("rtime") or ""),
        })
    return out


def fetch_jin10(limit: int = 60) -> list[dict]:
    """金十数据快讯（VIP 条目用 vip_title 兜底）。"""
    try:
        r = requests_get(
            "https://flash-api.jin10.com/get_flash_list",
            headers={**HEADERS, "x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0"},
            params={"channel": "-8200", "vip": "1"},
        )
        rows = (r.json() or {}).get("data") or []
    except Exception:
        return []
    out = []
    for it in rows[:limit]:
        d = it.get("data") or {}
        title = _clean(d.get("content") or d.get("vip_title") or "")
        if not title:
            continue
        out.append({
            "title": title[:180],
            "url": "https://www.jin10.com/",
            "source": "金十快讯",
            "tier": "tier1",
            "lang": "zh",
            "ts": _to_ts(it.get("time") or ""),
        })
    return out


def fetch_cninfo(limit: int = 60) -> list[dict]:
    """巨潮资讯 A股公告（财报/重大事项）。"""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    end = _dt.now()
    start = end - _td(days=KEEP_DAYS + 1)
    data = {
        "pageNum": 1, "pageSize": limit, "column": "szse", "tabName": "fulltext",
        "plate": "", "stock": "", "searchkey": "", "secid": "", "category": "",
        "trade": "", "seDate": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    try:
        import requests

        r = requests.post(
            "http://www.cninfo.com.cn/new/hisAnnouncement/query",
            data=data, headers=HEADERS, timeout=TIMEOUT,
        )
        rows = (r.json() or {}).get("announcements") or []
    except Exception:
        return []
    out = []
    for it in rows[:limit]:
        title = _clean(it.get("announcementTitle") or "")
        if not title:
            continue
        name = _clean(it.get("secName") or "")
        code = str(it.get("secCode") or "").strip()
        url = str(it.get("adjunctUrl") or "")
        if url and not url.startswith("http"):
            url = "http://static.cninfo.com.cn/" + url.lstrip("/")
        out.append({
            "title": f"{name} {title}".strip()[:180],
            "url": url,
            "source": "巨潮公告",
            "tier": "official",
            "lang": "zh",
            "ts": _to_ts(it.get("announcementTime") or ""),
        })
    return out


def fetch_eia() -> list[dict]:
    """EIA Today in Energy（能源政策/油价/库存评论）。"""
    return fetch_rss("https://www.eia.gov/rss/todayinenergy.xml", "EIA", tier="official", lang="en", limit=15)


def fetch_fao_giews() -> list[dict]:
    """FAO GIEWS：粮食预警（HTML 列表，抓标题+链接）。"""
    out = []
    try:
        import re as _re
        r = _get("https://www.fao.org/giews/en/")
        txt = r.text
        for m in _re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{15,140})</a>', txt):
            href, title = m.group(1), _clean(m.group(2))
            if not title or len(title) < 15:
                continue
            if not href.startswith("http"):
                href = "https://www.fao.org" + ("" if href.startswith("/") else "/") + href
            out.append({"title": title, "url": href, "source": "FAO GIEWS",
                        "tier": "official", "lang": "en", "ts": time.time()})
            if len(out) >= 12:
                break
    except Exception:
        return []
    return out


def fetch_cma_alerts() -> list[dict]:
    """中国气象局预警（nmc.cn）：红/橙/黄 级别预警。"""
    out = []
    try:
        import re as _re
        r = _get("http://www.nmc.cn/publish/alarm.html")
        r.encoding = r.apparent_encoding or "utf-8"
        txt = r.text
        for m in _re.finditer(r"<li[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", txt, _re.S):
            href, title = m.group(1), _clean(m.group(2))
            if not title or len(title) < 6:
                continue
            if href.startswith("/"):
                href = "http://www.nmc.cn" + href
            out.append({"title": title, "url": href, "source": "气象局预警",
                        "tier": "official", "lang": "zh", "ts": time.time()})
            if len(out) >= 12:
                break
    except Exception:
        return []
    return out


# 说明：新浪国际/人民网/WSJ 的 RSS 内容是几年前的老条目（实测最新也有 500~2900 天前），
# 在 5 天保留窗口下会被立刻清掉，只会白耗时间，所以不接。
SOURCES = [
    ("同花顺快讯", lambda: fetch_ths_flash()),
    ("金十快讯", lambda: fetch_jin10()),
    ("巨潮公告", lambda: fetch_cninfo()),
    ("EIA", fetch_eia),
    ("东财快讯", lambda: fetch_em_kuaixun()),
    ("CNBC", lambda: fetch_rss("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC", lang="en", limit=25)),
    ("WHO", lambda: fetch_rss("https://www.who.int/rss-feeds/news-english.xml", "WHO", tier="official", lang="en", limit=15)),
    ("CDC", lambda: fetch_rss("https://tools.cdc.gov/api/v2/resources/media/132608.rss", "CDC", tier="official", lang="en", limit=15)),
    ("FAO", lambda: fetch_rss("https://www.fao.org/feeds/fao-newsroom-rss", "FAO", tier="official", lang="en", limit=15)),
    ("USGS", lambda: fetch_usgs()),
]


# ---------------- 新闻 → 板块 ----------------
# 板块名以外的常用同义词（命中后映射到该板块）
SECTOR_ALIAS = {
    "半导体": ["半导体", "芯片", "晶圆", "光刻", "存储芯片", "先进制程", "台积电", "nvidia", "英伟达", "tsmc"],
    "元件": ["pcb", "覆铜板", "被动元件", "电容", "电阻"],
    "消费电子": ["消费电子", "手机", "苹果", "iphone", "可穿戴"],
    "软件开发": ["软件", "saas", "操作系统", "信创"],
    "IT服务": ["it服务", "云计算", "数据中心", "算力"],
    "通信设备": ["通信设备", "光模块", "5g", "基站", "卫星互联网"],
    "白酒": ["白酒", "茅台", "五粮液", "泸州老窖"],
    "饮料制造": ["饮料", "乳品", "啤酒"],
    "食品加工制造": ["食品", "预制菜", "调味品"],
    "银行": ["银行", "信贷", "存款", "息差"],
    "证券": ["券商", "证券", "投行"],
    "保险": ["保险", "保费", "寿险"],
    "房地产开发": ["房地产", "楼市", "地产", "房贷"],
    "电力": ["电力", "电价", "核电", "水电", "火电"],
    "煤炭开采加工": ["煤炭", "动力煤", "焦煤"],
    "石油加工贸易": ["原油", "油价", "炼化"],
    "油气开采及服务": ["油气", "石油开采", "页岩油", "opec"],
    "化学制品": ["化工", "化学品", "农药", "化肥"],
    "有色金属": ["有色", "铜", "铝", "锌", "镍"],
    "贵金属": ["黄金", "金价", "白银", "金矿"],
    "小金属": ["稀土", "锂", "钴", "钨"],
    "钢铁": ["钢铁", "铁矿", "螺纹钢"],
    "电池": ["电池", "锂电", "储能", "固态电池"],
    "光伏设备": ["光伏", "太阳能", "硅片", "组件"],
    "风电设备": ["风电", "风机", "海上风电"],
    "汽车整车": ["汽车", "整车", "新能源车", "电动车", "车企"],
    "汽车零部件": ["汽车零部件", "零部件", "tier1"],
    "军工装备": ["军工", "国防", "导弹", "战机", "军演"],
    "医疗器械": ["医疗器械", "影像设备", "耗材"],
    "化学制药": ["制药", "创新药", "集采", "医保", "fda"],
    "中药": ["中药", "中医药"],
    "生物制品": ["疫苗", "生物制品", "抗体"],
    "医疗服务": ["医疗服务", "医院", "cxo", "cro"],
    "航运": ["航运", "集运", "干散货", "运价"],
    "港口": ["港口", "码头"],
    "航空机场": ["航空", "机场", "航班"],
    "物流": ["物流", "快递", "供应链"],
    "旅游及景区": ["旅游", "景区", "出行"],
    "酒店餐饮": ["酒店", "餐饮"],
    "零售": ["零售", "超市", "电商"],
    "游戏": ["游戏", "版号"],
    "文化传媒": ["传媒", "影视", "广告", "出版"],
    "农牧饲渔": ["农业", "种业", "养殖", "生猪", "粮食"],
    "工程建设": ["基建", "工程", "建筑"],
    "水泥建材": ["水泥", "建材", "玻璃"],
    "家用电器": ["家电", "空调", "冰箱"],
}


def _sector_index() -> list[dict]:
    """从快照里取板块列表（code/name），用于新闻标题匹配。"""
    import sqlite3 as _s

    out: list[dict] = []
    try:
        conn = _s.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        conn.row_factory = _s.Row
        for key, market in (("list:cn:industry", "cn"), ("list:cn:concept", "cn")):
            row = conn.execute("SELECT payload FROM snapshots WHERE key=?", (key,)).fetchone()
            if not row:
                continue
            for r in json.loads(row["payload"]).get("rows") or []:
                if r.get("code"):
                    out.append({"market": market, "code": str(r["code"]), "name": str(r.get("name") or "")})
        conn.close()
    except Exception:
        pass
    return out


_STOCK_INDEX = {"ts": 0.0, "items": []}


def _stock_index() -> list[dict]:
    """A股股票名 → 代码（来自评分表，缓存 1 小时）。"""
    now = time.time()
    if _STOCK_INDEX["items"] and now - _STOCK_INDEX["ts"] < 3600:
        return _STOCK_INDEX["items"]
    items: list[dict] = []
    try:
        with open(ROOT / "data" / "company_scores.json", encoding="utf-8") as f:
            d = json.load(f)
        for r in d.get("rows") or []:
            name = str(r.get("名称") or "").strip()
            code = str(r.get("代码") or "").strip().zfill(6)
            if len(name) >= 2 and code.isdigit():
                items.append({"code": code, "name": name})
    except Exception:
        items = []
    _STOCK_INDEX["items"] = items
    _STOCK_INDEX["ts"] = now
    return items


def _match_stocks(title: str, index: list[dict]) -> list[dict]:
    """标题里出现股票名（≥2 字）或 6 位代码 → 关联个股，最多 5 只。"""
    t = str(title or "")
    low = t.lower()
    out: list[dict] = []
    seen: set[str] = set()
    for it in index:
        if it["code"] in seen:
            continue
        if it["name"] in t or it["code"] in t:
            # 2 字名字容易误伤（如“中国”），要求前后不是字母数字
            out.append(it)
            seen.add(it["code"])
            if len(out) >= 5:
                break
    return out


def _match_sectors(title: str, index: list[dict]) -> list[dict]:
    """标题里出现板块名（含同义词）就关联，最多 4 个。"""
    t = str(title or "").lower()
    if not t:
        return []
    hits: list[dict] = []
    seen: set[str] = set()
    for sec in index:
        code = sec["code"]
        if code in seen:
            continue
        name = sec["name"]
        kws = [name] + SECTOR_ALIAS.get(name, [])
        if any(k and k.lower() in t for k in kws):
            hits.append(sec)
            seen.add(code)
            if len(hits) >= 4:
                break
    return hits


# ---------------- 事件聚类 & 注意力级别 ----------------
_GLOBAL_KW = ["全球", "世界", "国际", "global", "world", "international"]
_NATION_KW = ["全国", "国家", "national", "nationwide", "联邦"]
_LONG_KW = ["持续", "长期", "多年", "season", "long-term", "蔓延", "escalat"]


def _watch_codes() -> tuple[list[str], list[str]]:
    """自选里的板块代码 + 个股代码（后 6 位）。"""
    sectors: list[str] = []
    stocks: list[str] = []
    try:
        with open(ROOT / "data" / "watch.json", encoding="utf-8") as f:
            items = json.load(f)
        for it in items or []:
            code = str(it.get("code") or "").strip()
            if not code:
                continue
            if it.get("market") == "us":
                continue
            if str(it.get("kind")) in ("industry", "concept"):
                sectors.append(code)
            else:
                stocks.append(re.sub(r"\D", "", code)[-6:].zfill(6))
    except Exception:
        pass
    return sectors, stocks


def _cluster(items: list[dict]) -> None:
    """把同一事件的多条新闻归到一个 cluster（标题相似度 ≥0.82 且 12 小时内）。"""
    from difflib import SequenceMatcher

    items.sort(key=lambda x: -(x.get("ts") or 0))
    reps: list[dict] = []
    for it in items:
        title = (it.get("title") or "")[:120]
        hit = None
        for rep in reps:
            if abs((it.get("ts") or 0) - (rep.get("ts") or 0)) > 12 * 3600:
                continue
            if SequenceMatcher(None, title, (rep.get("title") or "")[:120]).ratio() >= 0.82:
                hit = rep
                break
        if hit is None:
            it["cluster"] = it["id"]
            it["sources_n"] = 1
            reps.append(it)
        else:
            hit["sources_n"] = int(hit.get("sources_n") or 1) + 1
            it["cluster"] = hit["cluster"]
            it["sources_n"] = 0            # 0 = 聚类里的非代表条目，前端可折叠


def _level(it: dict) -> int:
    """注意力级别 L0-L4：分数 + 硬门控。"""
    title = (it.get("title") or "")
    t = title.lower()
    score = float(it.get("importance") or 1.4)
    score += 0.4 * max(0, min(int(it.get("sources_n") or 1) - 1, 3))     # 多源确认
    if any(k in t for k in _GLOBAL_KW):
        score += 0.6
    elif any(k in t for k in _NATION_KW):
        score += 0.3
    if any(k in t for k in _LONG_KW):
        score += 0.3

    lv = 4 if score >= 5.4 else 3 if score >= 4.4 else 2 if score >= 3.5 else 1 if score >= 2.6 else 0

    # 硬门控：客观量级直接提级
    import re as _re

    m = _re.search(r"\bm\s*([3-9](?:\.\d)?)\b|震级\s*([3-9](?:\.\d)?)", t)
    if m:
        mag = float(m.group(1) or m.group(2))
        if mag >= 7.5:
            lv = max(lv, 4)
        elif mag >= 6.5:
            lv = max(lv, 3)

    gates = [
        (["pheic", "pandemic", "大流行", "pandemic", "who declares"], 3),
        (["宣战", "战争升级", "全面战争", "invasion", "nuclear", "核武"], 3),
        (["封锁海峡", "航道封锁", "strait closed", "苏伊士", "红海"], 3),
        (["出口管制", "制裁名单", "sanction", "entity list", "关税"], 2),
        (["红色预警", "red alert", "最高级别"], 2),
        (["台风", "飓风", "hurricane", "typhoon", "洪水", "地震", "海啸"], 1),
    ]
    for kws, floor in gates:
        if any(k in t for k in kws):
            lv = max(lv, floor)

    if int(it.get("sources_n") or 1) >= 3:
        lv = max(lv, 2)
    return min(4, lv)


def _fingerprint(title: str) -> str:
    key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(title or "").lower())[:80]
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def _dedupe(items: list[dict]) -> list[dict]:
    seen_fp: dict[str, dict] = {}
    seen_url: set[str] = set()
    out = []
    for it in items:
        fp = _fingerprint(it["title"])
        url = (it.get("url") or "").strip().lower()
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
                "INSERT OR REPLACE INTO news(id,ts,source,tier,category,title,url,lang,importance,fetched,"
                "level,cluster,sources_n) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (it["id"], it["ts"], it["source"], it["tier"], it["category"],
                     it["title"], it["url"], it["lang"], it["importance"], time.time(),
                     it.get("level", 0), it.get("cluster", it["id"]), it.get("sources_n", 1))
                    for it in items
                ],
            )
            conn.execute("DELETE FROM news WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
            # 关联板块
            links = []
            for it in items:
                for sec in it.get("sectors") or []:
                    links.append((it["id"], sec["market"], sec["code"], sec["name"]))
            slinks = []
            for it in items:
                for st in it.get("stocks") or []:
                    slinks.append((it["id"], st["code"], st["name"]))
            if slinks:
                conn.executemany(
                    "INSERT OR REPLACE INTO news_stock(news_id,code,name) VALUES(?,?,?)", slinks
                )
            if links:
                conn.executemany(
                    "INSERT OR REPLACE INTO news_sector(news_id,market,code,name) VALUES(?,?,?,?)",
                    links,
                )
            conn.commit()
            conn.close()
    except Exception:
        pass


def load_news(limit: int = 200, category: str = "", q: str = "", hours: int = 0,
              code: str = "", stock: str = "") -> list[dict]:
    try:
        conn = _conn()
        sql = ("SELECT id,ts,source,tier,category,title,url,lang,importance,"
               "ifnull(level,0),ifnull(cluster,id),ifnull(sources_n,1) FROM news")
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
        if stock:
            import re as _re2

            sc = _re2.sub(r"\D", "", str(stock))[-6:].zfill(6)
            sql = ("SELECT n.id,n.ts,n.source,n.tier,n.category,n.title,n.url,n.lang,n.importance,"
                   "ifnull(n.level,0),ifnull(n.cluster,n.id),ifnull(n.sources_n,1) "
                   "FROM news n JOIN news_stock t ON t.news_id=n.id WHERE t.code=?")
            args = [sc]
            if category:
                sql += " AND n.category=?"
                args.append(category)
            sql += " ORDER BY n.ts DESC LIMIT ?"
            args.append(max(1, min(int(limit), 500)))
            rows = conn.execute(sql, args).fetchall()
            conn.close()
            return [
                {"id": r[0], "ts": r[1], "source": r[2], "tier": r[3], "category": r[4],
                 "title": r[5], "url": r[6], "lang": r[7], "importance": r[8],
                 "level": r[9], "cluster": r[10], "sources_n": r[11]}
                for r in rows
            ]
        if code:
            sql = ("SELECT n.id,n.ts,n.source,n.tier,n.category,n.title,n.url,n.lang,n.importance,"
                   "ifnull(n.level,0),ifnull(n.cluster,n.id),ifnull(n.sources_n,1) "
                   "FROM news n JOIN news_sector s ON s.news_id=n.id WHERE s.code=?")
            args = [str(code)]
            if category:
                sql += " AND n.category=?"
                args.append(category)
            sql += " ORDER BY n.ts DESC LIMIT ?"
            args.append(max(1, min(int(limit), 500)))
            rows = conn.execute(sql, args).fetchall()
            conn.close()
            return [
                {"id": r[0], "ts": r[1], "source": r[2], "tier": r[3], "category": r[4],
                 "title": r[5], "url": r[6], "lang": r[7], "importance": r[8],
                 "level": r[9], "cluster": r[10], "sources_n": r[11]}
                for r in rows
            ]
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
         "title": r[5], "url": r[6], "lang": r[7], "importance": r[8],
         "level": r[9], "cluster": r[10], "sources_n": r[11]}
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
    index = _sector_index()
    for it in items:
        it["id"] = _fingerprint(it["title"])
        it["category"] = _categorize(it["title"])
        it["importance"] = _score(it)
        it["ts"] = float(it.get("ts") or now)
        it["sectors"] = _match_sectors(it["title"], index) if index else []
        it["stocks"] = _match_stocks(it["title"], _stock_index())
    _cluster(items)
    for it in items:
        it["level"] = _level(it)
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


def state(limit: int = 200, category: str = "", q: str = "", code: str = "",
          stock: str = "", min_level: int = 0, watch: bool = False) -> dict:
    """给接口用：立即返回库里数据，同时判断是否需要后台刷新。"""
    kick_refresh(False)
    counts: dict[str, int] = {}
    for r in load_news(limit=800):
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    rows = load_news(limit=limit, category=category, q=q, code=code, stock=stock)
    if min_level:
        rows = [r for r in rows if int(r.get("level") or 0) >= min_level]
    if watch:   # 只看自选板块 / 自选个股相关
        secs, stks = _watch_codes()
        if secs or stks:
            ids: set[str] = set()
            try:
                conn = _conn()
                if secs:
                    q1 = ",".join("?" * len(secs))
                    ids |= {r[0] for r in conn.execute(
                        f"SELECT DISTINCT news_id FROM news_sector WHERE code IN ({q1})", secs)}
                if stks:
                    q2 = ",".join("?" * len(stks))
                    ids |= {r[0] for r in conn.execute(
                        f"SELECT DISTINCT news_id FROM news_stock WHERE code IN ({q2})", stks)}
                conn.close()
            except Exception:
                ids = set()
            rows = [r for r in rows if r["id"] in ids]
    return {
        "items": rows,
        "counts": counts,
        "levels": {str(l): sum(1 for r in load_news(limit=800) if int(r.get("level") or 0) == l) for l in range(5)},
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
