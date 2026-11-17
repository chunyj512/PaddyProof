#!/usr/bin/env python3
"""
웹 서버 대신 무거운 계산을 맡는 일회용 작업 프로세스.

pandas·matplotlib·earthaccess·h5py 를 웹 서버 프로세스에서 import 하면, 첫
분석 이후 서버가 그 메모리(약 200 MB)를 영영 돌려주지 않는다. 여기에
paddy_check 서브프로세스가 겹치면 512 MB 인스턴스가 한도를 넘어 재시작된다.
그래서 서버는 이 스크립트를 서브프로세스로 부르고, 일이 끝나면 프로세스와
함께 메모리가 통째로 반환된다.

    python web/worker.py '<json>'      # 결과는 마지막 줄에 RESULT_PREFIX + JSON

모드
  cross        : NISAR 교차 증거 + 통합 그래프 (분석 작업 직후)
  nisar        : /api/nisar — 교차 증거 + 비교 그래프
  sensitivity  : /api/sensitivity — 임계값 ±1 dB 민감도
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "output"
RESULT_PREFIX = "@@RESULT@@"

sys.path.insert(0, str(ROOT))


def _rows_to_str_dates(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["date"] = str(r["date"])[:10]
    return rows


def cross(a: dict) -> dict:
    """NISAR 교차 증거를 붙이고 두 센서를 겹쳐 그린 통합 그래프를 만든다.

    통합 그래프는 NISAR 를 못 얻어도 그린다. 그래야 결과 화면에 늘 같은 그래프가
    놓이고, '교차 검증을 아직 안 한 것' 과 '할 수 없는 것' 이 구분된다.
    """
    out: dict = {"logs": []}
    csv_path = OUTDIR / a["csv"]

    rows = None
    try:
        from nisar_evidence import cross_check
        res = cross_check(a["lat"], a["lon"], csv_path,
                          buffer_m=float(a["buffer"]), verbose=False)
        rows = _rows_to_str_dates(res.get("rows") or [])
        detail = {k: res[k] for k in
                  ("reason", "hh_drained", "hh_ponded", "hh_diff", "n_ponded",
                   "n_drained", "n_nisar", "timeline") if k in res}
        detail["rows"] = rows
        out.update(nisar=res["verdict"], nisar_reason=res.get("reason"),
                   nisar_detail=detail)
    except Exception as exc:
        out.update(nisar="error", nisar_reason=str(exc)[:200])
        out["logs"].append(f"      NISAR 실패: {str(exc)[:60]}")

    try:
        from nisar_compare import render_combined_png
        name = f"combined_{Path(a['csv']).stem}.png"
        res = render_combined_png(
            a["lat"], a["lon"], csv_path, OUTDIR / name,
            threshold=float(a["threshold"]), min_days=int(a["min_days"]),
            nisar_rows=rows, buffer_m=float(a["buffer"]), verbose=False,
        )
        out["combined_png"] = res["png"]
        out["logs"].append(f"      저장: {res['png']}")
    except Exception as exc:
        out["logs"].append(f"      통합 그래프 실패: {str(exc)[:80]}")
    return out


def nisar(a: dict) -> dict:
    from nisar_evidence import cross_check

    name = a["csv"]
    res = cross_check(a["lat"], a["lon"], OUTDIR / name,
                      buffer_m=float(a["buffer"]), verbose=False)
    _rows_to_str_dates(res.get("rows", []))

    # 관측이 있으면 S1+NISAR 통합 비교 그래프도 만들어 준다 (캐시라 수 초).
    if res.get("rows"):
        try:
            from nisar_compare import render_compare_png
            png_name = f"nisarcmp_{Path(name).stem}.png"
            render_compare_png(a["lat"], a["lon"], OUTDIR / name, OUTDIR / png_name)
            res["png"] = png_name
        except Exception:
            pass  # 그래프는 부가 정보 — 실패해도 교차 증거는 그대로 반환한다
    return res


def sensitivity(a: dict) -> dict:
    """임계값을 ±1 dB 흔들어 판정이 유지되는지 확인한다."""
    import pandas as pd
    from paddy_check import classify, find_drain_periods

    df = pd.read_csv(OUTDIR / a["csv"], encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"])

    base, min_days = float(a["threshold"]), int(a["min_days"])
    out = []
    for th in sorted({round(base - 1, 2), round(base - 0.5, 2), base,
                      round(base + 0.5, 2), round(base + 1, 2)}):
        periods = find_drain_periods(classify(df, th))
        longest = max((p.days for p in periods), default=0)
        out.append({
            "threshold": th,
            "longest": longest,
            "verdict": "이행" if longest >= min_days else "미이행",
            "is_base": th == base,
        })
    return {"rows": out}


MODES = {"cross": cross, "nisar": nisar, "sensitivity": sensitivity}


def main() -> int:
    args = json.loads(sys.argv[1])
    try:
        result = {"ok": True, "value": MODES[args.pop("mode")](args)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:500]}
    # 라이브러리가 stdout 에 무엇을 찍든 마지막 줄만 결과로 읽는다.
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
