#!/usr/bin/env python3
"""日线数据（个股 / 指数 / ETF） — 腾讯前复权 + 新浪兜底，零 pandas 依赖。"""
from __future__ import annotations

import json
import re
from datetime import datetime

import requests

QQ_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Referer": "https://gu.qq.com/"}
SINA_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
          "Referer": "https://finance.sina.com.cn/"}


def cn_symbol(code: str) -> str:
    c = str(code or "").strip().upper().replace(".", "")
    if c[:2] in ("SH", "SZ", "BJ"):
        return c[:2].lower() + c[2:]
    if c.startswith(("60", "68", "90", "51", "58", "50", "11")):
        return "sh" + c
    if c.startswith(("4", "8", "92")):
        return "bj" + c
    return "sz" + c


def code_norm(code: str) -> str:
    return str(code or "").strip().upper().replace(".", "")


def index_code_norm(code: str) -> str:
    c = code_norm(code)
    if c[:2] in ("SH", "SZ", "BJ"):
        return c
    return ("SH" if c.startswith("000") else "SZ") + c


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fetch_daily(code: str, count: int = 900) -> tuple[list[dict], str]:
    """返回 ([{t,o,h,l,c,v}...] 升序, 来源)。失败返回 ([], "")。"""
    sym = cn_symbol(code)
    try:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={sym},day,,,{count},qfq")
        js = requests.get(url, headers=QQ_H, timeout=12).json()
        d = (js.get("data") or {}).get(sym) or {}
        arr = d.get("qfqday") or d.get("day") or []
        out = []
        for a in arr:
            try:
                t = str(a[0])[:10]
                o, c, h, low = float(a[1]), float(a[2]), float(a[3]), float(a[4])
                v = _num(a[5]) * 100          # 手 → 股
            except (TypeError, ValueError, IndexError):
                continue
            if c <= 0:
                continue
            out.append({"t": t, "o": o, "h": h, "l": low, "c": c, "v": v})
        if out:
            return out, "腾讯日线"
    except Exception:
        pass
    # 新浪兜底
    try:
        url = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
               f"?symbol={sym}&scale=240&ma=no&datalen={count}")
        raw = requests.get(url, headers=SINA_H, timeout=15).json()
        out = []
        for it in raw if isinstance(raw, list) else []:
            try:
                c = float(it["close"])
                if c <= 0:
                    continue
                out.append({"t": str(it["day"])[:10], "o": float(it["open"]),
                            "h": float(it["high"]), "l": float(it["low"]),
                            "c": c, "v": _num(it.get("volume"))})
            except (KeyError, TypeError, ValueError):
                continue
        if out:
            return out, "新浪日线"
    except Exception:
        pass
    return [], ""


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def write_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def search_symbols(q: str, limit: int = 12) -> list[dict]:
    """腾讯智能搜索：个股 / 指数 / ETF。返回 [{code,name,type,market,raw_type}]。"""
    q = (q or "").strip()
    if not q:
        return []
    try:
        r = requests.get("https://smartbox.gtimg.cn/s3/",
                         params={"v": 2, "q": q, "t": "all"},
                         headers=QQ_H, timeout=10)
        txt = r.content.decode("gbk", "ignore")
    except Exception:
        return []
    m = txt.split('v_hint="', 1)
    if len(m) < 2:
        return []
    out = []
    for item in m[1].split('"')[0].split("^"):
        parts = item.split("~")
        if len(parts) < 5:
            continue
        mk, code, name, _py, raw = parts[0].lower(), parts[1], parts[2], parts[3], parts[4]
        kind = "stock"
        if raw.startswith("ZS"):
            kind = "index"
        elif raw.startswith(("ETF", "LOF", "FJ")):
            kind = "etf"
        elif not raw.startswith("GP"):
            continue
        name = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name)
        out.append({"code": (mk.upper() + code), "name": name, "type": kind,
                    "market": "A", "raw_type": raw})
        if len(out) >= limit:
            break
    return out
