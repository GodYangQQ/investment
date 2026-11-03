from datetime import datetime

from core.events import us_calendar as uc


def test_et_to_beijing():
    ts = uc._et_ts("2026-11-06", "08:30")
    dt = datetime.fromtimestamp(ts, uc.BJ)
    # 11 月已结束夏令时：美东 08:30 = 北京 21:30
    assert dt.strftime("%m-%d %H:%M") == "11-06 21:30"


def test_et_default_time():
    ts = uc._et_ts("2026-11-06", "")
    dt = datetime.fromtimestamp(ts, uc.BJ)
    assert dt.strftime("%H:%M") == "05:00"  # 美东 16:00


def test_election_dates():
    ev = uc._election_events(1500)
    assert any(e["date"] == "2026-11-03" and "中期" in e["title"] for e in ev)
    assert any(e["date"] == "2028-11-07" and "总统大选" in e["title"] for e in ev)
