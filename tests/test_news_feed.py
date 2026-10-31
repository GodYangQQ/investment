import time

from core.events import news_feed as nf


def test_match_us_tickers_whole_word():
    hits = nf._match_us_tickers("AMD and AVGO beat estimates; AIGC is a theme", ["AMD", "AVGO", "AI"])
    assert [h["code"] for h in hits] == ["AMD", "AVGO"]


def test_sec_filing_level_floor():
    item = {
        "title": "AMD · ADVANCED MICRO DEVICES INC 季报(10-Q) 2026-10-30",
        "source": "SEC公告",
        "importance": 0.4,
        "sources_n": 1,
    }
    assert nf._level(item) >= 2
    item["title"] = "AMD · ADVANCED MICRO DEVICES INC 重大事项(8-K) 2026-10-30"
    assert nf._level(item) >= 1


def test_decay_keeps_week_old_items_alive():
    old = {"tier": "official", "title": "某公司季报", "ts": time.time() - 5 * 86400}
    assert nf._score(old) > 1.0   # 旧逻辑 3 天衰减到地板只剩 0.56


def test_categorize_cn_reports():
    assert nf._categorize("生益科技2025年年度报告") == "财报"
    assert nf._categorize("兆易创新2026年第三季度报告") == "财报"
