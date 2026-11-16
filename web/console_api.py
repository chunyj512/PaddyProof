"""HTTP API for the MRV console (/api/console/*).

Reads go through console_data.Console. The few writes (review decisions, the
tamper demo, reset) only touch in-memory state and are recorded in the audit log.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import PlainTextResponse

from console_data import (BENCHMARKS, DEMO_NOW, MIN_DRAIN_DAYS, PIPELINE, PROJECTS,
                          THRESHOLD_DB, get_console)

REASONS = ("few_observations", "threshold_margin", "nisar_disagree",
           "anchor_disagree", "chain_broken")


def make_router(require_auth) -> APIRouter:
    r = APIRouter(prefix="/api/console")

    def proj_or_404(pid: str) -> dict:
        for p in PROJECTS:
            if p["id"] == pid:
                return p
        raise HTTPException(404, "Unknown project")

    def as_of() -> str:
        return DEMO_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    @r.get("/overview")
    def overview(session: str | None = Cookie(default=None)):
        require_auth(session)
        c = get_console()
        with c.lock:
            projects = [c.project_view(p) for p in PROJECTS]
            tiers = {t: sum(p["tiers"][t] for p in projects)
                     for t in ("confirmed", "provisional", "review")}
            return {
                "as_of": as_of(),
                "totals": {
                    "area_ha": round(sum(p["area_ha"] for p in projects), 1),
                    "plots": sum(p["n_plots"] for p in projects),
                    "anchors": sum(p["n_anchors"] for p in projects),
                    "anchors_online": sum(p["anchors_online"] for p in projects),
                    "chain_alerts": sum(p["chain_alerts"] for p in projects),
                    "tiers": tiers,
                    "er_confirmed": round(sum(p["er_confirmed"] for p in projects), 1),
                    "er_provisional": round(sum(p["er_provisional"] for p in projects), 1),
                    "review_open": sum(p["review_open"] for p in projects),
                    "countries_active": len({p["country"] for p in projects}),
                    "countries_pipeline": len(PIPELINE),
                },
                "projects": projects, "pipeline": PIPELINE,
                "events": list(reversed(c.audit[-8:])),
                "benchmarks": BENCHMARKS,
            }

    @r.get("/projects/{pid}")
    def project(pid: str, session: str | None = Cookie(default=None)):
        require_auth(session)
        proj = proj_or_404(pid)
        c = get_console()
        with c.lock:
            plots = [c.plot_view(p) for p in c.plots.values() if p["project_id"] == pid]
            anchors = [c.anchor_view(a) for a in c.anchors.values() if a["project_id"] == pid]
            return {"as_of": as_of(), "project": c.project_view(proj), "plots": plots,
                    "anchors": anchors,
                    "rule": {"threshold_db": THRESHOLD_DB, "min_drain_days": MIN_DRAIN_DAYS}}

    @r.get("/plots/{plot_id}")
    def plot(plot_id: str, session: str | None = Cookie(default=None)):
        require_auth(session)
        c = get_console()
        with c.lock:
            p = c.plots.get(plot_id)
            if not p:
                raise HTTPException(404, "Unknown plot")
            out = {"plot": c.plot_view(p), "series": c.series[plot_id],
                   "nisar": c.nisar.get(plot_id), "review": c.review.get(plot_id),
                   "threshold_db": THRESHOLD_DB, "anchor": None, "anchor_daily": None}
            if p["anchor_id"]:
                a = c.anchors[p["anchor_id"]]
                out["anchor"] = c.anchor_view(a)
                out["anchor_daily"] = _daily(c.records[a["id"]])
            return out

    @r.get("/anchors")
    def anchors(session: str | None = Cookie(default=None)):
        require_auth(session)
        c = get_console()
        with c.lock:
            return {"as_of": as_of(),
                    "anchors": [c.anchor_view(a) for a in c.anchors.values()]}

    @r.get("/anchors/{aid}")
    def anchor(aid: str, session: str | None = Cookie(default=None)):
        require_auth(session)
        c = get_console()
        with c.lock:
            a = c.anchors.get(aid)
            if not a:
                raise HTTPException(404, "Unknown anchor")
            recs = c.records[aid]
            res = c.chain(aid)
            st = res["statuses"]
            # Hourly points for the chart (every 3rd hour keeps the payload small).
            series = [{"ts": x["ts"], "level_mm": x["level_mm"], "status": s}
                      for i, (x, s) in enumerate(zip(recs, st))
                      if i % 3 == 0 or s != "OK"]
            # Chain strip: one cell per day, worst status of that day.
            rank = {"OK": 0, "GAP": 1, "SEQ_CONFLICT": 2, "BROKEN": 3}
            days: dict[str, str] = {}
            for x, s in zip(recs, st):
                d = x["ts"][:10]
                if d not in days or rank[s] > rank[days[d]]:
                    days[d] = s
            flagged = [{"seq": x["seq"], "ts": x["ts"], "level_mm": x["level_mm"],
                        "status": s, "hash": x["hash"]}
                       for x, s in zip(recs, st) if s != "OK"][:20]
            tail = [{"seq": x["seq"], "ts": x["ts"], "level_mm": x["level_mm"],
                     "status": s, "hash": x["hash"], "prev_hash": x["prev_hash"]}
                    for x, s in list(zip(recs, st))[-6:]]
            return {"anchor": c.anchor_view(a), "series": series,
                    "days": [{"date": d, "status": s} for d, s in sorted(days.items())],
                    "flagged": flagged, "tail": tail}

    @r.post("/anchors/{aid}/tamper-demo")
    def tamper(aid: str, session: str | None = Cookie(default=None)):
        """Edit one stored record without the device key, as an attacker would."""
        require_auth(session)
        c = get_console()
        with c.lock:
            a = c.anchors.get(aid)
            if not a:
                raise HTTPException(404, "Unknown anchor")
            if a["tampered"]:
                raise HTTPException(409, "Already tampered. Reset the demo first.")
            recs = c.records[aid]
            # Pick a flooded reading in the middle of the log and make it look drained.
            mid = len(recs) // 2
            idx = next((i for i in range(mid, len(recs)) if recs[i]["level_mm"] > 20), mid)
            before = recs[idx]["level_mm"]
            recs[idx]["level_mm"] = -160
            a["tampered"] = True
            c._chains.pop(aid, None)
            summ = c.chain(aid)["summary"]
            c.log("demo", "record_edited", aid,
                  f"seq {recs[idx]['seq']}: level {before} mm changed to -160 mm without the device key")
            if summ["state"] == "BROKEN":
                c.log("system", "chain_broken", aid,
                      f"HMAC mismatch at seq {summ['first_break_seq']}; "
                      f"plot {a['plot_name']} moved to review")
            return {"seq": recs[idx]["seq"], "before_mm": before, "after_mm": -160,
                    "chain": summ}

    @r.post("/demo/reset")
    def reset(session: str | None = Cookie(default=None)):
        require_auth(session)
        get_console().reset()
        return {"ok": True}

    @r.get("/review")
    def review(session: str | None = Cookie(default=None)):
        require_auth(session)
        c = get_console()
        with c.lock:
            items = []
            for p in c.plots.values():
                v = c.plot_view(p)
                st = c.review.get(p["id"])
                if v["tier"] != "review" and not st:
                    continue
                items.append({**v, "review": st or {"status": "open", "assignee": None,
                                                    "outcome": None, "note": "", "updated": None}})
            order = {"open": 0, "assigned": 1, "resolved": 2}
            items.sort(key=lambda x: (order[x["review"]["status"]],
                                      -len(x["reasons"]), x["margin_db"], x["id"]))
            counts = {k: sum(1 for i in items if i["review"]["status"] == k) for k in order}
            return {"as_of": as_of(), "items": items, "counts": counts}

    @r.post("/review/{plot_id}")
    def review_act(plot_id: str, payload: dict, session: str | None = Cookie(default=None)):
        require_auth(session)
        c = get_console()
        with c.lock:
            p = c.plots.get(plot_id)
            if not p:
                raise HTTPException(404, "Unknown plot")
            action = payload.get("action")
            st = c.review.setdefault(plot_id, {"status": "open", "assignee": None,
                                               "outcome": None, "note": "", "updated": None})
            if action == "assign":
                st.update(status="assigned",
                          assignee=str(payload.get("assignee") or "Field team A")[:40])
                c.log("ops@paddyproof", "review_assigned", plot_id, st["assignee"])
            elif action == "resolve":
                outcome = payload.get("outcome")
                if outcome not in ("compliant", "non_compliant"):
                    raise HTTPException(400, "outcome must be compliant or non_compliant")
                st.update(status="resolved", outcome=outcome,
                          note=str(payload.get("note") or "")[:300],
                          assignee=st["assignee"] or "Field team A")
                p["verdict"] = outcome
                p["field_verified"] = True
                c.log("ops@paddyproof", "review_resolved", plot_id,
                      f"Field inspection: {outcome}" + (f" - {st['note']}" if st["note"] else ""))
            else:
                raise HTTPException(400, "action must be assign or resolve")
            st["updated"] = c.audit[-1]["ts"]
            return {"plot": c.plot_view(p), "review": st}

    @r.get("/audit")
    def audit(session: str | None = Cookie(default=None)):
        require_auth(session)
        c = get_console()
        with c.lock:
            return {"events": list(reversed(c.audit))}

    @r.get("/reports/{pid}")
    def report(pid: str, format: str = "json", session: str | None = Cookie(default=None)):
        require_auth(session)
        proj = proj_or_404(pid)
        c = get_console()
        with c.lock:
            view = c.project_view(proj)
            plots = [c.plot_view(p) for p in c.plots.values() if p["project_id"] == pid]
            anchors = [c.anchor_view(a) for a in c.anchors.values() if a["project_id"] == pid]
            reasons = {k: sum(1 for p in plots if k in p["reasons"]) for k in REASONS}
            data = {"as_of": as_of(), "project": view, "reasons": reasons,
                    "field_verified": sum(1 for p in plots if p["field_verified"]),
                    "anchors": [{"id": a["id"], "plot": a["plot_name"],
                                 "chain": a["chain"]["state"], "records": a["chain"]["records"],
                                 "head": a["chain"]["head"]} for a in anchors],
                    "audit_head": c.audit[-1]["hash"] if c.audit else None,
                    "audit_events": len(c.audit)}
            c_log = c.log
        if format == "json":
            return data
        if format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["plot_id", "lat", "lon", "area_ha", "tier", "verdict", "observations",
                        "longest_drain_days", "drain_cycles", "review_reasons", "nisar",
                        "anchor_id", "field_verified", "evidence_sha256", "data"])
            for p in plots:
                w.writerow([p["id"], p["lat"], p["lon"], p["area_ha"], p["tier"], p["verdict"],
                            p["n_obs"], p["longest_drain_days"], p["drain_cycles"],
                            ";".join(p["reasons"]), p["nisar"] or "", p["anchor_id"] or "",
                            p["field_verified"], p["evidence_sha256"], p["data"]])
            c_log("ops@paddyproof", "report_exported", pid, "Plot evidence table, CSV")
            return PlainTextResponse(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
                "Content-Disposition": f'attachment; filename="{pid}_plots.csv"'})
        if format == "md":
            c_log("ops@paddyproof", "report_exported", pid, "Draft MRV report, Markdown")
            return PlainTextResponse(_markdown(data), media_type="text/markdown; charset=utf-8", headers={
                "Content-Disposition": f'attachment; filename="{pid}_mrv_report_draft.md"'})
        raise HTTPException(400, "format must be json, csv or md")

    return r


def _daily(records: list[dict]) -> list[dict]:
    """Daily mean water level for the evidence chart."""
    acc: dict[str, list[int]] = {}
    for x in records:
        acc.setdefault(x["ts"][:10], []).append(x["level_mm"])
    return [{"date": d, "level_cm": round(sum(v) / len(v) / 10, 1)} for d, v in sorted(acc.items())]


def _markdown(d: dict) -> str:
    p = d["project"]
    t = p["tiers"]
    n = max(1, p["n_plots"])
    demo = p["data"] == "demo"
    rule = ("Mid-season drainage: at least 14 consecutive drained days (Sentinel-1 VH at or above -20 dB)"
            if p["rule"] == "msd14" else
            "AWD: at least 2 drain-and-reflood cycles seen by Sentinel-1 (VH threshold -20 dB). Exploratory rule.")
    L = [
        f"# MRV report (draft) - {p['name']}",
        "",
        f"Project {p['id']} · {p['region']}, {p['country_name']} · {p['season']}",
        f"Data as of {d['as_of']} · generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by PaddyProof console",
        "",
        "> **Status: draft, not verified, no credits issued.** This document is produced by the",
        "> PaddyProof console for internal review. It has not been submitted to or approved by",
        "> any registry or validation/verification body.",
    ]
    if demo:
        L += [">", "> **Demo data.** Plots, satellite series and anchor logs in this project are generated",
              "> from a fixed seed. They are not measurements."]
    else:
        L += [">", "> **Measured satellite data** (Sentinel-1 RTC, NISAR). Sample points have not been",
              "> checked against a paddy mask, and no anchors are deployed in this project."]
    L += [
        "", "## 1. Scope", "",
        f"- Programme: {p['programme']}",
        f"- Plots: {p['n_plots']} · area {p['area_ha']} ha",
        f"- Monitoring window: {p['window'][0]} to {p['window'][1]}",
        f"- Compliance rule: {rule}",
        f"- Mean satellite observations per plot: {p['mean_obs']}",
        f"- Anchors: {p['n_anchors']} ({p['anchors_online']} online)",
        "", "## 2. Result by tier", "",
        "| Tier | Plots | Share |", "|---|---:|---:|",
        f"| Confirmed | {t['confirmed']} | {t['confirmed'] / n:.1%} |",
        f"| Provisional | {t['provisional']} | {t['provisional'] / n:.1%} |",
        f"| Review (field inspection) | {t['review']} | {t['review'] / n:.1%} |",
        "",
        f"Compliant plots: {p['compliant']} of {p['n_plots']}. Settled by field inspection: {d['field_verified']}.",
        "", "## 3. Why plots are in review", "",
        "| Reason | Plots |", "|---|---:|",
        f"| Two or fewer satellite observations | {d['reasons']['few_observations']} |",
        f"| Verdict flips within +-1 dB of the threshold | {d['reasons']['threshold_margin']} |",
        f"| NISAR L-band contradicts Sentinel-1 | {d['reasons']['nisar_disagree']} |",
        f"| Anchor contradicts satellite | {d['reasons']['anchor_disagree']} |",
        f"| Anchor log failed chain verification | {d['reasons']['chain_broken']} |",
        "", "## 4. Estimated emission reduction (ex-ante, not issued)", "",
        f"- Factor: {p['ef']} tCO2e/ha per season ({p['ef_source']})",
        f"- Confirmed compliant plots: **{p['er_confirmed']} tCO2e**",
        f"- Provisional compliant plots: {p['er_provisional']} tCO2e",
        "- Review plots are excluded until inspected.",
        "", "## 5. Anchor log integrity", "",
    ]
    if d["anchors"]:
        L += ["| Anchor | Plot | Records | Chain | Chain head (HMAC-SHA256) |", "|---|---|---:|---|---|"]
        L += [f"| {a['id']} | {a['plot']} | {a['records']} | {a['chain']} | `{a['head'][:24]}...` |"
              for a in d["anchors"]]
    else:
        L += ["No anchors in this project."]
    L += [
        "", "## 6. Reproducibility", "",
        "- Every plot row in the CSV export carries the SHA-256 of its satellite series and rule.",
        f"- Audit log: {d['audit_events']} events, head `{(d['audit_head'] or '')[:24]}...`",
        "- Methodology references: Verra VM0051 v1.1 (structure only), IPCC 2019 Refinement Vol.4 Ch.5.",
        "", "## 7. Limits", "",
        "- Sentinel-1 C-band is masked by the canopy late in the season; drained readings then can be false.",
        "- The -20 dB threshold is not calibrated per region.",
        "- The anchor key is symmetric; a party holding the server key could forge records.",
        "",
    ]
    return "\n".join(L)
