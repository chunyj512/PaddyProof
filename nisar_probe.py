#!/usr/bin/env python3
"""
Phase 1 — NISAR 부분 읽기 타당성 프로브 (go/no-go 관문)

NISAR GCOV 한 그래뉼은 5~6 GB다. 논 한 필지(반경 40 m)를 보려고 전체를 받는 건
비현실적이므로, HTTP range 요청으로 필요한 픽셀 창만 읽을 수 있는지 확인한다.

측정 항목: 실제 전송 바이트, 소요 시간, 읽어온 값의 타당성.

    python nisar_probe.py
"""

from __future__ import annotations

import math
import sys
import time
from datetime import datetime

LAT, LON = 35.9820, 126.9250      # 익산 논 (기존 검증 필지)
BUFFER_M = 40.0
SHORT_NAME = "NISAR_L2_GCOV_PROVISIONAL_V1"
TEMPORAL = ("2026-06-17", "2026-07-24")

# go/no-go 기준
MAX_MB = 500.0          # 그래뉼당 이보다 많이 받으면 부분 읽기 실패로 본다
MAX_SECONDS = 300.0


class ByteCounter:
    """파일 객체를 감싸 실제 전송량을 센다."""

    def __init__(self, fp):
        self._fp = fp
        self.bytes_read = 0
        self.n_reads = 0

    def read(self, size=-1):
        data = self._fp.read(size)
        self.bytes_read += len(data)
        self.n_reads += 1
        return data

    def readinto(self, buf):
        # h5py 는 성능상 readinto 를 쓴다. 이걸 빠뜨리면 전송량이 0으로 집계된다.
        n = self._fp.readinto(buf)
        self.bytes_read += n or 0
        self.n_reads += 1
        return n

    def __getattr__(self, name):
        return getattr(self._fp, name)


def find_dataset(h5, want: str) -> str | None:
    """GCOV 계층에서 지정한 이름의 데이터셋 경로를 찾는다."""
    hits = []

    def visit(name, obj):
        if hasattr(obj, "shape") and name.rsplit("/", 1)[-1] == want:
            hits.append(name)

    h5.visititems(visit)
    return hits[0] if hits else None


def main() -> int:
    import earthaccess
    import h5py
    import numpy as np

    print("=" * 68)
    print("  Phase 1 — NISAR 부분 읽기 프로브")
    print("=" * 68)

    # --- 인증 -------------------------------------------------------------
    print("\n[1/5] Earthdata 인증")
    try:
        auth = earthaccess.login(strategy="netrc")
    except Exception as exc:
        print(f"  실패: {exc}")
        print("  먼저 실행하세요:")
        print('    .venv/bin/python -c "import earthaccess; earthaccess.login(persist=True)"')
        return 1
    if not getattr(auth, "authenticated", False):
        print("  인증되지 않았습니다. 위 명령으로 로그인하세요.")
        return 1
    print("  인증 완료")

    # --- 검색 -------------------------------------------------------------
    print(f"\n[2/5] 그래뉼 검색 ({SHORT_NAME})")
    print(f"  좌표 {LAT}, {LON} | 기간 {TEMPORAL[0]} ~ {TEMPORAL[1]}")
    results = earthaccess.search_data(
        short_name=SHORT_NAME, point=(LON, LAT), temporal=TEMPORAL,
    )
    if not results:
        print("  해당 조건에 그래뉼이 없습니다.")
        return 1
    print(f"  {len(results)}건 발견")
    for g in results[:5]:
        name = g["meta"]["native-id"]
        size = float(g.size()) if callable(getattr(g, "size", None)) else 0.0
        print(f"    - {name[:62]}  ({size:,.0f} MB)")

    granule = results[0]
    full_mb = float(granule.size()) if callable(getattr(granule, "size", None)) else 0.0

    # --- 원격 열기 --------------------------------------------------------
    print(f"\n[3/5] 원격 열기 (다운로드 없이)")
    t0 = time.time()
    try:
        fps = earthaccess.open([granule])
    except Exception as exc:
        print(f"  실패: {exc}")
        return 1
    if not fps:
        print("  파일 핸들을 얻지 못했습니다.")
        return 1

    counter = ByteCounter(fps[0])
    try:
        h5 = h5py.File(counter, "r")
    except Exception as exc:
        print(f"  HDF5 열기 실패: {exc}")
        print("  → 부분 읽기 불가. 대안(프레임 캐시)을 검토해야 합니다.")
        return 2
    open_mb = counter.bytes_read / 1e6
    print(f"  열기 성공 — 메타데이터 {open_mb:.1f} MB, {time.time() - t0:.1f}초")

    # --- 좌표축 읽기 ------------------------------------------------------
    print("\n[4/5] 격자 좌표 확인")
    xpath = find_dataset(h5, "xCoordinates")
    ypath = find_dataset(h5, "yCoordinates")
    if not xpath or not ypath:
        print("  좌표 데이터셋을 찾지 못했습니다. 구조를 확인하세요:")
        h5.visititems(lambda n, o: print("   ", n) if hasattr(o, "shape") else None)
        return 2

    xs = h5[xpath][:]
    ys = h5[ypath][:]
    print(f"  격자 {len(ys)} x {len(xs)}")
    print(f"  x {xs[0]:,.0f} ~ {xs[-1]:,.0f} | y {ys[0]:,.0f} ~ {ys[-1]:,.0f}")

    # 투영 정보 → 위경도를 격자 좌표로 변환
    epsg = None
    proj_path = find_dataset(h5, "projection")
    if proj_path:
        node = h5[proj_path]
        epsg = node.attrs.get("epsg_code") or node.attrs.get("spatial_ref")
        try:
            epsg = int(np.asarray(epsg).item())
        except Exception:
            epsg = None
    print(f"  EPSG: {epsg or '미확인'}")

    from pyproj import Transformer
    if epsg:
        tf = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        px, py = tf.transform(LON, LAT)
    else:
        px, py = LON, LAT
    print(f"  필지 좌표 → 격자 ({px:,.0f}, {py:,.0f})")

    if not (min(xs) <= px <= max(xs) and min(ys) <= py <= max(ys)):
        print("  주의: 필지가 이 그래뉼 범위 밖입니다. 다른 그래뉼을 써야 합니다.")
        return 2

    # --- 부분 읽기 --------------------------------------------------------
    print(f"\n[5/5] 반경 {BUFFER_M:.0f} m 창만 읽기")
    band = find_dataset(h5, "HHHH") or find_dataset(h5, "VVVV")
    if not band:
        print("  편파 데이터셋(HHHH/VVVV)을 찾지 못했습니다.")
        return 2
    dset = h5[band]
    print(f"  대상: {band}  shape={dset.shape}  dtype={dset.dtype}")
    print(f"  청크: {dset.chunks}  압축: {dset.compression}")

    res_x = abs(xs[1] - xs[0])
    res_y = abs(ys[1] - ys[0])
    half_x = max(1, int(BUFFER_M / res_x))
    half_y = max(1, int(BUFFER_M / res_y))
    ix = int(np.argmin(np.abs(xs - px)))
    iy = int(np.argmin(np.abs(ys - py)))
    y0, y1 = max(0, iy - half_y), min(dset.shape[0], iy + half_y + 1)
    x0, x1 = max(0, ix - half_x), min(dset.shape[1], ix + half_x + 1)
    print(f"  해상도 {res_x:.0f} m | 슬라이스 [{y0}:{y1}, {x0}:{x1}]"
          f" = {(y1-y0)}x{(x1-x0)} 픽셀")

    before = counter.bytes_read
    t1 = time.time()
    window = dset[y0:y1, x0:x1]
    read_mb = (counter.bytes_read - before) / 1e6
    elapsed = time.time() - t1

    vals = np.asarray(window, dtype="float64")
    vals = vals[np.isfinite(vals) & (vals > 0)]
    print(f"  전송 {read_mb:.1f} MB, {elapsed:.1f}초")
    if vals.size:
        mean = float(vals.mean())
        print(f"  유효 픽셀 {vals.size} / {window.size}")
        print(f"  평균 감마0 {mean:.6f}  →  {10 * math.log10(mean):+.2f} dB")
    else:
        print("  유효 픽셀 없음 (구름 아님 — SAR이므로 무효값/영역 밖 가능성)")

    total_mb = counter.bytes_read / 1e6
    total_s = time.time() - t0

    # --- 판정 -------------------------------------------------------------
    print("\n" + "=" * 68)
    print(f"  전체 파일 {full_mb:,.0f} MB 중 {total_mb:.1f} MB 전송 "
          f"({total_mb / full_mb * 100:.2f}%)")
    print(f"  총 {total_s:.1f}초, HTTP 읽기 {counter.n_reads}회")
    ok = total_mb <= MAX_MB and total_s <= MAX_SECONDS and vals.size > 0
    print(f"  판정: {'GO — Phase 2 진행 가능' if ok else 'NO-GO — 대안 검토 필요'}")
    print("=" * 68)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
