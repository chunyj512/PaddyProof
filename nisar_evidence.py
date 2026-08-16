#!/usr/bin/env python3
"""
Phase 5 — NISAR 교차 증거 모듈

S1 판정을 그대로 두고, NISAR L-band 로 그 판정이 캐노피 성장에 속은 것은
아닌지 검증한다. paddy_check.py 는 건드리지 않는다.

절대 임계값이 아직 없으므로 **같은 필지 안의 상대 비교**를 쓴다.

  S1 이 '낙수' 라고 본 날들의 NISAR HH 평균
    vs
  S1 이 '담수' 라고 본 날들의 NISAR HH 평균

실제로 물을 뺐다면 수면-줄기 이중반사가 사라져 HH 가 내려가야 한다.
반대로 올라갔다면 S1 의 상승은 낙수가 아니라 캐노피 성장 때문이다.

    python nisar_evidence.py --lat 35.9820 --lon 126.9250 \
        --start 2026-06-17 --end 2026-07-23
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTDIR = ROOT / "output"

# NISAR 공개 개시일. 이보다 이른 기간은 교차 검증이 불가능하다.
NISAR_EPOCH = date(2026, 6, 17)

# 두 그룹의 HH 평균 차이가 이보다 작으면 판단을 보류한다 (궤도 혼재 노이즈 수준).
MARGIN_DB = 1.0
MIN_OBS_PER_GROUP = 2

VERDICT_AGREE = "agree"                    # 두 센서 일치
VERDICT_S1_OVEREST = "s1_overestimate"     # S1 과대판정 의심
VERDICT_UNCLEAR = "unclear"                # 판단 보류
VERDICT_NO_DATA = "no_data"                # 교차 검증 불가


def s1_state_on(dates, states, target) -> str | None:
    """관측 사이 구간은 직전 관측 상태가 유지된 것으로 보고, 해당 날짜의 S1 상태를 찾는다."""
    prev = None
    for d, s in zip(dates, states):
        if d > target:
            break
        prev = s
    return prev


def cross_check(lat: float, lon: float, s1_csv: Path,
                buffer_m: float = 40.0, verbose: bool = True) -> dict:
    """S1 결과와 NISAR 시계열을 대조해 교차 증거를 만든다."""
    import pandas as pd

    sys.path.insert(0, str(ROOT))
    from nisar_source import NisarError, fetch_timeseries

    s1 = pd.read_csv(s1_csv, encoding="utf-8-sig")
    s1["date"] = pd.to_datetime(s1["date"])
    s1_dates = [d.date() for d in s1["date"]]
    s1_states = list(s1["state"])

    win_start = max(min(s1_dates), NISAR_EPOCH)
    win_end = max(s1_dates)

    if win_start > win_end:
        return {
            "verdict": VERDICT_NO_DATA,
            "reason": (f"분석 기간이 NISAR 공개 개시일({NISAR_EPOCH}) 이전입니다. "
                       f"교차 검증을 할 수 없습니다."),
            "rows": [],
        }

    try:
        rows = fetch_timeseries(lat, lon, win_start.isoformat(), win_end.isoformat(),
                                buffer_m=buffer_m, verbose=verbose)
    except NisarError as exc:
        return {"verdict": VERDICT_NO_DATA, "reason": str(exc), "rows": []}

    ni = pd.DataFrame(rows)
    ni["date"] = pd.to_datetime(ni["date"])
    ni = ni.groupby("date", as_index=False).agg({"hh_db": "mean", "hv_db": "mean"})
    ni["hh_hv"] = ni["hh_db"] - ni["hv_db"]
    ni["s1_state"] = [s1_state_on(s1_dates, s1_states, d.date()) for d in ni["date"]]

    drained = ni[ni["s1_state"] == "낙수"]
    ponded = ni[ni["s1_state"] == "담수"]

    result = {
        "window": (win_start.isoformat(), win_end.isoformat()),
        "n_nisar": len(ni),
        "n_drained": len(drained),
        "n_ponded": len(ponded),
        "rows": ni.to_dict("records"),
    }

    if len(drained) < MIN_OBS_PER_GROUP or len(ponded) < MIN_OBS_PER_GROUP:
        result.update({
            "verdict": VERDICT_UNCLEAR,
            "reason": (f"비교에 필요한 관측이 부족합니다 "
                       f"(S1 낙수 시점 {len(drained)}건 / 담수 시점 {len(ponded)}건, "
                       f"각 {MIN_OBS_PER_GROUP}건 이상 필요)."),
        })
        return result

    hh_d = float(drained["hh_db"].mean())
    hh_p = float(ponded["hh_db"].mean())
    diff = hh_d - hh_p
    result.update({"hh_drained": hh_d, "hh_ponded": hh_p, "hh_diff": diff})

    if diff > MARGIN_DB:
        result.update({
            "verdict": VERDICT_S1_OVEREST,
            "reason": (f"S1 이 낙수라고 본 시점의 NISAR HH({hh_d:+.2f} dB)가 "
                       f"담수라고 본 시점({hh_p:+.2f} dB)보다 {diff:+.2f} dB 높습니다. "
                       f"물을 뺐다면 이중반사가 약해져 내려가야 하므로, S1 의 상승은 "
                       f"낙수가 아니라 벼 캐노피 성장 때문일 가능성이 큽니다."),
        })
    elif diff < -MARGIN_DB:
        result.update({
            "verdict": VERDICT_AGREE,
            "reason": (f"S1 이 낙수라고 본 시점의 NISAR HH({hh_d:+.2f} dB)가 "
                       f"담수 시점({hh_p:+.2f} dB)보다 {diff:+.2f} dB 낮습니다. "
                       f"이중반사가 약해진 것으로, 실제 낙수와 부합합니다."),
        })
    else:
        result.update({
            "verdict": VERDICT_UNCLEAR,
            "reason": (f"두 시점의 NISAR HH 차이가 {diff:+.2f} dB 로 "
                       f"판단 기준({MARGIN_DB} dB) 안에 있습니다. 궤도 혼재 노이즈와 "
                       f"구분되지 않아 판단을 보류합니다."),
        })
    return result


def to_markdown(res: dict) -> str:
    """리포트에 덧붙일 마크다운 섹션."""
    label = {
        VERDICT_S1_OVEREST: "S1 과대판정 의심",
        VERDICT_AGREE: "두 센서 일치",
        VERDICT_UNCLEAR: "판단 보류",
        VERDICT_NO_DATA: "교차 검증 불가",
    }[res["verdict"]]

    L = [
        "## 7. NISAR L-band 교차 증거",
        "",
        f"### 결과: **{label}**",
        "",
        res["reason"],
        "",
    ]

    if res["verdict"] == VERDICT_NO_DATA:
        L += ["> NISAR 공개분은 2026-06-17 이후입니다. 그 이전 기간은 Sentinel-1",
              "> 단독 판정만 가능합니다.", ""]
        return "\n".join(L)

    if "hh_diff" in res:
        L += [
            "| 구분 | NISAR HH 평균 | 관측 수 |",
            "| --- | --- | --- |",
            f"| S1 담수 판정 시점 | {res['hh_ponded']:+.2f} dB | {res['n_ponded']}건 |",
            f"| S1 낙수 판정 시점 | {res['hh_drained']:+.2f} dB | {res['n_drained']}건 |",
            f"| 차이 | {res['hh_diff']:+.2f} dB | |",
            "",
        ]

    L += ["### NISAR 관측값", "",
          "| 날짜 | HH (dB) | HV (dB) | HH-HV | 같은 날 S1 판정 |",
          "| --- | --- | --- | --- | --- |"]
    for r in res["rows"]:
        d = str(r["date"])[:10]
        L.append(f"| {d} | {r['hh_db']:+.2f} | {r['hv_db']:+.2f} | "
                 f"{r['hh_hv']:+.2f} | {r['s1_state'] or '-'} |")
    L += [
        "",
        "### 원리와 한계",
        "",
        "L-band(24 cm)는 C-band(5.6 cm)와 달리 벼 캐노피를 투과해 수면과 줄기가",
        "만드는 이중반사를 봅니다. 담수 상태에서 벼가 자라면 이 신호가 오히려",
        "강해지므로, S1 이 '낙수'로 본 구간에서 HH 가 올라갔다면 실제로는 물이",
        "차 있었을 가능성이 큽니다.",
        "",
        "다만 다음 한계가 있습니다.",
        "",
        "1. **절대 임계값 없음** — NISAR 단독으로 담수/낙수를 판정하지 않습니다.",
        "   같은 필지 안의 상대 비교만 하므로, 판정 주체는 여전히 Sentinel-1 입니다.",
        f"2. **관측 기간 제한** — NISAR 공개분은 {NISAR_EPOCH} 이후입니다.",
        "3. **궤도 혼재** — 서로 다른 트랙·입사각 관측이 섞여 있어 수 dB 의 계통",
        f"   차이가 있을 수 있습니다. 그래서 {MARGIN_DB} dB 이내 차이는 보류합니다.",
        "4. **제품 성숙도** — Provisional 제품이라 보정값이 바뀔 수 있습니다.",
        "",
    ]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="NISAR 교차 증거 생성")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--buffer", type=float, default=40.0)
    ap.add_argument("--append", action="store_true",
                    help="기존 S1 리포트(.md) 끝에 교차 증거 섹션을 덧붙인다")
    args = ap.parse_args()

    stem = (f"paddy_{args.lat:.4f}_{args.lon:.4f}_"
            f"{args.start.replace('-','')}_{args.end.replace('-','')}")
    csv_path = OUTDIR / f"{stem}.csv"
    if not csv_path.is_file():
        print(f"[오류] S1 결과가 없습니다: {csv_path.name}\n"
              f"  먼저 실행하세요:\n"
              f"  python paddy_check.py --lat {args.lat} --lon {args.lon} "
              f"--start {args.start} --end {args.end} --csv", file=sys.stderr)
        return 1

    res = cross_check(args.lat, args.lon, csv_path, args.buffer)
    md = to_markdown(res)
    print("\n" + md)

    if args.append:
        md_path = OUTDIR / f"{stem}.md"
        if md_path.is_file():
            text = md_path.read_text(encoding="utf-8")
            marker = "## 7. NISAR L-band 교차 증거"
            if marker in text:
                text = text[:text.index(marker)]
            md_path.write_text(text.rstrip() + "\n\n" + md, encoding="utf-8")
            print(f"\n리포트에 추가: {md_path}")

    return 0 if res["verdict"] != VERDICT_NO_DATA else 1


if __name__ == "__main__":
    sys.exit(main())
