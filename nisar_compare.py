#!/usr/bin/env python3
"""
S1 C-band vs NISAR L-band 교차 검증 — 한 장의 그래프

두 센서를 위아래로 나눠 그리면 독자가 눈을 옮겨 가며 시점을 맞춰야 한다.
교차 검증의 요점은 "같은 날 두 센서가 서로 다른 말을 하는가" 이므로,
같은 시간축·같은 단위(dB) 위에 겹쳐 그린다.

  C-band VH : 담수면은 경면반사로 낮음. 벼가 자라면 캐노피 산란으로 올라감.
  L-band HH : 캐노피를 투과해 수면-줄기 이중반사를 봄. 담수 상태에서 오히려 높음.

따라서 S1 VH 와 NISAR HH 가 **함께 올라가면** 그 상승은 낙수가 아니라
캐노피 성장 때문이다. 그래프 한 장에서 이 어긋남이 바로 보여야 한다.

    python nisar_compare.py --lat 35.9820 --lon 126.9250 --start 2026-06-17 --end 2026-07-23
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTDIR = ROOT / "output"

# NISAR 공개 개시일. 이보다 이른 구간은 L-band 근거가 없다.
NISAR_EPOCH = date(2026, 6, 17)

# 색상은 dataviz 검증(전 쌍 CVD ΔE 9.2, 정상시 ΔE 24.0)을 통과한 조합이다.
C_S1 = "#2a78d6"        # Sentinel-1 VH — 판정 주체
C_NISAR = "#1baf7a"     # NISAR HH — 교차 증거
C_NISAR_HV = "#8FBFAE"  # NISAR HV — 보조 (식별이 아니라 맥락 제공)
C_THRESH = "#C62828"
C_DRAIN = "#F5A623"
C_POND = "#4A90D9"
C_INK = "#1b2330"
C_MUTED = "#6b7480"


def _drain_periods(s1, threshold: float):
    """S1 CSV 에서 연속 낙수 구간을 다시 계산한다 (임계값이 바뀔 수 있으므로)."""
    sys.path.insert(0, str(ROOT))
    from paddy_check import classify, find_drain_periods
    return find_drain_periods(classify(s1, threshold))


def render_combined_png(
    lat: float,
    lon: float,
    s1_csv: Path,
    out_png: Path,
    threshold: float = -20.0,
    min_days: int = 14,
    nisar_rows: list[dict] | None = None,
    buffer_m: float = 40.0,
    verbose: bool = False,
) -> dict:
    """S1 과 NISAR 를 한 축에 겹쳐 그린다.

    nisar_rows 를 주면 그것을 쓰고, 없으면 직접 조회한다. NISAR 를 얻지 못해도
    S1 단독으로 그래프를 그리고 그 사실을 그래프 안에 적는다 — 교차 검증이
    불가능한 상황과 아직 안 해 본 상황을 독자가 구분할 수 있어야 하기 때문이다.

    반환: {"png": 파일명, "n_nisar": 관측 수, "note": 화면에 덧붙일 설명}
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import pandas as pd

    sys.path.insert(0, str(ROOT))
    from paddy_check import setup_korean_font

    s1 = pd.read_csv(s1_csv, encoding="utf-8-sig")
    s1["date"] = pd.to_datetime(s1["date"])

    ni = None
    note = ""
    if nisar_rows is None:
        from nisar_source import NisarError, fetch_timeseries
        win_start = max(s1["date"].min().date(), NISAR_EPOCH)
        win_end = s1["date"].max().date()
        if win_start > win_end:
            note = (f"분석 기간이 NISAR 공개 개시일({NISAR_EPOCH}) 이전이라 "
                    f"L-band 교차 검증을 할 수 없습니다.")
        else:
            try:
                nisar_rows = fetch_timeseries(lat, lon, win_start.isoformat(),
                                              win_end.isoformat(),
                                              buffer_m=buffer_m, verbose=verbose)
            except NisarError as exc:
                note = f"NISAR 자료를 얻지 못했습니다: {exc}"

    if nisar_rows:
        ni = pd.DataFrame(nisar_rows)
        ni["date"] = pd.to_datetime(ni["date"])
        # 같은 날 여러 궤도 관측은 평균으로 통합
        ni = ni.groupby("date", as_index=False).agg({"hh_db": "mean", "hv_db": "mean"})
        ni["hh_hv"] = ni["hh_db"] - ni["hv_db"]

    periods = _drain_periods(s1, threshold)
    longest = max((p.days for p in periods), default=0)
    verdict = "이행" if longest >= min_days else "미이행"

    setup_korean_font()
    fig, ax = plt.subplots(figsize=(12.5, 6.8))

    # --- 배경: 담수 전 구간에 깔고 낙수 구간만 덮는다 -----------------------
    xs = list(s1["date"])
    ax.axvspan(xs[0], xs[-1], color=C_POND, alpha=0.10,
               label="담수 구간 (S1 추정)")
    for k, p in enumerate(periods):
        ax.axvspan(datetime.combine(p.start, datetime.min.time()),
                   datetime.combine(p.end, datetime.min.time()),
                   color=C_DRAIN, alpha=0.30,
                   label="낙수 구간 (S1 추정)" if k == 0 else None)

    # --- 계열 1: Sentinel-1 C-band VH --------------------------------------
    ax.plot(s1["date"], s1["vh_db"], color=C_S1, lw=1.0, ls="--", alpha=.45,
            marker="o", ms=3, zorder=3)
    ax.plot(s1["date"], s1["vh_db_smooth"], color=C_S1, lw=2.0, marker="o",
            ms=6, mec="white", mew=0.8, zorder=4,
            label="S1 VH (C-band, 판정 주체)")
    ax.axhline(threshold, color=C_THRESH, lw=1.5, ls=":", zorder=2,
               label=f"S1 담수 임계값 {threshold:.0f} dB")

    # --- 계열 2: NISAR L-band ----------------------------------------------
    if ni is not None and not ni.empty:
        ax.plot(ni["date"], ni["hv_db"], color=C_NISAR_HV, lw=1.4, ls="--",
                marker="^", ms=5, zorder=4, label="NISAR HV (L-band, 보조)")
        ax.plot(ni["date"], ni["hh_db"], color=C_NISAR, lw=2.4, marker="s",
                ms=7, mec="white", mew=0.8, zorder=5,
                label="NISAR HH (L-band)")

        epoch = datetime.combine(NISAR_EPOCH, datetime.min.time())
        if xs[0] < epoch < xs[-1]:
            ax.axvline(epoch, color=C_MUTED, lw=1.2, ls="-.", alpha=.7, zorder=2)
            ax.annotate("NISAR 관측 시작", xy=(epoch, ax.get_ylim()[1]),
                        xytext=(6, -12), textcoords="offset points",
                        fontsize=9, color=C_MUTED, va="top")

    # --- 직접 라벨: 범례에만 의존하지 않게 계열 끝에 이름을 붙인다 ---------
    def label_end(x, y, text, color):
        ax.annotate(text, xy=(x, y), xytext=(8, 0), textcoords="offset points",
                    fontsize=10.5, color=color, va="center")

    label_end(s1["date"].iloc[-1], s1["vh_db_smooth"].iloc[-1], "S1 VH", C_S1)
    if ni is not None and not ni.empty:
        label_end(ni["date"].iloc[-1], ni["hh_db"].iloc[-1], "NISAR HH", C_NISAR)

    # --- 해석 문구 ---------------------------------------------------------
    if ni is not None and not ni.empty:
        caption = _interpretation(s1, ni, threshold)
    else:
        caption = note or "NISAR 자료 없음 — Sentinel-1 단독 판정입니다."

    ax.set_xlabel("날짜")
    ax.set_ylabel("후방산란 (dB)")
    ax.set_title(
        f"논물관리 이행 검증 — C-band · L-band 교차 검증\n"
        f"위치 {lat:.4f}, {lon:.4f}  |  S1 판정: {verdict} "
        f"(최장 연속 낙수 {longest}일 / 기준 {min_days}일)",
        fontsize=13, pad=12,
    )
    ax.grid(alpha=.22, lw=.5)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.legend(loc="upper left", fontsize=9, framealpha=.92, ncol=2)

    # 오른쪽 라벨이 잘리지 않게 여백을 준다
    ax.margins(x=.08)

    fig.text(0.012, 0.015, caption, fontsize=9.5, color=C_INK, va="bottom",
             wrap=True)
    fig.autofmt_xdate()
    fig.tight_layout(rect=(0, 0.075, 1, 1))
    fig.savefig(out_png, dpi=150)
    plt.close(fig)

    return {
        "png": Path(out_png).name,
        "n_nisar": 0 if ni is None else len(ni),
        "note": note,
        "verdict_s1": verdict,
        "longest": longest,
    }


def _interpretation(s1, ni, threshold: float) -> str:
    """두 센서가 같은 상태를 가리키는지 한 문장으로 정리한다.

    두 센서의 물리가 반대라는 점이 핵심이다.
      S1 VH 하락  = 담수 쪽    |  S1 VH 상승  = 낙수 쪽(또는 캐노피 성장)
      NISAR HH 상승 = 담수 쪽  |  NISAR HH 하락 = 낙수 쪽
    따라서 '둘 다 상승' 은 서로 반대를 가리키는 어긋남이고,
    'S1 하락 + HH 상승' 은 둘 다 담수를 가리키는 일치다.
    """
    MARGIN = 1.0  # 이보다 작은 변화는 궤도 혼재 노이즈와 구분되지 않는다
    s1_trend = s1["vh_db_smooth"].iloc[-1] - s1["vh_db_smooth"].iloc[0]
    hh_trend = ni["hh_db"].iloc[-1] - ni["hh_db"].iloc[0]

    head = (f"S1 VH {s1_trend:+.1f} dB,  NISAR HH {hh_trend:+.1f} dB "
            f"(기간 전체 변화).  ")

    s1_dir = "낙수" if s1_trend > MARGIN else "담수" if s1_trend < -MARGIN else None
    ni_dir = "담수" if hh_trend > MARGIN else "낙수" if hh_trend < -MARGIN else None

    if s1_dir is None or ni_dir is None:
        return head + (
            "한쪽 변화가 노이즈 수준이라 교차 검증으로 결론을 내리기 어렵습니다. "
            "관측 수를 늘리거나 기간을 넓혀 확인하세요.")

    if s1_dir == "낙수" and ni_dir == "담수":
        return head + (
            "어긋납니다. L-band 이중반사가 강해졌다는 것은 수면이 유지되었다는 뜻이므로, "
            "S1 의 상승은 낙수가 아니라 벼 캐노피 성장 때문일 가능성이 큽니다 "
            "— C-band 단독 판정이 과대평가된 사례입니다.")

    if s1_dir == ni_dir == "낙수":
        return head + (
            "두 센서가 일치합니다. C-band 상승과 L-band 이중반사 약화가 함께 나타나 "
            "실제로 물이 빠진 것으로 보입니다.")

    return head + (
        "두 센서가 일치합니다. C-band 하락과 L-band 이중반사 강화가 함께 나타나 "
        "수면이 유지된 것으로 보입니다.")


# 이전 이름으로 부르던 코드를 위해 남겨 둔다.
def render_compare_png(lat, lon, s1_csv, out_png, threshold=-20.0, verbose=False):
    return render_combined_png(lat, lon, s1_csv, out_png, threshold=threshold,
                               verbose=verbose)


def main() -> int:
    ap = argparse.ArgumentParser(description="S1 vs NISAR 교차 검증 (한 그래프)")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--start", default="2026-06-17")
    ap.add_argument("--end", default="2026-07-23")
    ap.add_argument("--threshold", type=float, default=-20.0,
                    help="S1 담수 임계값 (dB)")
    ap.add_argument("--min-days", type=int, default=14)
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from nisar_source import NisarError

    stem = f"{args.lat:.4f}_{args.lon:.4f}"
    s1_path = (OUTDIR / f"paddy_{stem}_{args.start.replace('-','')}"
                        f"_{args.end.replace('-','')}.csv")
    if not s1_path.is_file():
        print(f"[오류] S1 결과가 없습니다: {s1_path.name}\n"
              f"  먼저 실행하세요:\n"
              f"  python paddy_check.py --lat {args.lat} --lon {args.lon} "
              f"--start {args.start} --end {args.end} --csv", file=sys.stderr)
        return 1

    png = OUTDIR / f"combined_{stem}_{args.start.replace('-','')}.png"
    try:
        res = render_combined_png(args.lat, args.lon, s1_path, png,
                                  threshold=args.threshold,
                                  min_days=args.min_days, verbose=True)
    except NisarError as exc:
        print(f"\n[오류] {exc}", file=sys.stderr)
        return 1

    print("\n" + "=" * 70)
    print("  교차 검증 그래프")
    print("=" * 70)
    print(f"  S1 판정   : {res['verdict_s1']} (최장 연속 낙수 {res['longest']}일)")
    print(f"  NISAR 관측: {res['n_nisar']}건")
    if res["note"]:
        print(f"  참고      : {res['note']}")
    print(f"\n  저장: {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
