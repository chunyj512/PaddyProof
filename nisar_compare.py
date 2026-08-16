#!/usr/bin/env python3
"""
Phase 4 — S1 C-band vs NISAR L-band 교차 검증

핵심 질문: S1 이 "낙수"라고 본 구간에서 NISAR 는 이중반사(담수)를 보는가?

  - 그렇다  → S1 의 캐노피 과대판정을 NISAR 가 잡아냄 (서비스 가치 입증)
  - 아니다  → 실제 낙수. S1 판정 신뢰도 상승

두 센서의 물리가 반대라는 점에 주의한다.
  C-band VH : 담수면은 경면반사로 낮음. 벼가 자라면 캐노피 산란으로 올라감.
  L-band HH : 캐노피를 투과해 수면-줄기 이중반사를 봄. 담수 상태에서 오히려 높음.

    python nisar_compare.py --lat 35.9820 --lon 126.9250 --start 2026-06-17 --end 2026-07-23
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTDIR = ROOT / "output"


def render_compare_png(lat: float, lon: float, s1_csv: Path, out_png: Path,
                       threshold: float = -20.0, verbose: bool = False) -> "pd.DataFrame":
    """S1 CSV 와 NISAR 시계열로 2단 비교 그래프를 그린다. NISAR DataFrame 을 반환."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import pandas as pd

    sys.path.insert(0, str(ROOT))
    from paddy_check import setup_korean_font
    from nisar_source import fetch_timeseries

    s1 = pd.read_csv(s1_csv, encoding="utf-8-sig")
    s1["date"] = pd.to_datetime(s1["date"])
    start = s1["date"].min().date().isoformat()
    end = s1["date"].max().date().isoformat()

    rows = fetch_timeseries(lat, lon, start, end, verbose=verbose)
    ni = pd.DataFrame(rows)
    ni["date"] = pd.to_datetime(ni["date"])
    # 같은 날 여러 궤도 관측은 평균으로 통합
    ni = ni.groupby("date", as_index=False).agg({"hh_db": "mean", "hv_db": "mean"})
    ni["hh_hv"] = ni["hh_db"] - ni["hv_db"]

    _draw(s1, ni, lat, lon, threshold, out_png, plt, mdates, setup_korean_font)
    return ni


def _draw(s1, ni, lat, lon, threshold, png, plt, mdates, setup_korean_font):
    setup_korean_font()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9), sharex=True,
                                   gridspec_kw={"height_ratios": [1, 1]})

    # --- 위: S1 C-band ---
    ax1.plot(s1["date"], s1["vh_db"], color="#B0B0B0", ls="--", marker="o",
             ms=4, lw=1, label="VH 원자료")
    ax1.plot(s1["date"], s1["vh_db_smooth"], color="#1F3A5F", marker="o",
             ms=6, lw=2, label="VH 이동평균")
    ax1.axhline(threshold, color="#C62828", ls=":", lw=1.5,
                label=f"임계값 {threshold:.0f} dB")
    drained = s1[s1["state"] == "낙수"]
    if not drained.empty:
        ax1.scatter(drained["date"], drained["vh_db_smooth"], s=110, zorder=5,
                    facecolor="none", edgecolor="#E65100", linewidth=2.5,
                    label="S1 낙수 판정")
    ax1.set_ylabel("Sentinel-1 VH (dB)")
    ax1.set_title("C-band: 담수면은 낮고, 벼가 자라면 캐노피 산란으로 올라감\n"
                  "→ 생육 후기에 담수 상태여도 '낙수'로 보일 수 있음",
                  fontsize=11, pad=10)
    ax1.legend(fontsize=9, loc="best")
    ax1.grid(alpha=.25, lw=.5)

    # --- 아래: NISAR L-band ---
    ax2.plot(ni["date"], ni["hh_db"], color="#2E7D32", marker="s", ms=6, lw=2,
             label="NISAR HH")
    ax2.plot(ni["date"], ni["hv_db"], color="#81C784", marker="^", ms=5, lw=1.5,
             ls="--", label="NISAR HV")
    ax2.set_ylabel("NISAR HH · HV (dB)")
    ax2.set_xlabel("날짜")
    ax2.grid(alpha=.25, lw=.5)

    ax3 = ax2.twinx()
    ax3.plot(ni["date"], ni["hh_hv"], color="#6A1B9A", marker="D", ms=5, lw=2,
             alpha=.85, label="HH-HV (이중반사 지표)")
    ax3.set_ylabel("HH - HV (dB)", color="#6A1B9A")
    ax3.tick_params(axis="y", labelcolor="#6A1B9A")

    h2, l2 = ax2.get_legend_handles_labels()
    h3, l3 = ax3.get_legend_handles_labels()
    ax2.legend(h2 + h3, l2 + l3, fontsize=9, loc="best")
    ax2.set_title("L-band: 캐노피를 투과해 수면-줄기 이중반사를 봄\n"
                  "→ 담수 상태에서 HH 와 HH-HV 가 함께 상승",
                  fontsize=11, pad=10)

    for ax in (ax1, ax2):
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.suptitle(f"C-band vs L-band 교차 검증 — {lat:.4f}, {lon:.4f}",
                 fontsize=13, fontweight="bold")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(png, dpi=150)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description="S1 vs NISAR 교차 검증")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--start", default="2026-06-17")
    ap.add_argument("--end", default="2026-07-23")
    ap.add_argument("--threshold", type=float, default=-20.0,
                    help="S1 담수 임계값 (dB)")
    args = ap.parse_args()

    import pandas as pd

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

    png = OUTDIR / f"compare_{stem}_{args.start.replace('-','')}.png"
    try:
        ni = render_compare_png(args.lat, args.lon, s1_path, png,
                                args.threshold, verbose=True)
    except NisarError as exc:
        print(f"\n[오류] {exc}", file=sys.stderr)
        return 1

    s1 = pd.read_csv(s1_path, encoding="utf-8-sig")
    s1["date"] = pd.to_datetime(s1["date"])

    # --- 판정 --------------------------------------------------------------
    print("\n" + "=" * 70)
    print("  교차 검증 결과")
    print("=" * 70)

    hh_trend = ni["hh_db"].iloc[-1] - ni["hh_db"].iloc[0]
    ratio_trend = ni["hh_hv"].iloc[-1] - ni["hh_hv"].iloc[0]
    n_drained = int((s1["state"] == "낙수").sum())

    print(f"\n  S1  : 관측 {len(s1)}건 중 낙수 판정 {n_drained}건")
    print(f"        VH {s1['vh_db_smooth'].iloc[0]:+.1f} → "
          f"{s1['vh_db_smooth'].iloc[-1]:+.1f} dB "
          f"({s1['vh_db_smooth'].iloc[-1] - s1['vh_db_smooth'].iloc[0]:+.1f})")
    print(f"  NISAR: 관측 {len(ni)}건")
    print(f"        HH {ni['hh_db'].iloc[0]:+.1f} → {ni['hh_db'].iloc[-1]:+.1f} dB "
          f"({hh_trend:+.1f})")
    print(f"        HH-HV {ni['hh_hv'].iloc[0]:+.1f} → "
          f"{ni['hh_hv'].iloc[-1]:+.1f} dB ({ratio_trend:+.1f})")

    print("\n  해석:")
    if hh_trend > 2 and ratio_trend > 1:
        print("    NISAR HH 와 이중반사 지표가 함께 상승했습니다. 이는 벼가 자라는")
        print("    동안 수면이 유지되었다는 신호입니다 (수면-줄기 이중반사 강화).")
        if n_drained:
            print(f"\n    그런데 S1 은 같은 기간에 낙수를 {n_drained}건 판정했습니다.")
            print("    → S1 의 상승은 낙수가 아니라 캐노피 성장에 의한 것으로 보입니다.")
            print("       NISAR 가 C-band 과대판정을 잡아낸 사례입니다.")
        else:
            print("\n    S1 도 이 기간을 담수로 보아 두 센서가 일치합니다.")
    elif hh_trend < -2:
        print("    NISAR HH 가 하락했습니다. 실제 낙수 가능성이 있습니다.")
        print("    S1 판정과 방향이 일치하는지 확인하세요.")
    else:
        print("    뚜렷한 추세가 없습니다. 관측 수를 늘리거나 기간을 넓혀야 합니다.")

    print("\n  한계: NISAR 판정 임계값은 아직 없습니다. 위 해석은 절대값이 아니라")
    print("        추세에 근거하며, 담수→낙수 전환이 실제로 일어난 필지를 확보해야")
    print("        임계값을 정할 수 있습니다.")
    print(f"\n  저장: {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
