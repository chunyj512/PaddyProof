#!/usr/bin/env python3
"""
Phase 1(후속) — 낙수 필지 탐색: NISAR 판정 임계값 도출용

지금까지 본 논은 전부 담수 유지(HH 상승)라 낙수 상태의 L-band 값을 모른다.
임계값을 정하려면 실제로 물을 뺀 필지가 필요하다.

여러 후보 좌표의 NISAR HH 추세를 훑어, HH 가 뚜렷이 하락하는 필지를 찾는다.
담수→낙수 전환이 일어난 필지를 확보하면 그 전환점에서 임계값을 도출할 수 있다.

    python nisar_scan.py                    # 기본 후보군
    python nisar_scan.py --csv 후보.csv      # 이름,위도,경도
"""

from __future__ import annotations

import argparse
import csv as _csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTDIR = ROOT / "output"

# 호남·논산·예당·철원 등 주요 평야에 흩어진 후보 좌표.
# 필지마다 물 관리가 다르므로 넓게 뿌려 낙수 사례를 찾는다.
CANDIDATES = {
    "익산A": (35.9820, 126.9250),
    "익산B": (35.9650, 126.9480),
    "익산C": (36.0100, 126.9020),
    "논산A": (36.1600, 127.0500),
    "논산B": (36.1420, 127.0810),
    "김제A": (35.8600, 126.9500),
    "김제B": (35.8320, 126.9210),
    "부안":  (35.7300, 126.7600),
    "정읍":  (35.6100, 126.8400),
    "당진":  (36.8900, 126.7800),
    "예산":  (36.7500, 126.8500),
    "평택":  (37.0300, 127.0100),
    "나주":  (35.0300, 126.7200),
    "해남":  (34.5700, 126.5900),
}

START, END = "2026-06-17", "2026-08-03"
MIN_OBS = 5          # 추세를 보려면 최소 관측 수
DROP_DB = -1.5       # 이보다 내려가면 낙수 후보
# 논의 L-band HH 는 보통 -5 ~ -17 dB 다. 이보다 높으면 건물 등 강산란체로,
# 논이 아닐 가능성이 크다 (참고: 대전 도심 -5.6 dB).
NOT_PADDY_DB = -3.0


def trend(rows: list[dict]) -> dict:
    """전반부 대비 후반부 HH 변화로 추세를 요약한다."""
    import statistics as st

    by_date: dict[str, list[float]] = {}
    ratio: dict[str, list[float]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r["hh_db"])
        if r.get("hv_db") is not None:
            ratio.setdefault(r["date"], []).append(r["hh_db"] - r["hv_db"])

    dates = sorted(by_date)
    hh = [st.mean(by_date[d]) for d in dates]
    half = max(1, len(hh) // 2)
    early, late = st.mean(hh[:half]), st.mean(hh[half:])

    rr = [st.mean(ratio[d]) for d in dates if d in ratio]
    r_delta = (st.mean(rr[half:]) - st.mean(rr[:half])) if len(rr) >= 2 else None

    return {
        "n": len(dates), "first": dates[0], "last": dates[-1],
        "hh_early": early, "hh_late": late, "hh_delta": late - early,
        "hh_min": min(hh), "hh_max": max(hh),
        "ratio_delta": r_delta,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="NISAR 낙수 필지 탐색")
    ap.add_argument("--csv", help="후보 좌표 CSV (이름,위도,경도)")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--buffer", type=float, default=40.0)
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from nisar_source import NisarError, fetch_timeseries

    sites = dict(CANDIDATES)
    if args.csv:
        sites = {}
        with open(args.csv, encoding="utf-8-sig") as f:
            for row in _csv.reader(f):
                cells = [c.strip() for c in row if c.strip()]
                if len(cells) >= 3:
                    try:
                        sites[cells[0]] = (float(cells[1]), float(cells[2]))
                    except ValueError:
                        continue

    print("=" * 78)
    print(f"  낙수 필지 탐색 — 후보 {len(sites)}곳, {args.start} ~ {args.end}")
    print("=" * 78)
    print("  HH 가 하락한 필지가 낙수 후보입니다 (이중반사 소멸).\n")

    results = []
    for i, (name, (lat, lon)) in enumerate(sites.items(), 1):
        try:
            rows = fetch_timeseries(lat, lon, args.start, args.end,
                                    buffer_m=args.buffer, verbose=False)
        except NisarError as exc:
            print(f"  [{i:2d}/{len(sites)}] {name:<8} 실패: {str(exc)[:45]}")
            continue
        if len(rows) < MIN_OBS:
            print(f"  [{i:2d}/{len(sites)}] {name:<8} 관측 {len(rows)}건 (부족)")
            continue
        t = trend(rows)
        results.append((name, lat, lon, t))
        flag = "  ← 낙수 후보" if t["hh_delta"] <= DROP_DB else ""
        print(f"  [{i:2d}/{len(sites)}] {name:<8} n={t['n']:2d}  "
              f"HH {t['hh_early']:+6.2f} → {t['hh_late']:+6.2f}  "
              f"({t['hh_delta']:+5.2f} dB){flag}")

    if not results:
        print("\n분석할 수 있는 필지가 없습니다.")
        return 1

    print("\n" + "=" * 78)
    print("  요약 (HH 변화량 오름차순 — 위쪽이 낙수 가능성 높음)")
    print("=" * 78)
    print(f"  {'필지':<8}{'관측':>5}{'HH 변화':>10}{'HH-HV 변화':>12}"
          f"{'HH 범위':>18}   해석")
    print("  " + "-" * 74)

    drained, not_paddy = [], []
    for name, lat, lon, t in sorted(results, key=lambda r: r[3]["hh_delta"]):
        rd = t["ratio_delta"]
        if max(t["hh_early"], t["hh_late"]) >= NOT_PADDY_DB:
            # 논이 아닌 좌표는 추세 해석 자체가 무의미하므로 먼저 걸러낸다.
            interp, mark = "논 아님 의심 (HH 과다)", "!"
            not_paddy.append((name, lat, lon, t))
        elif t["hh_delta"] <= DROP_DB:
            interp, mark = "낙수 후보", "*"
            drained.append((name, lat, lon, t))
        elif t["hh_delta"] >= 1.5:
            interp, mark = "담수 유지 (이중반사 강화)", " "
        else:
            interp, mark = "변화 미미", " "
        print(f" {mark}{name:<8}{t['n']:>5}{t['hh_delta']:>+10.2f}"
              f"{(f'{rd:+.2f}' if rd is not None else '-'):>12}"
              f"{f'{t['hh_min']:+.1f} ~ {t['hh_max']:+.1f}':>18}   {interp}")

    print()
    if not_paddy:
        print(f"  [!] {len(not_paddy)}곳은 HH 가 {NOT_PADDY_DB:.0f} dB 이상으로 논이 아닐")
        print(f"      가능성이 큽니다 (도심 참고값 -5.6 dB). 좌표를 다시 확인하세요:")
        for name, lat, lon, t in not_paddy:
            print(f"        {name:<8} {lat:.4f}, {lon:.4f}  "
                  f"HH {t['hh_early']:+.1f} ~ {t['hh_late']:+.1f} dB")
        print()

    if drained:
        print(f"  낙수 후보 {len(drained)}곳을 찾았습니다. 다음 단계:")
        for name, lat, lon, t in drained:
            print(f"    python paddy_check.py --lat {lat} --lon {lon} "
                  f"--start {args.start} --end {args.end} --csv")
            print(f"    python nisar_evidence.py --lat {lat} --lon {lon} "
                  f"--start {args.start} --end {args.end}")
        print("\n  S1 판정과 대조해 담수/낙수 전환점의 HH 값을 확인하면")
        print("  NISAR 절대 임계값을 도출할 수 있습니다.")
    else:
        print("  낙수 후보를 찾지 못했습니다. 후보 좌표를 늘리거나, NISAR 관측이")
        print("  더 쌓인 뒤(중간물떼기는 보통 7월 하순~8월) 다시 시도하세요.")

    out = OUTDIR / "nisar_scan.csv"
    OUTDIR.mkdir(exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = _csv.writer(f)
        w.writerow(["이름", "위도", "경도", "관측수", "HH_초기", "HH_후기",
                    "HH_변화", "HHHV_변화", "HH_최소", "HH_최대"])
        for name, lat, lon, t in sorted(results, key=lambda r: r[3]["hh_delta"]):
            w.writerow([name, lat, lon, t["n"], round(t["hh_early"], 2),
                        round(t["hh_late"], 2), round(t["hh_delta"], 2),
                        round(t["ratio_delta"], 2) if t["ratio_delta"] is not None else "",
                        round(t["hh_min"], 2), round(t["hh_max"], 2)])
    print(f"\n  저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
