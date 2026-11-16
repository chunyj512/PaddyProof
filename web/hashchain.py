"""Anchor log hash chain (seq + HMAC-SHA256).

Each record carries the hash of the previous record, so a log cannot be edited,
reordered or thinned out without the server noticing. The verifier here is real
code; only the records fed to it by the console are demo data.

Statuses per record:
    OK            hash matches and links to the previous record
    BROKEN        content does not match its own HMAC, or it does not link back
    GAP           one or more seq numbers are missing before this record
                  (normal while a device is offline; cleared by backfill)
    SEQ_CONFLICT  a seq number arrived twice with different content; the later
                  arrival is quarantined and the first one kept

Known limit: the key is symmetric, so someone holding the server-side key could
forge records. Per-device signatures or external notarisation of the chain head
would close that; neither is implemented.
"""

from __future__ import annotations

import hashlib
import hmac

GENESIS = "0" * 64
FIELDS = ("device_id", "seq", "ts", "level_mm", "temp_c", "batt_mv")


def canonical(rec: dict, prev_hash: str) -> bytes:
    return "|".join([prev_hash] + [str(rec[f]) for f in FIELDS]).encode()


def sign(key: bytes, rec: dict, prev_hash: str) -> str:
    return hmac.new(key, canonical(rec, prev_hash), hashlib.sha256).hexdigest()


def build_chain(key: bytes, readings: list[dict]) -> list[dict]:
    """Device side: turn plain readings into chained records."""
    out, prev = [], GENESIS
    for r in readings:
        rec = {f: r[f] for f in FIELDS}
        rec["prev_hash"] = prev
        rec["hash"] = prev = sign(key, rec, prev)
        out.append(rec)
    return out


def verify_chain(key: bytes, records: list[dict]) -> dict:
    """Server side: check records in arrival order.

    Returns {"statuses": [...], "summary": {...}}. ``statuses`` is aligned with
    ``records``.
    """
    statuses: list[str] = []
    seen: dict[int, str] = {}
    last_seq: int | None = None
    last_hash = GENESIS
    first_break: int | None = None

    for rec in records:
        seq = rec["seq"]
        if seq in seen:
            statuses.append("OK" if seen[seq] == rec["hash"] else "SEQ_CONFLICT")
            continue

        self_ok = hmac.compare_digest(sign(key, rec, rec["prev_hash"]), rec["hash"])
        if not self_ok:
            status = "BROKEN"
        elif last_seq is not None and seq > last_seq + 1:
            status = "GAP"
        elif last_seq is not None and rec["prev_hash"] != last_hash:
            status = "BROKEN"
        elif last_seq is None and seq == 0 and rec["prev_hash"] != GENESIS:
            status = "BROKEN"
        else:
            status = "OK"

        if status == "BROKEN" and first_break is None:
            first_break = seq
        statuses.append(status)
        seen[seq] = rec["hash"]
        last_seq, last_hash = seq, rec["hash"]

    counts = {s: statuses.count(s) for s in ("OK", "BROKEN", "GAP", "SEQ_CONFLICT")}
    missing = 0
    seqs = sorted(seen)
    for a, b in zip(seqs, seqs[1:]):
        missing += b - a - 1
    if counts["BROKEN"]:
        state = "BROKEN"
    elif counts["SEQ_CONFLICT"]:
        state = "SEQ_CONFLICT"
    elif counts["GAP"]:
        state = "GAP"
    else:
        state = "OK"
    after_break = (sum(1 for r in records if r["seq"] > first_break)
                   if first_break is not None else 0)
    return {
        "statuses": statuses,
        "summary": {
            "state": state, "records": len(records), **counts,
            "missing": missing, "first_break_seq": first_break,
            "after_break": after_break,
            "head": last_hash if records else GENESIS,
        },
    }
