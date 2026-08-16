#!/usr/bin/env python3
"""판정 로직 단위 검증 (openEO 접속 없이 합성 데이터로 확인)."""

import math
from datetime import date, timedelta

import pandas as pd

from paddy_check import (
    DEFAULT_THRESHOLD_DB,
    buffer_polygon,
    _bbox_of,
    _parse_aggregate_result,
    classify,
    find_drain_periods,
    smooth,
)


def make_df(values, start=date(2025, 5, 1), step=6):
    """6일 간격 관측 시계열 DataFrame 생성."""
    dates = [start + timedelta(days=step * i) for i in range(len(values))]
    df = pd.DataFrame({"date": pd.to_datetime(dates), "vh_db": values})
    return df


def test_buffer_polygon():
    geom = buffer_polygon(36.3721, 127.3604, 40.0)
    ring = geom["coordinates"][0]
    assert geom["type"] == "Polygon"
    assert ring[0] == ring[-1], "폴리곤이 닫혀 있어야 함"
    # 중심에서 각 꼭짓점까지 거리가 약 40m 인지 확인
    for lon, lat in ring[:-1]:
        dy = (lat - 36.3721) * 111_320.0
        dx = (lon - 127.3604) * 111_320.0 * math.cos(math.radians(36.3721))
        assert abs(math.hypot(dx, dy) - 40.0) < 0.5, "버퍼 반경 오차"
    bbox = _bbox_of(geom)
    assert bbox["west"] < 127.3604 < bbox["east"]
    assert bbox["south"] < 36.3721 < bbox["north"]
    print("  OK  buffer_polygon / _bbox_of")


def test_classify_and_periods():
    # 담수(-24) 4회 → 낙수(-16) 5회(=24일) → 담수 3회
    vals = [-24, -24, -24, -24, -16, -16, -16, -16, -16, -24, -24, -24]
    df = classify(smooth(make_df(vals), 1), DEFAULT_THRESHOLD_DB)
    periods = find_drain_periods(df)
    assert len(periods) == 1, f"낙수 구간 1건이어야 하는데 {len(periods)}건"
    p = periods[0]
    assert p.start == date(2025, 5, 25), p.start
    assert p.end == date(2025, 6, 18), p.end
    assert p.days == 24, p.days
    assert p.days >= 14, "14일 기준 충족해야 함"
    print(f"  OK  단일 낙수 구간: {p.start} ~ {p.end} ({p.days}일)")


def test_multiple_periods():
    # 짧은 낙수(12일) + 긴 낙수(18일)
    vals = [-24, -16, -16, -16, -24, -24, -16, -16, -16, -16, -24]
    df = classify(smooth(make_df(vals), 1), DEFAULT_THRESHOLD_DB)
    periods = find_drain_periods(df)
    assert len(periods) == 2, f"{len(periods)}건"
    assert [p.days for p in periods] == [12, 18], [p.days for p in periods]
    longest = max(p.days for p in periods)
    assert longest == 18 and longest >= 14
    print(f"  OK  다중 구간: {[(str(p.start), p.days) for p in periods]}")


def test_no_drain():
    vals = [-24, -25, -23, -26, -24, -25]
    df = classify(smooth(make_df(vals), 1), DEFAULT_THRESHOLD_DB)
    assert find_drain_periods(df) == [], "상시담수는 낙수 구간 0건"
    print("  OK  상시담수 → 낙수 구간 없음")


def test_all_drain():
    vals = [-12, -13, -11, -14, -12]
    df = classify(smooth(make_df(vals), 1), DEFAULT_THRESHOLD_DB)
    periods = find_drain_periods(df)
    assert len(periods) == 1 and periods[0].days == 24
    print("  OK  전 기간 낙수 → 단일 구간 24일")


def test_smoothing_removes_spike():
    # 낙수 구간 한가운데 단발 노이즈(-21). 스무딩 없으면 구간이 둘로 쪼개진다.
    # 양 끝은 중심 이동평균의 경계 효과를 피하도록 확실한 담수값(-26)으로 둔다.
    vals = [-26, -26, -16, -16, -21, -16, -16, -26, -26]
    raw = classify(smooth(make_df(vals), 1), DEFAULT_THRESHOLD_DB)
    assert len(find_drain_periods(raw)) == 2, "스무딩 전에는 2건으로 쪼개져야 함"
    sm = classify(smooth(make_df(vals), 3), DEFAULT_THRESHOLD_DB)
    periods = find_drain_periods(sm)
    assert len(periods) == 1, f"스무딩 후 1건이어야 하는데 {len(periods)}건"
    assert periods[0].days == 24
    print("  OK  3점 이동평균이 단발 노이즈 흡수")


def test_threshold_adjustable():
    vals = [-24, -22, -22, -22, -24]
    lenient = find_drain_periods(classify(smooth(make_df(vals), 1), -20.0))
    strict = find_drain_periods(classify(smooth(make_df(vals), 1), -23.0))
    assert lenient == [], "-20 dB 기준에서는 전부 담수"
    assert len(strict) == 1 and strict[0].days == 12, "-23 dB 기준에서는 낙수 구간 발생"
    print("  OK  임계값 조정이 판정에 반영됨")


def test_parse_result():
    payload = {
        "2025-05-01T00:00:00Z": [[0.01]],          # 정상
        "2025-05-07T00:00:00Z": [[float("nan")]],  # NaN → 제외
        "2025-05-13T00:00:00Z": [[None]],          # null → 제외
        "2025-05-19T00:00:00Z": [[0.0]],           # 0 → log10 불가, 제외
        "2025-05-25T00:00:00Z": [[0.02], [0.04]],  # 여러 값 → 평균
    }
    recs = _parse_aggregate_result(payload)
    assert len(recs) == 2, f"유효 2건이어야 하는데 {len(recs)}건"
    by_date = {r["date"]: r["sigma0"] for r in recs}
    assert by_date[date(2025, 5, 1)] == 0.01
    assert abs(by_date[date(2025, 5, 25)] - 0.03) < 1e-9
    assert abs(10 * math.log10(0.01) - (-20.0)) < 1e-9, "dB 변환 확인"
    print("  OK  응답 파싱: NaN/null/0 제외, 동일 시각 평균, dB 변환")


if __name__ == "__main__":
    print("판정 로직 검증")
    for fn in [
        test_buffer_polygon,
        test_classify_and_periods,
        test_multiple_periods,
        test_no_drain,
        test_all_drain,
        test_smoothing_removes_spike,
        test_threshold_adjustable,
        test_parse_result,
    ]:
        fn()
    print("\n전체 통과")
