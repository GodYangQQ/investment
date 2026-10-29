#!/usr/bin/env python3
"""
抓取 X/Twitter 用户最近推文
==========================
多通道尝试：官方 X API v2（有 token 时最优） → 公共镜像/RSS 兜底。

用法:
    python scripts/fetch_x_user.py aleabitoreddit
    python scripts/fetch_x_user.py aleabitoreddit --limit 20
    X_BEARER_TOKEN=xxxx python scripts/fetch_x_user.py aleabitoreddit   # 走官方API

依赖: requests
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}

# 公共镜像（不稳定，逐个尝试）
NITTER = [
    "https://nitter.poast.org", "https://nitter.tiekoetter.com", "https://nitter.space",
    "https://nitter.kavin.rocks", "https://nitter.cz", "https://xcancel.com",
    "https://nitter.privacydev.net", "https://bird.trom.tf", "https://twiiit.com",
]
RSSHUB = ["https://rsshub.app", "https://rsshub.rssforever.com"]


def via_x_api(handle: str, limit: int) -> list[dict]:
    token = os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")
    if not token:
        return []
    try:
        r = requests.get(f"https://api.twitter.com/2/users/by/username/{handle}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
        uid = (r.json().get("data") or {}).get("id")
        if not uid:
            return []
        r2 = requests.get(f"https://api.twitter.com/2/users/{uid}/tweets",
                          headers={"Authorization": f"Bearer {token}"},
                          params={"max_results": min(max(limit, 5), 100),
                                  "tweet.fields": "created_at,public_metrics"},
                          timeout=20)
        out = []
        for t in (r2.json().get("data") or []):
            out.append({"text": t["text"], "created": t.get("created_at"),
                        "link": f"https://x.com/{handle}/status/{t['id']}", "src": "X-API"})
        return out
    except Exception as e:
        print(f"[x-api] 失败: {str(e)[:80]}", file=sys.stderr)
        return []


def _parse_rss(text: str, handle: str, src: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items = []
    for it in root.iter("item"):
        title = re.sub("<[^>]+>", "", it.findtext("title") or "").strip()
        link = it.findtext("link") or ""
        pub = it.findtext("pubDate") or ""
        try:
            when = parsedate_to_datetime(pub).strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = pub
        if title:
            items.append({"text": title, "created": when, "link": link, "src": src})
    return items


def via_nitter(handle: str, limit: int) -> list[dict]:
    for base in NITTER:
        for path in (f"/{handle}/rss", f"/{handle}"):
            try:
                r = requests.get(base + path, headers=H, timeout=12)
                if r.status_code == 200 and "<item" in r.text:
                    items = _parse_rss(r.text, handle, f"nitter:{base}")
                    if items:
                        return items[:limit]
            except Exception:
                continue
    return []


def via_rsshub(handle: str, limit: int) -> list[dict]:
    for base in RSSHUB:
        try:
            r = requests.get(f"{base}/twitter/user/{handle}", headers=H, timeout=12)
            if r.status_code == 200 and "<item" in r.text:
                items = _parse_rss(r.text, handle, f"rsshub:{base}")
                if items:
                    return items[:limit]
        except Exception:
            continue
    return []


def via_fxtwitter_profile(handle: str) -> dict:
    try:
        r = requests.get(f"https://api.fxtwitter.com/{handle}", headers=H, timeout=12)
        if r.status_code == 200:
            return r.json().get("user", {})
    except Exception:
        pass
    return {}


def main():
    ap = argparse.ArgumentParser(description="抓取 X 用户最近推文")
    ap.add_argument("handle", help="X 用户名（不含@），如 aleabitoreddit")
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()
    handle = args.handle.lstrip("@")

    prof = via_fxtwitter_profile(handle)
    if prof:
        print(f"用户: {prof.get('name')} (@{handle})  粉丝 {prof.get('followers')}")
        if prof.get("description"):
            print(f"简介: {prof['description'][:140]}\n")

    for fn in (via_x_api, via_nitter, via_rsshub):
        items = fn(handle, args.limit)
        if items:
            print(f"== 最近 {len(items)} 条（来源 {items[0]['src']}）==")
            for it in items:
                print(f"\n[{it['created']}] {it['text'][:280]}")
                if it.get("link"):
                    print(f"  {it['link']}")
            return
    print("抓取失败：X 官方与公共镜像均不可达。")
    print("可选：1) 设置 X_BEARER_TOKEN 环境变量走官方API；2) 在能访问 X 的网络运行本脚本；3) 直接粘贴推文内容。")


if __name__ == "__main__":
    main()
