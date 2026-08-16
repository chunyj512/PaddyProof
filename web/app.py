#!/usr/bin/env python3
"""
논물관리 이행 검증 - 웹 프론트엔드

paddy_check.py 를 서브프로세스로 호출하는 얇은 래퍼다. 분석 로직은 전혀
건드리지 않으며, 임계값 민감도 분석에만 판정 함수를 그대로 import 해서 쓴다.

    python web/app.py            # http://127.0.0.1:8000
    PADDY_WEB_PASSWORD=xxx python web/app.py --host 0.0.0.0   # 공유 시
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "paddy_check.py"
OUTDIR = ROOT / "output"
STATIC = Path(__file__).resolve().parent / "static"
PYTHON = sys.executable

PASSWORD = os.environ.get("PASSWORD") or os.environ.get("PADDY_WEB_PASSWORD")
_sessions: set[str] = set()

# 공개 배포 시 남용을 막는 상한. 환경변수로 조정한다.
MAX_PLOTS = int(os.environ.get("PADDY_MAX_PLOTS", "300"))
RATE_PER_HOUR = int(os.environ.get("PADDY_RATE_PER_HOUR", "0"))  # 0 = 제한 없음
MAX_CONCURRENT = int(os.environ.get("PADDY_MAX_CONCURRENT", "2"))


class RateLimiter:
    """IP 단위 시간당 요청 수와 동시 실행 수를 제한한다.

    단일 프로세스 기준의 간단한 구현이다. 여러 워커로 늘리면 공유 저장소가 필요하다.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._running: dict[str, int] = {}
        self._lock = threading.Lock()

    def check(self, ip: str) -> None:
        import time as _t

        now = _t.time()
        with self._lock:
            if MAX_CONCURRENT and self._running.get(ip, 0) >= MAX_CONCURRENT:
                raise HTTPException(
                    429, f"이미 실행 중인 분석이 {MAX_CONCURRENT}건입니다. "
                         f"끝난 뒤 다시 시도하세요.")
            if RATE_PER_HOUR:
                hits = [t for t in self._hits.get(ip, []) if now - t < 3600]
                if len(hits) >= RATE_PER_HOUR:
                    raise HTTPException(
                        429, f"시간당 요청 한도({RATE_PER_HOUR}회)를 초과했습니다. "
                             f"잠시 후 다시 시도하세요.")
                hits.append(now)
                self._hits[ip] = hits

    def enter(self, ip: str) -> None:
        with self._lock:
            self._running[ip] = self._running.get(ip, 0) + 1

    def leave(self, ip: str) -> None:
        with self._lock:
            n = self._running.get(ip, 1) - 1
            if n <= 0:
                self._running.pop(ip, None)
            else:
                self._running[ip] = n


_limiter = RateLimiter()


def client_ip(request) -> str:
    """리버스 프록시 뒤에 있을 때도 실제 클라이언트를 식별한다."""
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else None) or (
        request.client.host if request.client else "unknown")

app = FastAPI(title="논물관리 이행 검증")


# --------------------------------------------------------------------------
# 작업 관리
# --------------------------------------------------------------------------

@dataclass
class Plot:
    """분석 대상 필지 하나."""
    name: str
    lat: float
    lon: float
    status: str = "대기"          # 대기 | 진행 | 완료 | 실패
    verdict: str | None = None    # 이행 | 미이행
    longest: int | None = None
    n_obs: int | None = None
    png: str | None = None
    md: str | None = None
    csv: str | None = None
    error: str | None = None
    source: str | None = None     # openEO | Sentinel Hub
    nisar: str | None = None      # NISAR 교차 증거 verdict
    nisar_reason: str | None = None
    nisar_detail: dict | None = None  # 교차 증거 표에 쓸 값들 (재조회 없이 화면에 전달)
    combined_png: str | None = None   # S1+NISAR 를 겹쳐 그린 통합 그래프

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Job:
    id: str
    plots: list[Plot]
    params: dict
    events: list[dict] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    _cv: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, kind: str, **payload) -> None:
        with self._cv:
            self.events.append({"kind": kind, **payload})
            self._cv.notify_all()

    def finish(self) -> None:
        with self._cv:
            self.done = True
            self._cv.notify_all()


_jobs: dict[str, Job] = {}


# --------------------------------------------------------------------------
# 분석 실행
# --------------------------------------------------------------------------

_SAVE_RE = re.compile(r"저장:\s*(\S+)")
_VERDICT_RE = re.compile(r"판정:\s*(이행|미이행)\s*\(최장 연속 낙수 (\d+)일")
_OBS_RE = re.compile(r"유효 관측 (\d+)건")


def run_one(job: Job, idx: int, plot: Plot) -> None:
    """paddy_check.py 를 서브프로세스로 실행하고 진행 상황을 스트리밍한다."""
    p = job.params
    cmd = [
        PYTHON, str(SCRIPT),
        "--lat", f"{plot.lat}", "--lon", f"{plot.lon}",
        "--start", p["start"], "--end", p["end"],
        "--threshold", str(p["threshold"]),
        "--buffer", str(p["buffer"]),
        "--min-days", str(p["min_days"]),
        "--smooth", str(p["smooth"]),
        "--outdir", str(OUTDIR),
        "--csv",
        # 웹에서는 아무도 로그인 코드를 입력해 줄 수 없다. 인증이 만료되었으면
        # 대화형 흐름으로 넘어가 5분을 기다리지 말고 바로 실패해야 한다.
        "--non-interactive",
    ]
    if p.get("source") and p["source"] != "auto":
        cmd += ["--source", p["source"]]

    plot.status = "진행"
    job.emit("plot", index=idx, plot=plot.to_dict())

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=str(ROOT))
    except OSError as exc:
        plot.status, plot.error = "실패", f"실행 불가: {exc}"
        job.emit("plot", index=idx, plot=plot.to_dict())
        return

    tail: list[str] = []
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-40]

        if "Sentinel Hub 경로로 전환" in line:
            plot.source = "Sentinel Hub"
        elif "Sentinel-1 GRD 조회" in line and plot.source is None:
            plot.source = "Sentinel Hub" if "Sentinel Hub" in line else "openEO"

        if m := _OBS_RE.search(line):
            plot.n_obs = int(m.group(1))
        if m := _SAVE_RE.search(line):
            path = Path(m.group(1))
            setattr(plot, path.suffix.lstrip("."), path.name)
        if m := _VERDICT_RE.search(line):
            plot.verdict, plot.longest = m.group(1), int(m.group(2))

        job.emit("log", index=idx, line=line)

    proc.wait()
    if proc.returncode == 0 and plot.verdict:
        plot.status = "완료"
        if p.get("nisar") and plot.csv:
            _attach_cross_check(job, idx, plot, p)
    else:
        plot.status = "실패"
        err = next((l for l in reversed(tail) if "[오류]" in l), None)
        plot.error = (err or "\n".join(tail[-3:]) or "알 수 없는 오류").replace("[오류] ", "")
    job.emit("plot", index=idx, plot=plot.to_dict())


def _attach_cross_check(job: Job, idx: int, plot: Plot, p: dict) -> None:
    """NISAR 교차 증거를 붙이고 두 센서를 겹쳐 그린 통합 그래프를 만든다.

    통합 그래프는 NISAR 를 못 얻어도 그린다. 그래야 결과 화면에 늘 같은 그래프가
    놓이고, '교차 검증을 아직 안 한 것' 과 '할 수 없는 것' 이 구분된다.
    """
    sys.path.insert(0, str(ROOT))
    job.emit("log", index=idx, line="      NISAR 교차 증거 조회 중...")

    rows = None
    try:
        from nisar_evidence import cross_check
        res = cross_check(plot.lat, plot.lon, OUTDIR / plot.csv,
                          buffer_m=float(p["buffer"]), verbose=False)
        plot.nisar = res["verdict"]
        plot.nisar_reason = res.get("reason")
        rows = res.get("rows") or []
        for r in rows:
            r["date"] = str(r["date"])[:10]
        plot.nisar_detail = {
            k: res[k] for k in
            ("reason", "hh_drained", "hh_ponded", "hh_diff", "n_ponded",
             "n_drained", "n_nisar")
            if k in res
        }
        plot.nisar_detail["rows"] = rows
    except Exception as exc:
        plot.nisar = "error"
        plot.nisar_reason = str(exc)[:200]
        job.emit("log", index=idx, line=f"      NISAR 실패: {str(exc)[:60]}")

    try:
        from nisar_compare import render_combined_png
        name = f"combined_{Path(plot.csv).stem}.png"
        out = render_combined_png(
            plot.lat, plot.lon, OUTDIR / plot.csv, OUTDIR / name,
            threshold=float(p["threshold"]), min_days=int(p["min_days"]),
            nisar_rows=rows, buffer_m=float(p["buffer"]), verbose=False,
        )
        plot.combined_png = out["png"]
        job.emit("log", index=idx, line=f"      저장: {out['png']}")
    except Exception as exc:
        job.emit("log", index=idx, line=f"      통합 그래프 실패: {str(exc)[:80]}")


def run_job(job: Job) -> None:
    for idx, plot in enumerate(job.plots):
        if job.cancelled:
            plot.status = "취소"
            job.emit("plot", index=idx, plot=plot.to_dict())
            continue
        try:
            run_one(job, idx, plot)
        except Exception as exc:  # 한 필지 실패가 전체를 멈추지 않도록
            plot.status, plot.error = "실패", str(exc)
            job.emit("plot", index=idx, plot=plot.to_dict())
    job.emit("done")
    job.finish()


# --------------------------------------------------------------------------
# 임계값 민감도 (판정 로직은 paddy_check 것을 그대로 사용)
# --------------------------------------------------------------------------

def sensitivity(csv_name: str, min_days: int, base: float) -> list[dict]:
    """임계값을 ±1 dB 흔들어 판정이 유지되는지 확인한다."""
    sys.path.insert(0, str(ROOT))
    import pandas as pd
    from paddy_check import classify, find_drain_periods

    df = pd.read_csv(OUTDIR / csv_name, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"])

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
    return out


# --------------------------------------------------------------------------
# 인증 (비밀번호 미설정 시 통과 — 로컬 사용)
# --------------------------------------------------------------------------

def require_auth(session: str | None) -> None:
    if PASSWORD and session not in _sessions:
        raise HTTPException(status_code=401, detail="인증이 필요합니다")


@app.get("/api/auth")
def auth_status(session: str | None = Cookie(default=None)):
    return {"required": bool(PASSWORD), "ok": not PASSWORD or session in _sessions}


# 위성 자격증명 점검 결과를 잠깐 캐시한다. 매번 확인하면 화면을 열 때마다
# 인증 서버로 왕복이 생긴다.
_cred_cache: dict = {"at": 0.0, "value": None}
CRED_TTL = 120.0


@app.get("/api/credentials")
def credentials(session: str | None = Cookie(default=None)):
    """위성 자료 자격증명이 살아 있는지 알려준다.

    분석을 눌러 실패를 겪기 전에 화면에서 먼저 알 수 있게 하려는 것이다.
    """
    require_auth(session)
    import time as _t

    now = _t.time()
    if _cred_cache["value"] and now - _cred_cache["at"] < CRED_TTL:
        return _cred_cache["value"]

    sys.path.insert(0, str(ROOT))
    try:
        from paddy_check import AUTH_OK, check_auth
        state, message = check_auth()
        value = {"state": state, "ok": state == AUTH_OK, "message": message}
    except Exception as exc:
        value = {"state": "unknown", "ok": True,
                 "message": f"자격증명 상태를 확인하지 못했습니다: {exc}"}

    _cred_cache.update({"at": now, "value": value})
    return value


@app.post("/api/login")
def login(response: Response, password: str = Form(...)):
    if not PASSWORD:
        return {"ok": True}
    if not secrets.compare_digest(password, PASSWORD):
        raise HTTPException(status_code=401, detail="비밀번호가 틀렸습니다")
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400)
    return {"ok": True}


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def _num(v: Any, name: str, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} 값이 숫자가 아닙니다: {v!r}")
    if not lo <= x <= hi:
        raise HTTPException(400, f"{name} 값이 범위({lo}~{hi})를 벗어났습니다: {x}")
    return x


@app.post("/api/analyze")
async def analyze(payload: dict, request: Request,
                  session: str | None = Cookie(default=None)):
    require_auth(session)
    ip = client_ip(request)
    _limiter.check(ip)

    raw_plots = payload.get("plots") or []
    if not raw_plots:
        raise HTTPException(400, "분석할 필지가 없습니다.")
    if len(raw_plots) > MAX_PLOTS:
        raise HTTPException(400, f"한 번에 {MAX_PLOTS}필지까지만 가능합니다.")

    plots = []
    for i, rp in enumerate(raw_plots, 1):
        plots.append(Plot(
            name=str(rp.get("name") or f"필지{i}")[:40],
            lat=_num(rp.get("lat"), "위도", -90, 90),
            lon=_num(rp.get("lon"), "경도", -180, 180),
        ))

    try:
        start = date.fromisoformat(payload["start"])
        end = date.fromisoformat(payload["end"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, f"기간 형식이 잘못되었습니다: {exc}")
    if end <= start:
        raise HTTPException(400, "종료일은 시작일보다 뒤여야 합니다.")

    params = {
        "start": start.isoformat(), "end": end.isoformat(),
        "threshold": _num(payload.get("threshold", -20), "임계값", -40, 0),
        "buffer": _num(payload.get("buffer", 40), "버퍼", 10, 500),
        "min_days": int(_num(payload.get("min_days", 14), "기준일수", 1, 200)),
        "smooth": int(_num(payload.get("smooth", 3), "스무딩", 1, 9)),
        "source": payload.get("source", "auto"),
        # 단건 분석은 교차 검증을 기본으로 켠다. 결과 화면의 주 그래프가
        # 두 센서를 겹쳐 그린 통합 그래프이기 때문이다.
        "nisar": bool(payload.get("nisar", True)),
    }

    job = Job(id=uuid.uuid4().hex[:12], plots=plots, params=params)
    _jobs[job.id] = job

    _limiter.enter(ip)

    def _run() -> None:
        try:
            run_job(job)
        finally:
            _limiter.leave(ip)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job.id, "plots": [p.to_dict() for p in plots], "params": params}


@app.get("/api/jobs/{job_id}/stream")
async def stream(job_id: str, session: str | None = Cookie(default=None)):
    require_auth(session)
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")

    async def gen():
        # 주의: 여기서 threading.Condition.wait() 를 쓰면 이벤트 루프 전체가
        # 멈춰 다른 요청(예: NISAR 조회)이 무한 대기한다. 비동기 폴링을 쓴다.
        sent = 0
        idle = 0.0
        while True:
            with job._cv:
                batch = job.events[sent:]
                sent += len(batch)
                done = job.done and sent >= len(job.events)

            for ev in batch:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if done:
                break

            if batch:
                idle = 0.0
            else:
                # 조용할 때는 주기적으로 주석을 보내 프록시가 끊지 않게 한다.
                idle += 0.2
                if idle >= 15.0:
                    idle = 0.0
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str, session: str | None = Cookie(default=None)):
    require_auth(session)
    if job := _jobs.get(job_id):
        job.cancelled = True
        return {"ok": True}
    raise HTTPException(404, "작업을 찾을 수 없습니다.")


@app.get("/api/sensitivity")
def api_sensitivity(csv: str, min_days: int = 14, threshold: float = -20.0,
                    session: str | None = Cookie(default=None)):
    require_auth(session)
    name = Path(csv).name
    if not (OUTDIR / name).is_file():
        raise HTTPException(404, "결과 파일을 찾을 수 없습니다.")
    try:
        return {"rows": sensitivity(name, min_days, threshold)}
    except Exception as exc:
        raise HTTPException(500, f"민감도 계산 실패: {exc}")


@app.get("/api/nisar")
def api_nisar(csv: str, lat: float, lon: float, buffer: float = 40.0,
              session: str | None = Cookie(default=None)):
    """NISAR L-band 교차 증거. S1 판정은 그대로 두고 캐노피 과대판정만 점검한다."""
    require_auth(session)
    name = Path(csv).name
    if not (OUTDIR / name).is_file():
        raise HTTPException(404, "S1 결과 파일을 찾을 수 없습니다.")
    sys.path.insert(0, str(ROOT))
    try:
        from nisar_evidence import cross_check
    except ImportError as exc:
        raise HTTPException(503, f"NISAR 모듈을 불러올 수 없습니다: {exc}")
    try:
        res = cross_check(lat, lon, OUTDIR / name, buffer_m=buffer, verbose=False)
    except Exception as exc:
        raise HTTPException(500, f"NISAR 교차 검증 실패: {exc}")
    for r in res.get("rows", []):
        r["date"] = str(r["date"])[:10]

    # 관측이 있으면 S1+NISAR 통합 비교 그래프도 만들어 준다 (캐시라 수 초).
    if res.get("rows"):
        try:
            from nisar_compare import render_compare_png
            png_name = f"nisarcmp_{Path(name).stem}.png"
            render_compare_png(lat, lon, OUTDIR / name, OUTDIR / png_name)
            res["png"] = png_name
        except Exception:
            pass  # 그래프는 부가 정보 — 실패해도 교차 증거는 그대로 반환한다
    return res


@app.post("/api/parse-csv")
async def parse_csv(file: UploadFile, session: str | None = Cookie(default=None)):
    """이름,위도,경도 형태의 CSV 를 필지 목록으로 변환."""
    require_auth(session)
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        raise HTTPException(400, "빈 파일입니다.")

    def is_num(s: str) -> bool:
        try:
            float(s)
            return True
        except ValueError:
            return False

    # 첫 줄이 헤더면 건너뛴다
    if len(rows[0]) >= 2 and not all(is_num(c.strip()) for c in rows[0][-2:]):
        rows = rows[1:]

    plots, errors = [], []
    for n, row in enumerate(rows, 1):
        cells = [c.strip() for c in row if c.strip()]
        if len(cells) < 2:
            continue
        try:
            if len(cells) >= 3 and is_num(cells[1]) and is_num(cells[2]):
                name, lat, lon = cells[0], float(cells[1]), float(cells[2])
            elif is_num(cells[0]) and is_num(cells[1]):
                name, lat, lon = f"필지{len(plots) + 1}", float(cells[0]), float(cells[1])
            else:
                raise ValueError("좌표를 찾을 수 없음")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("좌표 범위 초과")
            plots.append({"name": name, "lat": lat, "lon": lon})
        except ValueError as exc:
            errors.append(f"{n}행: {exc}")

    if not plots:
        raise HTTPException(400, "읽을 수 있는 좌표가 없습니다. 형식: 이름,위도,경도")
    return {"plots": plots, "errors": errors[:10]}


@app.get("/api/history")
def history(session: str | None = Cookie(default=None)):
    require_auth(session)
    items = []
    for md in sorted(OUTDIR.glob("paddy_*.md"), key=lambda p: p.stat().st_mtime,
                     reverse=True)[:60]:
        stem = md.stem
        verdict = longest = None
        try:
            text = md.read_text(encoding="utf-8")
            if m := re.search(r"최종 판정: \*\*(이행|미이행)\*\*", text):
                verdict = m.group(1)
            if m := re.search(r"최장 연속 낙수 일수: \*\*(\d+)일\*\*", text):
                longest = int(m.group(1))
        except OSError:
            pass
        parts = stem.split("_")
        items.append({
            "stem": stem,
            "lat": parts[1] if len(parts) > 2 else "",
            "lon": parts[2] if len(parts) > 3 else "",
            "period": f"{parts[3]}~{parts[4]}" if len(parts) > 4 else "",
            "verdict": verdict, "longest": longest,
            "when": datetime.fromtimestamp(md.stat().st_mtime).strftime("%m-%d %H:%M"),
            "png": f"{stem}.png" if (OUTDIR / f"{stem}.png").exists() else None,
        })
    return {"items": items}


@app.get("/api/files/{name}")
def get_file(name: str, session: str | None = Cookie(default=None)):
    require_auth(session)
    safe = Path(name).name
    path = OUTDIR / safe
    if not path.is_file() or path.suffix not in {".png", ".md", ".csv"}:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    media = {".png": "image/png", ".md": "text/markdown; charset=utf-8",
             ".csv": "text/csv; charset=utf-8"}[path.suffix]
    return FileResponse(path, media_type=media)


@app.get("/healthz")
def healthz():
    """배포 플랫폼의 상태 점검용. 인증을 요구하지 않는다.

    위성 자격증명 확인은 넣지 않는다 — 자격증명이 만료되었다고 컨테이너를
    재시작해도 나아지지 않고, 재시작 루프만 생긴다. 그건 /api/credentials 로 본다.
    """
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    # 브라우저가 옛 JS 를 캐시하면 수정 사항이 반영되지 않으므로 캐시를 금지한다.
    return HTMLResponse(
        (STATIC / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(HTTPException)
def http_error(request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def main() -> None:
    ap = argparse.ArgumentParser(description="논물관리 이행 검증 웹 UI")
    # 대부분의 배포 플랫폼(Render, Railway, Fly 등)은 PORT 환경변수로 포트를 준다.
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                    help="0.0.0.0 으로 열면 외부 접속 허용 (비밀번호 설정 권장)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()

    OUTDIR.mkdir(exist_ok=True)
    if args.host != "127.0.0.1" and not PASSWORD:
        print("경고: 외부에 열면서 비밀번호가 없습니다. "
              "PADDY_WEB_PASSWORD 환경변수를 설정하세요.\n")
    print(f"  논물관리 이행 검증  →  http://{args.host}:{args.port}")
    print(f"  인증: {'비밀번호 필요' if PASSWORD else '없음 (로컬 전용)'}\n")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
