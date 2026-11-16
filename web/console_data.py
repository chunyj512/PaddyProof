"""Data behind the MRV console.

Two kinds of data live here and every object says which one it is (``data``):

* ``measured`` - Korea / Iksan block. Real Sentinel-1 RTC VH backscatter for 80
  sample points (seed/s1_points_iksan_2026.csv, Microsoft Planetary Computer)
  and real NISAR L-band observations (seed/nisar_cache.json). The points are a
  grid around the MVP's default coordinate; they have not been checked against
  a paddy mask. There are no anchors here because no hardware is deployed yet.
* ``demo`` - Vietnam and India. Plots, satellite series and anchor logs are
  generated from a fixed seed. Pass counts and reduction factors come from the
  team's virtual-paddy simulation (paddyproof_vv, outputs/gpu/SUMMARY_결선.md
  section 9 and outputs/asia/opt/opt2_summary.json). ``weak_contrast`` is the
  share of plots generated with radar values close to the threshold; the
  review share you see is whatever the tier rule then yields (about 40 % for
  Vietnam, about 25 % for India - the simulation predicts 39.7 % and 49.0 %
  under its own, stricter review rule). None of this is a field result.

Nothing here changes how the MVP judges a field: ``smooth``/``classify``/
``drain_runs`` mirror paddy_check.py (test_console.py checks they agree).
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from hashchain import FIELDS, build_chain, sign, verify_chain

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed"

SEED = 20261116
DEMO_NOW = datetime(2026, 10, 2, 9, 0)          # fixed "data as of" clock
THRESHOLD_DB = -20.0                            # same default as paddy_check.py
MIN_DRAIN_DAYS = 14
SMOOTH_WINDOW = 3
NISAR_MARGIN_DB = 1.0                           # same as nisar_evidence.py
NISAR_MIN_OBS = 2

PROJECTS = [
    {
        "id": "KR-IKSAN", "country": "KR", "country_name": "South Korea",
        "name": "Iksan Pilot Block", "region": "Jeonbuk",
        "lat": 35.980, "lon": 126.925, "status": "active", "data": "measured",
        "rule": "msd14", "season": "2026 main season",
        "window": ("2026-05-01", "2026-07-20"),
        "programme": "Low-Carbon Agriculture Programme (mid-season drainage)",
        "ef": 3.78, "ef_source": "RDA 2026 field trial, 315 to 175 kg CH4/ha, GWP100 27",
        "plot_area": 0.3, "anchors": 0,
    },
    {
        "id": "VN-ANGIANG", "country": "VN", "country_name": "Vietnam",
        "name": "An Giang Summer-Autumn", "region": "Mekong Delta",
        "lat": 10.520, "lon": 105.130, "status": "active", "data": "demo",
        "rule": "awd2", "season": "2026 summer-autumn",
        "window": ("2026-05-10", "2026-08-20"),
        "programme": "VM0051-aligned AWD project (draft design)",
        "ef": 0.974, "ef_source": "simulation assumption (opt2_summary.json, gpu_vn_sa)",
        "plot_area": 0.3, "anchors": 12, "plots": 600, "grid": (30, 20),
        "passes": 19, "weak_contrast": 0.40,
    },
    {
        "id": "IN-TELANGANA", "country": "IN", "country_name": "India",
        "name": "Telangana Kharif", "region": "Telangana",
        "lat": 17.950, "lon": 79.600, "status": "active", "data": "demo",
        "rule": "awd2", "season": "2026 kharif",
        "window": ("2026-07-01", "2026-09-30"),
        "programme": "VM0051-aligned AWD project (draft design)",
        "ef": 0.745, "ef_source": "simulation assumption (opt2_summary.json, gpu_in_kharif)",
        "plot_area": 0.3, "anchors": 8, "plots": 400, "grid": (25, 16),
        "passes": 9, "weak_contrast": 0.490,
    },
]

PIPELINE = [
    {"id": "BD", "country": "BD", "country_name": "Bangladesh", "lat": 25.75, "lon": 89.25},
    {"id": "KH", "country": "KH", "country_name": "Cambodia", "lat": 13.10, "lon": 103.20},
    {"id": "PH", "country": "PH", "country_name": "Philippines", "lat": 15.58, "lon": 120.92},
    {"id": "ID", "country": "ID", "country_name": "Indonesia", "lat": -6.50, "lon": 107.60},
]

# Virtual-paddy simulation, Korea profile, real Sentinel-1 acquisition schedule.
# Shown as-is, including the cells where PaddyProof is not the best method.
BENCHMARKS = {
    "source": "Virtual-paddy simulation (20,000 plots x 11 years). Not field results.",
    "rows": [
        {"key": "hours", "visits": 9280, "photo": 485, "pp": 528},
        {"key": "evader", "visits": 76.0, "photo": 25.6, "pp": 68.2},
        {"key": "overcredit", "visits": 16.4, "photo": 27.7, "pp": 43.0},
        {"key": "review", "visits": None, "photo": None, "pp": 48.5},
    ],
}


# --------------------------------------------------------------------------
# Satellite judgement (mirrors paddy_check.py)
# --------------------------------------------------------------------------

def smooth(values: list[float], window: int = SMOOTH_WINDOW) -> list[float]:
    """Centred moving average with min_periods=1, like pandas rolling(center=True)."""
    if window <= 1:
        return list(values)
    n, out = len(values), []
    left = window // 2
    right = window - left - 1
    for i in range(n):
        seg = values[max(0, i - left):min(n, i + right + 1)]
        out.append(sum(seg) / len(seg))
    return out


def classify(smoothed: list[float], threshold: float = THRESHOLD_DB) -> list[bool]:
    """True = drained (VH at or above the threshold)."""
    return [v >= threshold for v in smoothed]


def drain_runs(dates: list[date], drained: list[bool]) -> list[tuple[int, int]]:
    """Index ranges (first, last) of consecutive drained observations."""
    runs, i, n = [], 0, len(drained)
    while i < n:
        if not drained[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and drained[j + 1]:
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


def judge(dates: list[date], vh: list[float], rule: str,
          threshold: float = THRESHOLD_DB) -> dict:
    """Apply a project's compliance rule to one satellite series."""
    sm = smooth(vh)
    dr = classify(sm, threshold)
    runs = drain_runs(dates, dr)
    longest = max(((dates[b] - dates[a]).days for a, b in runs), default=0)
    # A drain event counts for AWD only when flooding is seen before and after it.
    cycles = sum(1 for a, b in runs if a > 0 and b < len(dr) - 1)
    if rule == "msd14":
        ok = longest >= MIN_DRAIN_DAYS
    else:
        ok = cycles >= 2
    open_run = bool(runs) and runs[-1][1] == len(dr) - 1
    return {"smooth": sm, "drained": dr, "runs": runs, "longest": longest,
            "cycles": cycles, "compliant": ok, "open_run": open_run}


def stable(dates: list[date], vh: list[float], rule: str) -> bool:
    """Does the verdict survive moving the threshold by +-0.5 and +-1 dB?"""
    seen = {judge(dates, vh, rule, THRESHOLD_DB + d)["compliant"]
            for d in (-1.0, -0.5, 0.0, 0.5, 1.0)}
    return len(seen) == 1


# --------------------------------------------------------------------------
# Korea: measured data
# --------------------------------------------------------------------------

def _load_kr_points() -> dict[int, dict]:
    pts: dict[int, dict] = {}
    with open(SEED_DIR / "s1_points_iksan_2026.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = pts.setdefault(int(row["point_id"]), {
                "lat": float(row["lat"]), "lon": float(row["lon"]), "obs": {}})
            # Two orbits can pass on one date; average them like the MVP does.
            p["obs"].setdefault(row["date"], []).append(float(row["vh_db"]))
    return pts


def _load_nisar(lat: float, lon: float) -> list[dict]:
    path = SEED_DIR / "nisar_cache.json"
    if not path.is_file():
        return []
    prefix = f"{lat:.5f},{lon:.5f},"
    by_date: dict[str, list[dict]] = {}
    for key, v in json.loads(path.read_text(encoding="utf-8")).items():
        if key.startswith(prefix) and v and v.get("hh_db") is not None:
            by_date.setdefault(v["date"], []).append(v)
    rows = []
    for d in sorted(by_date):
        g = by_date[d]
        hh = sum(x["hh_db"] for x in g) / len(g)
        hv = sum(x["hv_db"] for x in g) / len(g)
        rows.append({"date": d, "hh_db": round(hh, 2), "hv_db": round(hv, 2)})
    return rows


def nisar_cross_check(series: list[dict], nisar: list[dict]) -> dict:
    """Relative L-band check, same rule as nisar_evidence.cross_check."""
    if not nisar:
        return {"verdict": "no_data", "rows": []}
    rows = []
    for r in nisar:
        state = None
        for s in series:
            if s["date"] > r["date"]:
                break
            state = s["state"]
        rows.append({**r, "s1_state": state})
    dr = [r["hh_db"] for r in rows if r["s1_state"] == "drained"]
    po = [r["hh_db"] for r in rows if r["s1_state"] == "flooded"]
    out = {"rows": rows, "n_drained": len(dr), "n_ponded": len(po)}
    if len(dr) < NISAR_MIN_OBS or len(po) < NISAR_MIN_OBS:
        return {**out, "verdict": "unclear"}
    hd, hp = sum(dr) / len(dr), sum(po) / len(po)
    diff = hd - hp
    verdict = ("s1_overestimate" if diff > NISAR_MARGIN_DB
               else "agree" if diff < -NISAR_MARGIN_DB else "unclear")
    return {**out, "verdict": verdict, "hh_drained": round(hd, 2),
            "hh_ponded": round(hp, 2), "hh_diff": round(diff, 2)}


# --------------------------------------------------------------------------
# Demo projects
# --------------------------------------------------------------------------

def _demo_truth(rng: random.Random, start: date, end: date, kind: str) -> list[tuple[date, date]]:
    """Drained intervals of a demo plot (surface water absent)."""
    span = (end - start).days
    if kind == "none":
        return []
    n = {"single": 1, "awd": rng.choice([2, 2, 3])}[kind]
    out, t = [], start + timedelta(days=rng.randint(16, 24))
    for _ in range(n):
        length = rng.randint(8, 12)
        a, b = t, t + timedelta(days=length)
        if (b - start).days > span - 6:
            break
        out.append((a, b))
        t = b + timedelta(days=rng.randint(8, 13))
    return out


def _is_drained(truth: list[tuple[date, date]], d: date) -> bool:
    return any(a <= d < b for a, b in truth)


def _demo_series(rng: random.Random, proj: dict, truth, borderline: bool,
                 sparse: bool) -> tuple[list[date], list[float]]:
    start, end = (date.fromisoformat(x) for x in proj["window"])
    span = (end - start).days
    n = proj["passes"]
    step = span / n
    dates = [start + timedelta(days=int(i * step + step / 2)) for i in range(n)]
    if sparse:
        dates = sorted(rng.sample(dates, 2))
    vh = []
    for d in dates:
        dry = _is_drained(truth, d)
        if borderline:
            base = -19.3 if dry else -20.7
            v = base + rng.gauss(0, 0.35)
        else:
            base = -15.8 if dry else -23.6
            v = base + rng.gauss(0, 0.9)
        vh.append(round(v, 2))
    return dates, vh


def _anchor_readings(rng: random.Random, device_id: str, truth, start: date,
                     until: datetime, batt_end: float = 4060.0) -> list[dict]:
    """Hourly water level in an observation pipe (mm, 0 = soil surface)."""
    out, level, seq = [], 45.0, 0
    target = rng.uniform(30, 55)
    t = datetime(start.year, start.month, start.day)
    batt = 4160.0
    hours = max(1.0, (until - t).total_seconds() / 3600)
    batt_rate = (batt - batt_end) / hours
    rain_left = 0
    while t <= until:
        dry = _is_drained(truth, t.date())
        if rain_left == 0 and rng.random() < 0.012:
            rain_left = rng.randint(2, 6)
        if dry:
            # Gate open: standing water leaves within hours, then the water table
            # falls about 2.2 cm a day.
            level = level - 30 if level > 0 else max(level - 22 / 24, -230.0)
            if rain_left:
                level += rng.uniform(1.0, 3.0)
        else:
            level += (target - level) * 0.35                # irrigation refills in hours
            if rain_left:
                level += rng.uniform(0.5, 2.0)
            level -= 0.15
        rain_left = max(0, rain_left - 1)
        temp = 27 + 5 * math.sin((t.hour - 9) / 24 * 2 * math.pi) + rng.gauss(0, 0.4)
        batt -= batt_rate
        out.append({
            "device_id": device_id, "seq": seq,
            "ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level_mm": int(round(level + rng.gauss(0, 2))),
            "temp_c": round(temp, 1), "batt_mv": int(batt),
        })
        seq += 1
        t += timedelta(hours=1)
    return out


def sensor_cycles(records: list[dict]) -> int:
    """AWD cycles in an anchor log: water falls to -15 cm or lower, then returns."""
    cycles, low = 0, False
    for r in records:
        if r["level_mm"] <= -150:
            low = True
        elif low and r["level_mm"] >= 10:
            cycles += 1
            low = False
    return cycles


# --------------------------------------------------------------------------
# Console state
# --------------------------------------------------------------------------

class Console:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.build()

    # ---- build ----------------------------------------------------------
    def build(self) -> None:
        self.plots: dict[str, dict] = {}
        self.series: dict[str, list[dict]] = {}
        self.nisar: dict[str, dict] = {}
        self.anchors: dict[str, dict] = {}
        self.records: dict[str, list[dict]] = {}
        self.keys: dict[str, bytes] = {}
        self.review: dict[str, dict] = {}
        self.audit: list[dict] = []
        self._chains: dict[str, dict] = {}
        self._tick = 0
        for proj in PROJECTS:
            if proj["data"] == "measured":
                self._build_kr(proj)
            else:
                self._build_demo(proj)
        self._seed_review()
        self._seed_audit()

    def _finish_plot(self, proj: dict, pid: str, name: str, lat: float, lon: float,
                     dates: list[date], vh: list[float], extra: dict) -> dict:
        j = judge(dates, vh, proj["rule"])
        series = [{"date": d.isoformat(), "vh_db": v, "vh_smooth": round(s, 2),
                   "state": "drained" if dr else "flooded"}
                  for d, v, s, dr in zip(dates, vh, j["smooth"], j["drained"])]
        reasons = []
        if len(dates) <= 2:
            reasons.append("few_observations")
        elif not stable(dates, vh, proj["rule"]):
            reasons.append("threshold_margin")
        margin = min((abs(s - THRESHOLD_DB) for s in j["smooth"]), default=0.0)
        digest = hashlib.sha256(json.dumps(
            {"plot": pid, "rule": proj["rule"], "threshold": THRESHOLD_DB,
             "series": [(s["date"], s["vh_db"]) for s in series]},
            sort_keys=True).encode()).hexdigest()
        plot = {
            "id": pid, "project_id": proj["id"], "name": name,
            "lat": round(lat, 5), "lon": round(lon, 5), "area_ha": proj["plot_area"],
            "data": proj["data"], "n_obs": len(dates),
            "longest_drain_days": j["longest"], "drain_cycles": j["cycles"],
            "verdict": "compliant" if j["compliant"] else "non_compliant",
            "open_run": j["open_run"], "margin_db": round(margin, 2),
            "reasons": reasons, "nisar": None, "anchor_id": None,
            "anchor_agrees": None, "field_verified": False,
            "last_obs": series[-1]["date"] if series else None,
            "evidence_sha256": digest, **extra,
        }
        self.plots[pid] = plot
        self.series[pid] = series
        return plot

    def _build_kr(self, proj: dict) -> None:
        w0, w1 = proj["window"]
        pts = _load_kr_points()
        for i, (point_id, p) in enumerate(sorted(pts.items()), 1):
            days = sorted(d for d in p["obs"] if w0 <= d <= w1)
            dates = [date.fromisoformat(d) for d in days]
            vh = [round(sum(p["obs"][d]) / len(p["obs"][d]), 2) for d in days]
            pid = f"KR-IK-{i:03d}"
            plot = self._finish_plot(proj, pid, f"IK-{i:03d}", p["lat"], p["lon"],
                                     dates, vh, {"group": "Iksan block"})
            # NISAR exists only where the MVP's seed cache has it (plot 1 = 익산A).
            ni = _load_nisar(p["lat"], p["lon"])
            if ni:
                # State lookup needs S1 beyond the judgement window (NISAR starts 6/17).
                all_days = sorted(p["obs"])
                all_vh = [sum(p["obs"][d]) / len(p["obs"][d]) for d in all_days]
                st = classify(smooth(all_vh))
                full = [{"date": d, "state": "drained" if x else "flooded"}
                        for d, x in zip(all_days, st)]
                res = nisar_cross_check(full, ni)
                self.nisar[pid] = res
                plot["nisar"] = res["verdict"]
                if res["verdict"] == "s1_overestimate":
                    plot["reasons"].append("nisar_disagree")

    def _build_demo(self, proj: dict) -> None:
        rng = random.Random(f"{SEED}-{proj['id']}")
        start, end = (date.fromisoformat(x) for x in proj["window"])
        cols, rows = proj["grid"]
        n = proj["plots"]
        every = n // proj["anchors"]
        prefix = {"VN": "AG", "IN": "TG"}[proj["country"]]
        for i in range(n):
            r, c = divmod(i, cols)
            lat = proj["lat"] + (r - rows / 2) * 0.00055 + rng.uniform(-1, 1) * 0.00008
            lon = proj["lon"] + (c - cols / 2) * 0.00060 + rng.uniform(-1, 1) * 0.00008
            kind = rng.choices(["awd", "single", "none"], [0.58, 0.24, 0.18])[0]
            truth = _demo_truth(rng, start, end, kind)
            has_anchor = i % every == every // 2
            u = rng.random()
            sparse = (not has_anchor) and u < 0.03
            borderline = (not has_anchor) and (not sparse) and u < proj["weak_contrast"]
            dates, vh = _demo_series(rng, proj, truth, borderline, sparse)
            pid = f"{proj['country']}-{prefix}-{i + 1:04d}"
            plot = self._finish_plot(
                proj, pid, f"{prefix}-{i + 1:04d}", lat, lon, dates, vh,
                {"group": f"Cooperative {chr(65 + (i * 7 // n))}"})
            if has_anchor:
                self._add_anchor(rng, proj, plot, truth, start)

    def _add_anchor(self, rng, proj, plot, truth, start: date) -> None:
        k = sum(1 for a in self.anchors.values() if a["project_id"] == proj["id"]) + 1
        aid = f"PP-{proj['country']}-{k:04d}"
        # Demo key. A real device would hold a provisioned secret, not a derivable one.
        key = hashlib.sha256(f"demo-key-{aid}".encode()).digest()
        until = DEMO_NOW
        note = None
        if aid == "PP-IN-0003":                       # battery ran down
            until = DEMO_NOW - timedelta(days=3, hours=5)
            note = "low_battery"
        recs = build_chain(key, _anchor_readings(
            rng, aid, truth, start, until,
            3340.0 if note else rng.uniform(3900.0, 4110.0)))
        if aid == "PP-VN-0005":                       # 9 h outage, backfill not yet received
            cut = len(recs) - 60
            recs = recs[:cut] + recs[cut + 9:]
            note = "backfill_pending"
        if aid == "PP-VN-0009":                       # a reused seq number arrives
            dup = {f: recs[-30][f] for f in FIELDS}
            dup["level_mm"] += 40
            dup["prev_hash"] = recs[-30]["prev_hash"]
            dup["hash"] = sign(key, dup, dup["prev_hash"])
            recs.append(dup)
            note = "seq_conflict"
        self.keys[aid] = key
        self.records[aid] = recs
        cycles = sensor_cycles(recs)
        sensor_ok = cycles >= 2
        plot["anchor_id"] = aid
        plot["anchor_agrees"] = sensor_ok == (plot["verdict"] == "compliant")
        if not plot["anchor_agrees"]:
            plot["reasons"].append("anchor_disagree")
        self.anchors[aid] = {
            "id": aid, "project_id": proj["id"], "plot_id": plot["id"],
            "plot_name": plot["name"], "lat": plot["lat"], "lon": plot["lon"],
            "firmware": "v1.2", "interval_min": 60, "comms": "LTE-M",
            "sensor_cycles": cycles,
            "sensor_verdict": "compliant" if sensor_ok else "non_compliant",
            "note": note, "tampered": False, "data": "demo",
        }

    # ---- derived --------------------------------------------------------
    def tier(self, plot: dict) -> str:
        """Display tier. Review > provisional > confirmed.

        review       few observations, verdict flips within +-1 dB, NISAR or an
                     anchor contradicts the satellite verdict, or the anchor's
                     log fails chain verification
        provisional  verdict is stable but the last drainage run is still open
                     at the latest observation, so it can still change
        confirmed    stable, closed, nothing contradicts it - or a field
                     inspection settled it
        """
        if plot["field_verified"]:
            return "confirmed"
        if plot["reasons"] or self._anchor_chain_bad(plot):
            return "review"
        if plot["open_run"]:
            return "provisional"
        return "confirmed"

    def _anchor_chain_bad(self, plot: dict) -> bool:
        aid = plot.get("anchor_id")
        return bool(aid) and self.chain(aid)["summary"]["state"] == "BROKEN"

    def reasons(self, plot: dict) -> list[str]:
        r = list(plot["reasons"])
        if self._anchor_chain_bad(plot):
            r.append("chain_broken")
        return r

    def chain(self, aid: str) -> dict:
        if aid not in self._chains:
            self._chains[aid] = verify_chain(self.keys[aid], self.records[aid])
        return self._chains[aid]

    def plot_view(self, plot: dict) -> dict:
        return {**plot, "tier": self.tier(plot), "reasons": self.reasons(plot)}

    def anchor_view(self, a: dict) -> dict:
        recs = self.records[a["id"]]
        last = recs[-1] if recs else None
        last_ts = datetime.strptime(max(r["ts"] for r in recs), "%Y-%m-%dT%H:%M:%SZ")
        age_h = (DEMO_NOW - last_ts).total_seconds() / 3600
        summ = self.chain(a["id"])["summary"]
        batt = last["batt_mv"] if last else 0
        return {
            **a, "status": "online" if age_h <= 2 else "offline",
            "last_seen": last_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "age_hours": round(age_h, 1), "batt_mv": batt,
            "batt_pct": max(0, min(100, round((batt - 3300) / (4200 - 3300) * 100))),
            "level_mm": last["level_mm"] if last else None, "chain": summ,
        }

    def project_view(self, proj: dict) -> dict:
        plots = [self.plot_view(p) for p in self.plots.values()
                 if p["project_id"] == proj["id"]]
        tiers = {t: sum(1 for p in plots if p["tier"] == t)
                 for t in ("confirmed", "provisional", "review")}
        anchors = [self.anchor_view(a) for a in self.anchors.values()
                   if a["project_id"] == proj["id"]]

        def er(tier: str) -> float:
            return round(sum(p["area_ha"] for p in plots
                             if p["tier"] == tier and p["verdict"] == "compliant")
                         * proj["ef"], 1)

        return {
            **{k: v for k, v in proj.items() if k not in ("grid",)},
            "n_plots": len(plots),
            "area_ha": round(sum(p["area_ha"] for p in plots), 1),
            "tiers": tiers,
            "compliant": sum(1 for p in plots if p["verdict"] == "compliant"),
            "n_anchors": len(anchors),
            "anchors_online": sum(1 for a in anchors if a["status"] == "online"),
            "chain_alerts": sum(1 for a in anchors if a["chain"]["state"] != "OK"),
            "er_confirmed": er("confirmed"), "er_provisional": er("provisional"),
            "review_open": sum(1 for p in plots if p["tier"] == "review"
                               and self.review.get(p["id"], {}).get("status") != "resolved"),
            "mean_obs": round(sum(p["n_obs"] for p in plots) / max(1, len(plots)), 1),
        }

    # ---- review & audit -------------------------------------------------
    def _seed_review(self) -> None:
        rng = random.Random(f"{SEED}-review")
        ids = sorted(p["id"] for p in self.plots.values() if self.tier(p) == "review")
        for pid in rng.sample(ids, min(14, len(ids))):
            self.review[pid] = {"status": "assigned",
                                "assignee": rng.choice(["Field team A", "Field team B"]),
                                "outcome": None, "note": "", "updated": self._stamp(-rng.randint(2, 40))}

    def _stamp(self, hours: float = 0.0) -> str:
        return (DEMO_NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def log(self, actor: str, action: str, target: str, detail: str = "",
            hours: float | None = None) -> dict:
        if hours is None:
            self._tick += 1
            hours = self._tick / 60
        prev = self.audit[-1]["hash"] if self.audit else "0" * 64
        ev = {"ts": self._stamp(hours), "actor": actor, "action": action,
              "target": target, "detail": detail, "prev": prev}
        ev["hash"] = hashlib.sha256(json.dumps(ev, sort_keys=True).encode()).hexdigest()
        self.audit.append(ev)
        return ev

    def _seed_audit(self) -> None:
        self.log("system", "satellite_pass", "IN-TELANGANA", "Sentinel-1 pass processed, 400 plots", -70)
        self.log("system", "satellite_pass", "KR-IKSAN", "Sentinel-1 pass processed, 80 plots", -49)
        self.log("system", "anchor_offline", "PP-IN-0003", "No uplink for 24 h, last battery 3.34 V", -53)
        self.log("ops@paddyproof", "review_assigned", "IN-TELANGANA", "6 plots to Field team B", -40)
        self.log("system", "seq_conflict", "PP-VN-0009", "Reused seq quarantined, first arrival kept", -29)
        self.log("system", "chain_gap", "PP-VN-0005", "9 records missing, backfill expected", -26)
        self.log("ops@paddyproof", "report_exported", "VN-ANGIANG", "Draft MRV report, Markdown", -20)
        self.log("system", "satellite_pass", "VN-ANGIANG", "Sentinel-1 pass processed, 600 plots", -11)
        self.log("system", "chain_verified", "all anchors", "Hourly chain verification finished", -1)

    def reset(self) -> None:
        with self.lock:
            self.build()


_console: Console | None = None
_console_lock = threading.Lock()


def get_console() -> Console:
    global _console
    with _console_lock:
        if _console is None:
            _console = Console()
        return _console
