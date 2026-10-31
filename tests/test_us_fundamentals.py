from core.datasets import us_fundamentals as uf


def _q(start, end, val, filed="2026-01-01"):
    return {"start": start, "end": end, "val": val, "form": "10-Q", "filed": filed}


def test_quarterly_filters_and_dedupes():
    rows = [
        _q("2026-01-01", "2026-03-31", 100),
        _q("2026-01-01", "2026-03-31", 110, filed="2026-05-01"),  # 更正后取最新
        _q("2025-01-01", "2025-12-31", 400),                      # 全年，应丢弃
    ]
    got = uf._by_end(rows)
    assert list(got) == ["2026-03-31"]
    assert got["2026-03-31"]["val"] == 110


def test_fill_q4_from_annual():
    quarterly = {
        "2026-03-31": {"end": "2026-03-31", "val": 100.0},
        "2026-06-30": {"end": "2026-06-30", "val": 110.0},
        "2026-09-30": {"end": "2026-09-30", "val": 120.0},
    }
    annual = {"2026-12-31": {"start": "2026-01-01", "end": "2026-12-31", "val": 480.0, "fy": 2026}}
    uf._fill_q4(quarterly, annual)
    assert quarterly["2026-12-31"]["val"] == 150.0
    assert quarterly["2026-12-31"]["fp"] == "Q4"


def test_fill_q4_skips_incomplete_year():
    quarterly = {"2026-03-31": {"end": "2026-03-31", "val": 100.0}}
    annual = {"2026-12-31": {"start": "2026-01-01", "end": "2026-12-31", "val": 480.0}}
    uf._fill_q4(quarterly, annual)
    assert "2026-12-31" not in quarterly
