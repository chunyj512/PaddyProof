#!/usr/bin/env python3
"""MRV 콘솔 검증: 해시체인, 판정 함수의 MVP 일치, 콘솔 API (네트워크 불필요)."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "web"))

import pandas as pd
from fastapi.testclient import TestClient

import console_data as cd
import paddy_check as pc
from hashchain import GENESIS, build_chain, sign, verify_chain

KEY = b"k" * 32


def readings(n):
    return [{"device_id": "T", "seq": i, "ts": f"2026-06-01T{i % 24:02d}:00:00Z",
             "level_mm": 40 - i, "temp_c": 25.0, "batt_mv": 4000} for i in range(n)]


def test_chain_ok():
    recs = build_chain(KEY, readings(50))
    res = verify_chain(KEY, recs)
    assert res["summary"]["state"] == "OK" and res["summary"]["OK"] == 50
    assert recs[0]["prev_hash"] == GENESIS
    print("  OK  chain: clean log verifies")


def test_chain_tamper():
    recs = build_chain(KEY, readings(50))
    recs[20]["level_mm"] = -160
    s = verify_chain(KEY, recs)["summary"]
    assert s["state"] == "BROKEN" and s["first_break_seq"] == 20 and s["after_break"] == 29
    # Re-signing with the wrong key does not help.
    recs[20]["hash"] = sign(b"x" * 32, recs[20], recs[20]["prev_hash"])
    assert verify_chain(KEY, recs)["summary"]["state"] == "BROKEN"
    print("  OK  chain: an edited record is caught")


def test_chain_gap_and_conflict():
    recs = build_chain(KEY, readings(50))
    gap = recs[:10] + recs[15:]
    s = verify_chain(KEY, gap)["summary"]
    assert s["state"] == "GAP" and s["missing"] == 5 and s["BROKEN"] == 0
    dup = {k: recs[30][k] for k in ("device_id", "seq", "ts", "level_mm", "temp_c", "batt_mv")}
    dup["level_mm"] += 5
    dup["prev_hash"] = recs[30]["prev_hash"]
    dup["hash"] = sign(KEY, dup, dup["prev_hash"])
    s = verify_chain(KEY, recs + [dup])["summary"]
    assert s["state"] == "SEQ_CONFLICT" and s["SEQ_CONFLICT"] == 1
    assert verify_chain(KEY, recs + [recs[30]])["summary"]["state"] == "OK"   # exact resend is fine
    print("  OK  chain: gap and reused seq are told apart")


def test_judge_matches_mvp():
    """console_data must judge a series exactly as paddy_check does."""
    con = cd.get_console()
    n = 0
    for pid, series in con.series.items():
        if not pid.startswith("KR-"):
            continue
        df = pd.DataFrame({"date": pd.to_datetime([s["date"] for s in series]),
                           "vh_db": [s["vh_db"] for s in series]})
        df = pc.classify(pc.smooth(df, pc.DEFAULT_SMOOTH_WINDOW), pc.DEFAULT_THRESHOLD_DB)
        longest = max((p.days for p in pc.find_drain_periods(df)), default=0)
        assert longest == con.plots[pid]["longest_drain_days"], pid
        want = "compliant" if longest >= pc.DEFAULT_MIN_DRAIN_DAYS else "non_compliant"
        assert want == con.plots[pid]["verdict"], pid
        n += 1
    assert n == 80
    print(f"  OK  judgement: {n} measured plots match paddy_check")


def test_awd_rule():
    d0 = date(2026, 5, 1)
    dates = [d0 + timedelta(days=6 * i) for i in range(11)]
    # Smoothing is part of the rule, so drained blocks are two passes wide.
    two = [-24, -24, -14, -14, -24, -24, -14, -14, -24, -24, -24]
    one = [-24, -24, -14, -14, -24, -24, -24, -24, -24, -24, -24]
    still_open = two[:8]                       # second drainage has no reflood after it
    assert cd.judge(dates, two, "awd2")["cycles"] == 2 and cd.judge(dates, two, "awd2")["compliant"]
    assert cd.judge(dates, one, "awd2")["cycles"] == 1 and not cd.judge(dates, one, "awd2")["compliant"]
    j = cd.judge(dates[:8], still_open, "awd2")
    assert j["cycles"] == 1 and j["open_run"] and not j["compliant"]
    assert not cd.judge(dates, [-24] * 11, "awd2")["compliant"]
    print("  OK  judgement: AWD cycle rule")


def test_deterministic():
    a, b = cd.Console(), cd.Console()
    assert {k: v["evidence_sha256"] for k, v in a.plots.items()} == \
           {k: v["evidence_sha256"] for k, v in b.plots.items()}
    assert all(a.chain(k)["summary"]["head"] == b.chain(k)["summary"]["head"] for k in a.anchors)
    print("  OK  data: two builds are identical")


def test_api():
    import app
    c = TestClient(app.app)
    c.post("/api/console/demo/reset")
    o = c.get("/api/console/overview").json()
    t = o["totals"]
    assert t["plots"] == 1080 and sum(t["tiers"].values()) == 1080
    assert {p["data"] for p in o["projects"]} == {"measured", "demo"}
    kr = c.get("/api/console/projects/KR-IKSAN").json()
    assert kr["project"]["n_anchors"] == 0 and all(p["data"] == "measured" for p in kr["plots"])
    assert c.get("/api/console/plots/KR-IK-001").json()["nisar"]["rows"], "seed NISAR missing"

    # tamper -> chain broken -> plot goes to review -> reset restores
    a = c.get("/api/console/anchors/PP-VN-0002").json()["anchor"]
    assert a["chain"]["state"] == "OK"
    r = c.post("/api/console/anchors/PP-VN-0002/tamper-demo").json()
    assert r["chain"]["state"] == "BROKEN" and r["chain"]["first_break_seq"] == r["seq"]
    p = c.get(f"/api/console/plots/{a['plot_id']}").json()["plot"]
    assert p["tier"] == "review" and "chain_broken" in p["reasons"]
    assert c.post("/api/console/anchors/PP-VN-0002/tamper-demo").status_code == 409

    # review decision
    rv = c.get("/api/console/review").json()
    pid = next(i["id"] for i in rv["items"] if i["review"]["status"] == "open")
    out = c.post(f"/api/console/review/{pid}", json={"action": "resolve", "outcome": "non_compliant"}).json()
    assert out["plot"]["tier"] == "confirmed" and out["plot"]["verdict"] == "non_compliant"
    assert c.post(f"/api/console/review/{pid}", json={"action": "x"}).status_code == 400

    # reports and audit chain
    assert "draft" in c.get("/api/console/reports/VN-ANGIANG?format=md").text.lower()
    assert c.get("/api/console/reports/KR-IKSAN?format=csv").text.count("\n") == 81
    ev = list(reversed(c.get("/api/console/audit").json()["events"]))
    assert all(b["prev"] == a["hash"] for a, b in zip(ev, ev[1:]))
    assert c.get("/api/console/projects/NOPE").status_code == 404

    c.post("/api/console/demo/reset")
    assert c.get("/api/console/anchors/PP-VN-0002").json()["anchor"]["chain"]["state"] == "OK"
    assert c.get("/").status_code == 200 and c.get("/static/js/console.js").status_code == 200
    print("  OK  api: overview, tamper, review, reports, audit, reset")


if __name__ == "__main__":
    for f in (test_chain_ok, test_chain_tamper, test_chain_gap_and_conflict, test_judge_matches_mvp,
              test_awd_rule, test_deterministic, test_api):
        f()
    print("\n모든 콘솔 테스트 통과")
