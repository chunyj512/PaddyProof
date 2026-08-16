#!/usr/bin/env python3
"""
Phase 2 — NISAR L-band 물리 검증

S1 검증 때와 같은 방식으로, 지표 유형별 후방산란이 물리적으로 타당한지 본다.
L-band 는 C-band 와 거동이 다르므로 기대값도 다르다.

  개활수면  : 경면반사 → HH 매우 낮음 (C-band 와 동일)
  도심      : 이중반사 → HH 매우 높음
  담수 벼논 : 수면-줄기 이중반사 → HH 높고 HV 는 상대적으로 낮음
              (C-band 라면 캐노피 산란으로 HV 가 올라간다)

한 그래뉼을 한 번만 열고 여러 지점을 읽어 전송량을 아낀다.

    python nisar_phase2.py
"""

from __future__ import annotations

import math
import sys
import time

BUFFER_M = 40.0
SHORT_NAME = "NISAR_L2_GCOV_PROVISIONAL_V1"

SITES = {
    # 수면은 그래뉼 무효영역에 걸릴 수 있어 후보를 여러 개 둔다
    "서해 (군산 앞바다)":  (35.9500, 126.4500),
    "새만금호 (내수면)":   (35.8000, 126.6500),
    "대청호 (개활수면)":   (36.4800, 127.4900),
    "대전 둔산 (도심)":    (36.3721, 127.3604),
    "익산 (논)":          (35.9820, 126.9250),
    "논산 (논)":          (36.1600, 127.0500),
    "김제 (논)":          (35.8600, 126.9500),
}

# S1 에서 확인한 같은 지점의 VH 값 (비교용, 2025 재배기)
S1_REFERENCE = {
    "대청호 (개활수면)": -28.5,
    "대전 둔산 (도심)": -20.0,
    "익산 (논)": None,
    "논산 (논)": None,
    "김제 (논)": None,
}


def find_dataset(h5, want: str):
    hits = []
    h5.visititems(lambda n, o: hits.append(n)
                  if hasattr(o, "shape") and n.rsplit("/", 1)[-1] == want else None)
    return hits[0] if hits else None


def sample(dset, xs, ys, px, py, buffer_m: float):
    """격자에서 (px,py) 주변 buffer_m 창의 평균을 dB 로 반환."""
    import numpy as np

    if not (min(xs) <= px <= max(xs) and min(ys) <= py <= max(ys)):
        return None, 0, 0
    res_x, res_y = abs(xs[1] - xs[0]), abs(ys[1] - ys[0])
    hx, hy = max(1, int(buffer_m / res_x)), max(1, int(buffer_m / res_y))
    ix, iy = int(np.argmin(np.abs(xs - px))), int(np.argmin(np.abs(ys - py)))
    y0, y1 = max(0, iy - hy), min(dset.shape[0], iy + hy + 1)
    x0, x1 = max(0, ix - hx), min(dset.shape[1], ix + hx + 1)

    win = np.asarray(dset[y0:y1, x0:x1], dtype="float64")
    total = win.size
    vals = win[np.isfinite(win) & (win > 0)]
    if vals.size == 0:
        return None, 0, total
    return 10 * math.log10(float(vals.mean())), vals.size, total


def main() -> int:
    import earthaccess
    import h5py
    import numpy as np
    from pyproj import Transformer

    print("=" * 72)
    print("  Phase 2 — NISAR L-band 물리 검증")
    print("=" * 72)

    earthaccess.login(strategy="netrc")

    # 여러 지점을 한 장면에 담으려면 넓게 걸치는 그래뉼이 필요하다.
    # 익산 기준으로 찾은 뒤 각 지점이 범위 안에 드는지 개별 확인한다.
    results = earthaccess.search_data(
        short_name=SHORT_NAME, point=(126.9250, 35.9820),
        temporal=("2026-06-17", "2026-07-24"),
    )
    if not results:
        print("그래뉼을 찾지 못했습니다.")
        return 1

    granule = results[0]
    gid = granule["meta"]["native-id"]
    print(f"\n대상 그래뉼: {gid}")
    print(f"관측 시각: {gid.split('_')[-2]}")

    t0 = time.time()
    fp = earthaccess.open([granule])[0]
    h5 = h5py.File(fp, "r")
    print(f"열기 완료 ({time.time() - t0:.1f}초)\n")

    xs = h5[find_dataset(h5, "xCoordinates")][:]
    ys = h5[find_dataset(h5, "yCoordinates")][:]
    proj = h5[find_dataset(h5, "projection")]
    epsg = int(np.asarray(proj.attrs.get("epsg_code")).item())
    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    hh_path = find_dataset(h5, "HHHH")
    hv_path = find_dataset(h5, "HVHV")
    print(f"밴드: HH={bool(hh_path)}  HV={bool(hv_path)}  (EPSG:{epsg}, "
          f"{abs(xs[1]-xs[0]):.0f} m 격자)\n")
    hh, hv = h5[hh_path], (h5[hv_path] if hv_path else None)

    print(f"{'지점':<20}{'HH (dB)':>10}{'HV (dB)':>10}{'HH-HV':>9}"
          f"{'유효화소':>10}   {'S1 VH 참고':>10}")
    print("-" * 72)

    rows = []
    for name, (lat, lon) in SITES.items():
        px, py = tf.transform(lon, lat)
        hh_db, n, total = sample(hh, xs, ys, px, py, BUFFER_M)
        if hh_db is None:
            print(f"{name:<20}{'범위 밖 또는 무효값':>40}")
            continue
        hv_db = sample(hv, xs, ys, px, py, BUFFER_M)[0] if hv is not None else None
        ref = S1_REFERENCE.get(name)
        print(f"{name:<20}{hh_db:>10.2f}"
              f"{(f'{hv_db:.2f}' if hv_db is not None else '-'):>10}"
              f"{(f'{hh_db - hv_db:+.2f}' if hv_db is not None else '-'):>9}"
              f"{f'{n}/{total}':>10}   {(f'{ref:.1f}' if ref else '-'):>10}")
        rows.append((name, hh_db, hv_db))

    print("\n" + "=" * 72)
    print("  해석")
    print("=" * 72)

    waters = [r for r in rows if "수면" in r[0] or "서해" in r[0]]
    city = next((r for r in rows if "도심" in r[0]), None)
    paddies = [r for r in rows if "논" in r[0]]

    if not waters:
        print("  [!] 수면 기준점을 얻지 못했습니다. 물리 검증의 핵심 앵커가 빠졌으므로")
        print("      이 그래뉼만으로는 검증이 불완전합니다.")
    else:
        w = min(waters, key=lambda r: r[1])
        if city:
            ok = w[1] < city[1] - 5
            print(f"  [{'O' if ok else 'X'}] 수면({w[1]:.1f}) < 도심({city[1]:.1f}) "
                  f"— {'정상' if ok else '이상: 물리적으로 맞지 않음'}")
        if paddies:
            avg = sum(p[1] for p in paddies) / len(paddies)
            ok = w[1] < avg
            print(f"  [{'O' if ok else 'X'}] 수면({w[1]:.1f}) < 논 평균({avg:.1f}) "
                  f"— {'정상' if ok else '이상'}")

    if paddies and all(p[2] is not None for p in paddies):
        print("\n  HH-HV 편차 (참고용):")
        for name, a, b in rows:
            if b is None:
                continue
            print(f"    {name:<20} {a - b:+.2f} dB")
        print("\n  주의: 도심도 논과 비슷한 HH-HV 편차를 보이므로, 이 값 하나로는")
        print("        담수 여부를 가릴 수 없습니다. 절대값·시계열 변화와 함께 봐야 합니다.")

    print("\n  이 결과는 관측 1회분입니다. 판정 임계값은 시계열(Phase 3)에서 같은")
    print("  필지의 담수→낙수 전환을 관찰한 뒤에야 정할 수 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
