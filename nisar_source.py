#!/usr/bin/env python3
"""
Phase 3 — NISAR L-band 시계열 추출

지정 좌표의 NISAR GCOV(감마0) HH·HV 시계열을 뽑는다. 그래뉼당 5~6 GB 이지만
HTTP range 요청으로 필요한 픽셀 창(0.2% 미만)만 읽는다.

paddy_check.py 는 건드리지 않는 별도 모듈이다. 결과는 캐시되어 재실행이 빠르다.

    python nisar_source.py --lat 35.9820 --lon 126.9250
    python nisar_source.py --lat 35.9820 --lon 126.9250 --start 2026-06-01 --end 2026-07-31
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTDIR = ROOT / "output"
CACHE = OUTDIR / "nisar_cache.json"
SHORT_NAME = "NISAR_L2_GCOV_PROVISIONAL_V1"
DEFAULT_BUFFER_M = 40.0

_TS = re.compile(r"(\d{8}T\d{6})")


class NisarError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 오류."""


def parse_granule_name(name: str) -> dict:
    """그래뉼 이름에서 관측 시각·궤도 정보를 뽑는다.

    예: NISAR_L2_PR_GCOV_023_053_A_020_4005_DHDH_A_20260617T204648_...
                          ^cycle ^track ^방향 ^frame        ^관측시각
    """
    parts = name.split("_")
    info = {"track": None, "direction": None, "cycle": None}
    if len(parts) > 6:
        info["cycle"] = parts[4]
        info["track"] = parts[5]
        info["direction"] = {"A": "상행", "D": "하행"}.get(parts[6], parts[6])
    if m := _TS.search(name):
        dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
        info["datetime"] = dt
        info["date"] = dt.date()
    return info


def _find(h5, want: str):
    hits = []
    h5.visititems(lambda n, o: hits.append(n)
                  if hasattr(o, "shape") and n.rsplit("/", 1)[-1] == want else None)
    return hits[0] if hits else None


def _window_mean_db(dset, xs, ys, px, py, buffer_m):
    """(px,py) 주변 buffer_m 창의 평균을 dB 로. 유효 화소가 없으면 None."""
    import numpy as np

    if not (min(xs) <= px <= max(xs) and min(ys) <= py <= max(ys)):
        return None, 0, 0
    hx = max(1, int(buffer_m / abs(xs[1] - xs[0])))
    hy = max(1, int(buffer_m / abs(ys[1] - ys[0])))
    ix, iy = int(np.argmin(np.abs(xs - px))), int(np.argmin(np.abs(ys - py)))
    y0, y1 = max(0, iy - hy), min(dset.shape[0], iy + hy + 1)
    x0, x1 = max(0, ix - hx), min(dset.shape[1], ix + hx + 1)

    win = np.asarray(dset[y0:y1, x0:x1], dtype="float64")
    vals = win[np.isfinite(win) & (win > 0)]
    if vals.size == 0:
        return None, 0, win.size
    return 10 * math.log10(float(vals.mean())), int(vals.size), int(win.size)


def load_cache() -> dict:
    if CACHE.is_file():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_cache(cache: dict) -> None:
    OUTDIR.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _earthdata_login(earthaccess):
    """Earthdata 에 로그인한다. 로컬은 ~/.netrc, 서버는 환경변수를 쓴다.

    컨테이너에는 홈 디렉터리에 .netrc 가 없다. 배포 환경에서는
    EARTHDATA_USERNAME / EARTHDATA_PASSWORD 를 넣고, earthaccess 가 읽는
    이름(EARTHDATA_LOGIN) 으로도 맞춰 준다.
    """
    import os  # noqa: PLC0415

    user = os.environ.get("EARTHDATA_USERNAME")
    pw = os.environ.get("EARTHDATA_PASSWORD")

    strategies = []
    if user and pw:
        os.environ.setdefault("EARTHDATA_LOGIN", user)
        strategies.append("environment")
    strategies.append("netrc")

    errors = []
    for how in strategies:
        try:
            auth = earthaccess.login(strategy=how)
        except Exception as exc:
            errors.append(f"{how}: {exc}")
            continue
        if getattr(auth, "authenticated", False):
            return auth
        errors.append(f"{how}: 인증되지 않음")

    raise NisarError(
        "Earthdata 인증에 실패했습니다 (" + " / ".join(errors) + ").\n"
        "  로컬에서는 아래를 한 번 실행하세요:\n"
        '    python -c "import earthaccess; earthaccess.login(persist=True)"\n'
        "  서버에서는 EARTHDATA_USERNAME / EARTHDATA_PASSWORD 환경변수를 넣으세요."
    )


def fetch_timeseries(lat: float, lon: float, start: str, end: str,
                     buffer_m: float = DEFAULT_BUFFER_M,
                     use_cache: bool = True, verbose: bool = True) -> list[dict]:
    """NISAR GCOV HH·HV 시계열을 반환한다."""
    try:
        import earthaccess
        import h5py
        import numpy as np
        from pyproj import Transformer
    except ImportError as exc:
        raise NisarError(
            f"필요한 패키지가 없습니다: {exc}\n"
            "  pip install earthaccess h5py pyproj"
        ) from exc

    auth = _earthdata_login(earthaccess)

    if verbose:
        print(f"[1/3] 그래뉼 검색: {lat}, {lon} | {start} ~ {end}")
    results = earthaccess.search_data(
        short_name=SHORT_NAME, point=(lon, lat), temporal=(start, end))
    if not results:
        raise NisarError(
            f"해당 기간·좌표에 NISAR 그래뉼이 없습니다.\n"
            "  NISAR 공개분은 2026-06-17 이후입니다. 기간을 확인하세요."
        )
    if verbose:
        print(f"      {len(results)}건")

    cache = load_cache() if use_cache else {}
    key_base = f"{lat:.5f},{lon:.5f},{buffer_m:.0f}"
    rows: list[dict] = []

    if verbose:
        print(f"[2/3] 부분 읽기 (반경 {buffer_m:.0f} m)")

    for i, granule in enumerate(results, 1):
        name = granule["meta"]["native-id"]
        info = parse_granule_name(name)
        ckey = f"{key_base},{name}"

        if ckey in cache:
            row = dict(cache[ckey])
            row["cached"] = True
            rows.append(row)
            if verbose:
                print(f"      [{i}/{len(results)}] {row['date']} (캐시)")
            continue

        t0 = time.time()
        try:
            fp = earthaccess.open([granule])[0]
            with h5py.File(fp, "r") as h5:
                xs = h5[_find(h5, "xCoordinates")][:]
                ys = h5[_find(h5, "yCoordinates")][:]
                proj = h5[_find(h5, "projection")]
                epsg = int(np.asarray(proj.attrs.get("epsg_code")).item())
                tf = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
                px, py = tf.transform(lon, lat)

                hh_p, hv_p = _find(h5, "HHHH"), _find(h5, "HVHV")
                hh, n, total = _window_mean_db(h5[hh_p], xs, ys, px, py, buffer_m)
                hv = (_window_mean_db(h5[hv_p], xs, ys, px, py, buffer_m)[0]
                      if hv_p else None)
        except Exception as exc:
            if verbose:
                print(f"      [{i}/{len(results)}] 실패: {str(exc)[:60]}")
            continue

        if hh is None:
            if verbose:
                print(f"      [{i}/{len(results)}] {info.get('date')} "
                      f"무효값(관측 범위 밖) — 건너뜀")
            continue

        row = {
            "date": info["date"].isoformat(),
            "datetime": info["datetime"].isoformat(),
            "hh_db": round(hh, 3),
            "hv_db": round(hv, 3) if hv is not None else None,
            "track": info["track"],
            "direction": info["direction"],
            "n_valid": n, "n_total": total,
            "granule": name,
        }
        rows.append(row)
        cache[ckey] = row
        if verbose:
            print(f"      [{i}/{len(results)}] {row['date']} "
                  f"HH {hh:+.2f} dB  HV {hv:+.2f} dB  "
                  f"궤도{info['track']}{info['direction']}  ({time.time()-t0:.0f}초)")

    if use_cache:
        save_cache(cache)
    if not rows:
        raise NisarError("유효한 관측을 하나도 얻지 못했습니다.")

    rows.sort(key=lambda r: r["date"])
    if verbose:
        print(f"[3/3] 유효 관측 {len(rows)}건")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="NISAR L-band 시계열 추출")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--buffer", type=float, default=DEFAULT_BUFFER_M)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", help="CSV 저장 경로")
    args = ap.parse_args()

    try:
        rows = fetch_timeseries(args.lat, args.lon, args.start, args.end,
                                args.buffer, use_cache=not args.no_cache)
    except NisarError as exc:
        print(f"\n[오류] {exc}", file=sys.stderr)
        return 1

    print(f"\n{'날짜':<12}{'HH (dB)':>10}{'HV (dB)':>10}{'HH-HV':>9}  궤도")
    print("-" * 52)
    for r in rows:
        hv = r["hv_db"]
        print(f"{r['date']:<12}{r['hh_db']:>10.2f}"
              f"{(f'{hv:.2f}' if hv is not None else '-'):>10}"
              f"{(f'{r['hh_db']-hv:+.2f}' if hv is not None else '-'):>9}"
              f"  {r['track']}{r['direction']}")

    out = Path(args.out) if args.out else (
        OUTDIR / f"nisar_{args.lat:.4f}_{args.lon:.4f}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    import csv as _csv
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
