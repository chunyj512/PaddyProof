#!/usr/bin/env python3
"""
논물관리(AWD / 중간물떼기) 이행 검증 도구

Sentinel-1 SAR (VH 편파) 후방산란 시계열로 논의 담수/낙수 상태를 판별하고,
"연속 낙수 N일 이상" 기준 충족 여부를 판정한다.

데이터: Copernicus Data Space Ecosystem openEO API (SENTINEL1_GRD, sigma0)

사용 예:
    python paddy_check.py --lat 36.3721 --lon 127.3604 --year 2025
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# 기본 설정값
# --------------------------------------------------------------------------

DEFAULT_THRESHOLD_DB = -20.0   # 이 값 미만이면 담수(ponded), 이상이면 낙수(drained)
DEFAULT_BUFFER_M = 40.0        # 좌표 주변 버퍼 반경(m)
DEFAULT_MIN_DRAIN_DAYS = 14    # 이행 판정 기준 연속 낙수 일수
DEFAULT_SMOOTH_WINDOW = 3      # 이동평균 창 크기(관측점 수)
COLLECTION = "SENTINEL1_GRD"
BAND = "VH"
BACKEND = "openeo.dataspace.copernicus.eu"
# 버전 협상용 /.well-known/openeo 가 503 을 내면 클라이언트는 HTML 을 서빙하는
# 루트로 폴백해 JSON 파싱에 실패한다. 그럴 때 쓸 버전 고정 URL.
BACKEND_PINNED = "https://openeo.dataspace.copernicus.eu/openeo/1.2"

# openEO 처리 백엔드 장애 시 폴백: 같은 CDSE 계정 토큰으로 쓰는 Sentinel Hub API.
# 데이터(S1 GRD)와 보정(sigma0-ellipsoid, 비정사보정)은 openEO 경로와 동일하다.
OIDC_ISSUER = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
OIDC_CLIENT_ID = "sh-b1c3a958-52d4-40fe-a333-153595d1c71e"
SH_STATS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"


class PaddyCheckError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 오류."""


# --------------------------------------------------------------------------
# 인증 상태 점검
#
# 자격증명이 죽었을 때 조용히 멈추지 않고 즉시 이유를 말하게 하는 것이 목적이다.
# 예전에는 refresh token 이 만료되면 openEO 가 대화형 device flow 로 넘어가
# 아무도 응답하지 않는 채 5분을 기다렸고, 폴백인 Sentinel Hub 도 같은 죽은
# 토큰을 다시 써서 결국 두 경로가 모두 실패했다.
# --------------------------------------------------------------------------

AUTH_OK = "ok"
AUTH_STALE = "stale"        # 토큰은 있으나 만료됨 — 재로그인 필요
AUTH_MISSING = "missing"    # 자격증명 자체가 없음

_CC_HINT = (
    "서버로 운영한다면 개인 계정 대신 CDSE 서비스 계정(M2M)을 쓰세요.\n"
    "      https://dataspace.copernicus.eu → 계정 → Sentinel Hub → OAuth clients\n"
    "      발급받은 값을 .env 에 넣으면 만료 없이 자동 갱신됩니다:\n"
    "        OPENEO_AUTH_METHOD=client_credentials\n"
    "        OPENEO_AUTH_CLIENT_ID=...\n"
    "        OPENEO_AUTH_CLIENT_SECRET=...\n"
    "        OPENEO_AUTH_PROVIDER_ID=CDSE"
)


def has_client_credentials() -> bool:
    """서비스 계정(M2M) 자격증명이 환경변수에 설정되어 있는가."""
    return bool(os.environ.get("OPENEO_AUTH_CLIENT_ID")
                and os.environ.get("OPENEO_AUTH_CLIENT_SECRET"))


def _client_credentials_token() -> str:
    """서비스 계정으로 CDSE 액세스 토큰을 받는다."""
    import requests  # noqa: PLC0415

    resp = requests.post(
        f"{OIDC_ISSUER}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials",
              "client_id": os.environ["OPENEO_AUTH_CLIENT_ID"],
              "client_secret": os.environ["OPENEO_AUTH_CLIENT_SECRET"]},
        timeout=30,
    )
    if resp.status_code != 200:
        raise PaddyCheckError(
            f"서비스 계정 인증에 실패했습니다({resp.status_code}). "
            f"OPENEO_AUTH_CLIENT_ID / SECRET 값을 확인하세요."
        )
    return resp.json()["access_token"]


def _stored_refresh_token() -> str | None:
    try:
        from openeo.rest.auth.config import RefreshTokenStore  # noqa: PLC0415
        return RefreshTokenStore().get_refresh_token(
            issuer=OIDC_ISSUER, client_id=OIDC_CLIENT_ID)
    except Exception:
        return None


def check_auth() -> tuple[str, str]:
    """자격증명이 실제로 쓸 수 있는 상태인지 확인한다. (상태, 사람이 읽을 메시지)

    네트워크 왕복 한 번으로 끝나므로 분석 시작 전에 부담 없이 부를 수 있다.
    """
    import requests  # noqa: PLC0415

    if has_client_credentials():
        try:
            _client_credentials_token()
        except PaddyCheckError as exc:
            return AUTH_STALE, str(exc)
        except requests.RequestException as exc:
            return AUTH_STALE, f"인증 서버에 연결할 수 없습니다: {exc}"
        return AUTH_OK, "서비스 계정(client_credentials)으로 인증되어 있습니다."

    rt = _stored_refresh_token()
    if not rt:
        return AUTH_MISSING, (
            "Copernicus 자격증명이 없습니다.\n"
            "      로컬에서 쓰려면:  python auth_setup.py\n"
            f"      {_CC_HINT}")

    try:
        resp = requests.post(
            f"{OIDC_ISSUER}/protocol/openid-connect/token",
            data={"grant_type": "refresh_token", "refresh_token": rt,
                  "client_id": OIDC_CLIENT_ID},
            timeout=30,
        )
    except requests.RequestException as exc:
        return AUTH_STALE, f"인증 서버에 연결할 수 없습니다: {exc}"

    if resp.status_code == 200:
        return AUTH_OK, "저장된 Copernicus 로그인으로 인증되어 있습니다."

    detail = ""
    try:
        detail = resp.json().get("error_description", "")
    except ValueError:
        pass
    return AUTH_STALE, (
        f"Copernicus 로그인이 만료되었습니다({resp.status_code} {detail}).\n"
        "      로컬에서 쓰려면 다시 로그인하세요:  python auth_setup.py\n"
        f"      {_CC_HINT}")


# --------------------------------------------------------------------------
# 지오메트리
# --------------------------------------------------------------------------

def buffer_polygon(lat: float, lon: float, radius_m: float, n_vertices: int = 16) -> dict:
    """위경도 좌표 주변에 반경 radius_m 의 정n각형 폴리곤(GeoJSON)을 만든다.

    소규모(수십 m) 버퍼이므로 위도별 경도 축척 보정만 적용한 평면 근사로 충분하다.
    """
    if not -90.0 <= lat <= 90.0:
        raise PaddyCheckError(f"위도 값이 범위를 벗어났습니다: {lat} (-90 ~ 90)")
    if not -180.0 <= lon <= 180.0:
        raise PaddyCheckError(f"경도 값이 범위를 벗어났습니다: {lon} (-180 ~ 180)")

    deg_lat_per_m = 1.0 / 111_320.0
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-9:
        raise PaddyCheckError("극점 근처 좌표는 지원하지 않습니다.")
    deg_lon_per_m = 1.0 / (111_320.0 * cos_lat)

    ring = []
    for i in range(n_vertices):
        theta = 2.0 * math.pi * i / n_vertices
        ring.append([
            lon + radius_m * math.cos(theta) * deg_lon_per_m,
            lat + radius_m * math.sin(theta) * deg_lat_per_m,
        ])
    ring.append(ring[0])  # 폴리곤 닫기
    return {"type": "Polygon", "coordinates": [ring]}


def _bbox_of(geometry: dict) -> dict:
    """폴리곤의 경계 상자. load_collection 의 처리 범위를 좁혀 속도를 높인다."""
    ring = geometry["coordinates"][0]
    lons = [pt[0] for pt in ring]
    lats = [pt[1] for pt in ring]
    return {"west": min(lons), "east": max(lons),
            "south": min(lats), "north": max(lats)}


# --------------------------------------------------------------------------
# openEO 데이터 취득
# --------------------------------------------------------------------------

def connect_openeo(verbose: bool = True, interactive: bool = True):
    """openEO 백엔드에 접속해 인증한다.

    interactive=False 면 대화형 device flow 로 넘어가지 않는다. 웹 서버처럼
    아무도 코드를 입력해 줄 수 없는 환경에서 5분씩 멈추는 것을 막기 위해서다.
    """
    try:
        import openeo  # noqa: PLC0415
    except ImportError as exc:
        raise PaddyCheckError(
            "openeo 패키지가 설치되어 있지 않습니다.  pip install -r requirements.txt"
        ) from exc

    if verbose:
        print(f"[1/5] openEO 백엔드 접속: {BACKEND}")
        if interactive:
            print("      * 최초 실행 시 브라우저가 열리거나 터미널에 로그인 URL과")
            print("        인증 코드가 표시됩니다. Copernicus 계정으로 로그인하세요.")
            print("        (한 번 인증하면 refresh token 이 저장되어 이후에는 생략됩니다.)")

    if not interactive and not has_client_credentials():
        # 대화형 폴백이 없으므로, 쓸 수 있는 토큰인지 먼저 확인하고 아니면 즉시 끝낸다.
        state, msg = check_auth()
        if state != AUTH_OK:
            raise PaddyCheckError(msg)

    try:
        conn = _connect_with_fallback(openeo, verbose=verbose)
        if has_client_credentials():
            conn = conn.authenticate_oidc_client_credentials()
        else:
            # 이미 토큰 유효성을 확인했으므로 정상 경로에서는 폴링까지 가지 않는다.
            # 그래도 경합으로 만료되는 경우를 대비해 대기 시간을 짧게 묶는다.
            conn = conn.authenticate_oidc(max_poll_time=300 if interactive else 20)
    except Exception as exc:  # 네트워크/인증 실패 모두 포괄
        raise PaddyCheckError(
            f"openEO 인증 또는 접속에 실패했습니다: {exc}\n"
            "      - 네트워크 연결과 Copernicus Data Space 계정을 확인하세요.\n"
            "      - 인증 문제라면 python auth_setup.py 로 다시 로그인하세요.\n"
            "      - 백엔드 점검 중일 수 있습니다. 잠시 후 재시도하세요."
        ) from exc

    if verbose:
        print("      인증 완료.")
    return conn


def _connect_with_fallback(openeo, verbose: bool = True):
    """기본 접속이 버전 협상 장애로 실패하면 버전 고정 URL 로 재시도한다."""
    import requests  # noqa: PLC0415

    try:
        return openeo.connect(BACKEND)
    except (requests.JSONDecodeError, ValueError) as exc:
        # /.well-known/openeo 가 죽어 루트의 HTML 을 파싱하려다 난 오류로 본다.
        if verbose:
            print(f"      버전 협상 실패({exc.__class__.__name__}), "
                  f"버전 고정 URL 로 재시도합니다.")
        return openeo.connect(BACKEND_PINNED)


def fetch_vh_timeseries(
    conn,
    geometry: dict,
    start: date,
    end: date,
    verbose: bool = True,
    attempts: int = 4,
) -> "pd.DataFrame":
    """openEO 에서 VH sigma0 공간평균 시계열을 받아 dB 로 변환한 DataFrame 반환."""
    import pandas as pd  # noqa: PLC0415

    if verbose:
        print(f"[2/5] Sentinel-1 GRD 조회: {start} ~ {end}, 밴드 {BAND}")

    try:
        cube = conn.load_collection(
            COLLECTION,
            spatial_extent=_bbox_of(geometry),
            temporal_extent=[start.isoformat(), end.isoformat()],
            bands=[BAND],
        )
        # sigma0 (지형보정 없는 타원체 기준) 로 방사보정
        cube = cube.sar_backscatter(coefficient="sigma0-ellipsoid")
        cube = cube.aggregate_spatial(geometries=geometry, reducer="mean")
        result = _execute_with_retry(cube, attempts=attempts, verbose=verbose)
    except PaddyCheckError:
        raise
    except Exception as exc:
        raise PaddyCheckError(
            f"openEO 처리 요청이 실패했습니다: {exc}\n"
            "      - 백엔드 일시 장애이거나 해당 기간/영역에 자료가 없을 수 있습니다.\n"
            "      - 기간을 좁혀 다시 시도해 보세요."
        ) from exc

    records = _parse_aggregate_result(result)
    if not records:
        raise PaddyCheckError(
            "해당 기간·좌표에서 유효한 Sentinel-1 관측이 0건입니다.\n"
            "      - 좌표가 육지 위인지, 기간이 올바른지 확인하세요.\n"
            "      - 버퍼(--buffer)를 키우거나 기간을 넓혀 보세요."
        )

    df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    # 같은 날 여러 관측(서로 다른 궤도)은 평균으로 통합
    df = df.groupby("date", as_index=False)["sigma0"].mean()
    df["vh_db"] = 10.0 * df["sigma0"].apply(math.log10)
    df["date"] = pd.to_datetime(df["date"])

    if verbose:
        print(f"      유효 관측 {len(df)}건 수신.")
    return df


def _sh_access_token() -> str:
    """CDSE 액세스 토큰을 발급받는다.

    서비스 계정이 설정되어 있으면 그것을 쓰고, 없으면 저장된 refresh token 을 쓴다.
    폴백 경로가 주 경로와 같은 자격증명을 쓰므로, 서비스 계정을 넣어 두면
    두 경로 모두 만료 없이 동작한다.
    """
    import requests  # noqa: PLC0415
    from openeo.rest.auth.config import RefreshTokenStore  # noqa: PLC0415

    if has_client_credentials():
        return _client_credentials_token()

    store = RefreshTokenStore()
    rt = store.get_refresh_token(issuer=OIDC_ISSUER, client_id=OIDC_CLIENT_ID)
    if not rt:
        raise PaddyCheckError(
            "Copernicus 자격증명이 없습니다.\n"
            "      로컬에서 쓰려면:  python auth_setup.py\n"
            f"      {_CC_HINT}"
        )
    resp = requests.post(
        f"{OIDC_ISSUER}/protocol/openid-connect/token",
        data={"grant_type": "refresh_token", "refresh_token": rt,
              "client_id": OIDC_CLIENT_ID},
        timeout=30,
    )
    if resp.status_code != 200:
        raise PaddyCheckError(
            f"Copernicus 로그인이 만료되었습니다({resp.status_code}).\n"
            "      로컬에서 쓰려면 다시 로그인하세요:  python auth_setup.py\n"
            f"      {_CC_HINT}"
        )
    tokens = resp.json()
    # Keycloak 이 refresh token 을 회전시키면 새것을 저장해 openEO 인증도 유지한다.
    if tokens.get("refresh_token") and tokens["refresh_token"] != rt:
        store.set_refresh_token(issuer=OIDC_ISSUER, client_id=OIDC_CLIENT_ID,
                                refresh_token=tokens["refresh_token"])
    return tokens["access_token"]


_SH_EVALSCRIPT = """//VERSION=3
function setup(){return{input:[{bands:["VH","dataMask"]}],
  output:[{id:"data",bands:1},{id:"dataMask",bands:1}]};}
function evaluatePixel(s){return{data:[s.VH],dataMask:[s.dataMask]};}"""


def fetch_vh_timeseries_sh(
    geometry: dict,
    start: date,
    end: date,
    verbose: bool = True,
) -> "pd.DataFrame":
    """Sentinel Hub Statistical API 로 VH sigma0 공간평균 시계열을 받는다.

    openEO 경로와 동일한 자료(S1 GRD)·보정(sigma0-ellipsoid)이며,
    Statistical API 의 집계 구간 수 제한 때문에 80일 단위로 나눠 요청한다.
    """
    import pandas as pd  # noqa: PLC0415
    import requests  # noqa: PLC0415

    if verbose:
        print(f"[2/5] Sentinel-1 GRD 조회 (Sentinel Hub 폴백): {start} ~ {end}, 밴드 {BAND}")

    token = _sh_access_token()
    records: list[dict] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=80), end)
        req = {
            "input": {
                "bounds": {"geometry": geometry},
                "data": [{
                    "type": "sentinel-1-grd",
                    "processing": {"backCoeff": "SIGMA0_ELLIPSOID",
                                   "orthorectify": "false"},
                }],
            },
            "aggregation": {
                "timeRange": {"from": f"{chunk_start}T00:00:00Z",
                              "to": f"{chunk_end}T23:59:59Z"},
                "aggregationInterval": {"of": "P1D"},
                "width": 32, "height": 32,
                "evalscript": _SH_EVALSCRIPT,
            },
        }
        try:
            resp = requests.post(SH_STATS_URL, json=req, timeout=120,
                                 headers={"Authorization": f"Bearer {token}"})
        except requests.RequestException as exc:
            raise PaddyCheckError(f"Sentinel Hub 요청 실패: {exc}") from exc
        if resp.status_code != 200:
            raise PaddyCheckError(
                f"Sentinel Hub 응답 오류({resp.status_code}): {resp.text[:200]}"
            )
        for item in resp.json().get("data", []):
            try:
                stats = item["outputs"]["data"]["bands"]["B0"]["stats"]
            except KeyError:
                continue
            mean = stats.get("mean")
            n = stats.get("sampleCount", 0) - stats.get("noDataCount", 0)
            if mean is None or n <= 0 or not math.isfinite(mean) or mean <= 0:
                continue
            records.append({
                "date": _parse_timestamp(item["interval"]["from"]),
                "sigma0": float(mean),
            })
        chunk_start = chunk_end

    if not records:
        raise PaddyCheckError(
            "해당 기간·좌표에서 유효한 Sentinel-1 관측이 0건입니다.\n"
            "      - 좌표가 육지 위인지, 기간이 올바른지 확인하세요."
        )

    df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    df = df.groupby("date", as_index=False)["sigma0"].mean()
    df["vh_db"] = 10.0 * df["sigma0"].apply(math.log10)
    df["date"] = pd.to_datetime(df["date"])
    if verbose:
        print(f"      유효 관측 {len(df)}건 수신.")
    return df


def _execute_with_retry(cube, attempts: int = 4, verbose: bool = True):
    """일시적인 네트워크 오류(연결 끊김, 타임아웃)는 재시도한다.

    openEO 동기 처리 요청은 수십 초 걸릴 수 있어 회선이 불안정하면 중간에
    끊기기 쉽다. HTTP 오류(잘못된 요청 등)는 재시도해도 소용없으므로 즉시 올린다.
    """
    import time  # noqa: PLC0415

    import requests  # noqa: PLC0415
    from openeo.rest import OpenEoApiError, OpenEoApiPlainError  # noqa: PLC0415

    transient_http = {502, 503, 504}

    for attempt in range(1, attempts + 1):
        reason = None
        try:
            return cube.execute()
        except (requests.ConnectionError, requests.Timeout) as exc:
            reason = f"네트워크 오류({exc.__class__.__name__})"
            last = exc
        except (OpenEoApiError, OpenEoApiPlainError) as exc:
            if getattr(exc, "http_status_code", None) not in transient_http:
                raise
            reason = f"백엔드 일시 오류({exc.http_status_code})"
            last = exc

        if attempt == attempts:
            raise PaddyCheckError(
                f"openEO 조회가 {attempts}회 연속 실패했습니다: {last}\n"
                "      - 503 'no available server' 는 CDSE 처리 백엔드 점검·과부하입니다.\n"
                "        잠시(수십 분) 뒤 다시 시도하세요. 코드 문제가 아닙니다.\n"
                "      - 상태 확인: https://dataspace.copernicus.eu/news 또는 status 페이지"
            ) from last

        wait = min(2 ** attempt * 5, 60)
        if verbose:
            print(f"      {reason}, {wait}초 후 재시도 ({attempt}/{attempts - 1})")
        time.sleep(wait)


def _parse_aggregate_result(result) -> list[dict]:
    """aggregate_spatial 결과(dict: 날짜 -> [[값,...]])를 레코드 목록으로 정리.

    NaN / null / 0 이하 값은 sigma0 → dB 변환이 불가능하므로 버린다.
    """
    if isinstance(result, (bytes, str)):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise PaddyCheckError(f"openEO 응답을 해석할 수 없습니다: {exc}") from exc

    if not isinstance(result, dict):
        raise PaddyCheckError(f"예상치 못한 openEO 응답 형식입니다: {type(result).__name__}")

    records: list[dict] = []
    for ts, payload in result.items():
        values = _flatten_numbers(payload)
        values = [v for v in values if v is not None and not math.isnan(v) and v > 0]
        if not values:
            continue
        records.append({
            "date": _parse_timestamp(ts),
            "sigma0": sum(values) / len(values),
        })
    return records


def _flatten_numbers(payload) -> list[float]:
    """중첩 리스트에서 숫자만 평탄화해 추출."""
    out: list[float] = []
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, bool):
            continue
        elif isinstance(item, (int, float)):
            out.append(float(item))
    return out


def _parse_timestamp(ts: str) -> date:
    text = str(ts).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return datetime.strptime(str(ts)[:10], "%Y-%m-%d").date()


# --------------------------------------------------------------------------
# 판정 로직
# --------------------------------------------------------------------------

@dataclass
class DrainPeriod:
    """연속 낙수 구간."""
    start: date
    end: date

    @property
    def days(self) -> int:
        # 시작일 관측부터 낙수 상태가 시작되어 종료일 관측 시점까지 유지된 것으로 본다.
        return (self.end - self.start).days


def smooth(df: "pd.DataFrame", window: int) -> "pd.DataFrame":
    """이동평균 스무딩. 궤도별 노이즈 차이를 완화한다."""
    df = df.copy()
    if window and window > 1:
        df["vh_db_smooth"] = (
            df["vh_db"].rolling(window=window, center=True, min_periods=1).mean()
        )
    else:
        df["vh_db_smooth"] = df["vh_db"]
    return df


def classify(df: "pd.DataFrame", threshold_db: float) -> "pd.DataFrame":
    """임계값 기준으로 관측일별 담수/낙수 분류."""
    df = df.copy()
    df["drained"] = df["vh_db_smooth"] >= threshold_db
    df["state"] = df["drained"].map({True: "낙수", False: "담수"})
    return df


def find_drain_periods(df: "pd.DataFrame") -> list[DrainPeriod]:
    """연속 낙수 구간을 모두 찾는다.

    관측 간격 사이는 앞 관측의 상태가 유지된 것으로 간주한다. 구간의 시작·종료는
    낙수로 확인된 첫/마지막 관측일로 잡는다 — 실제 낙수는 그보다 이르게 시작해
    늦게 끝났을 수 있으므로 지속일수는 과소추정(보수적) 값이다.
    """
    periods: list[DrainPeriod] = []
    dates = [d.date() for d in df["date"]]
    flags = list(df["drained"])

    i = 0
    n = len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        start = dates[i]
        j = i
        while j + 1 < n and flags[j + 1]:
            j += 1
        # j 가 낙수로 확인된 마지막 관측. 실제로는 다음 관측 직전까지 낙수였을 수
        # 있으나, 보수적으로 마지막 낙수 관측일을 종료일로 사용한다.
        periods.append(DrainPeriod(start=start, end=dates[j]))
        i = j + 1
    return periods


# --------------------------------------------------------------------------
# 출력: 그래프
# --------------------------------------------------------------------------

def setup_korean_font() -> str:
    """macOS 한글 폰트 설정. 사용된 폰트명을 반환."""
    import matplotlib  # noqa: PLC0415
    from matplotlib import font_manager  # noqa: PLC0415

    available = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in ("AppleGothic", "Apple SD Gothic Neo", "NanumGothic", "Malgun Gothic"):
        if candidate in available:
            matplotlib.rcParams["font.family"] = candidate
            matplotlib.rcParams["axes.unicode_minus"] = False  # 마이너스 기호 깨짐 방지
            return candidate
    matplotlib.rcParams["axes.unicode_minus"] = False
    return "(한글 폰트 없음 - 라벨이 깨질 수 있습니다)"


def plot_timeseries(
    df: "pd.DataFrame",
    periods: list[DrainPeriod],
    threshold_db: float,
    lat: float,
    lon: float,
    out_path: Path,
    min_drain_days: int,
    verbose: bool = True,
) -> None:
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    import matplotlib.dates as mdates  # noqa: PLC0415

    font_used = setup_korean_font()
    if verbose:
        print(f"[4/5] 그래프 생성 (폰트: {font_used})")

    fig, ax = plt.subplots(figsize=(12, 6))

    # 배경 음영: 전 구간을 담수(파랑)로 깔고 낙수 구간만 주황으로 덮는다.
    xs = list(df["date"])
    ax.axvspan(xs[0], xs[-1], color="#4A90D9", alpha=0.12, label="담수 구간(추정)")
    for k, p in enumerate(periods):
        ax.axvspan(
            datetime.combine(p.start, datetime.min.time()),
            datetime.combine(p.end, datetime.min.time()),
            color="#F5A623",
            alpha=0.35,
            label="낙수 구간(추정)" if k == 0 else None,
        )

    # 원자료 + 스무딩
    ax.plot(df["date"], df["vh_db"], color="#B0B0B0", lw=1.0, ls="--",
            marker="o", ms=3.5, label="VH 원자료")
    ax.plot(df["date"], df["vh_db_smooth"], color="#1F3A5F", lw=2.0,
            marker="o", ms=5, label="VH 이동평균(3점)")

    # 담수/낙수 관측점 강조
    ponded = df[~df["drained"]]
    drained = df[df["drained"]]
    if not ponded.empty:
        ax.scatter(ponded["date"], ponded["vh_db_smooth"], s=55, zorder=5,
                   color="#1565C0", edgecolor="white", linewidth=0.8, label="담수 판정 관측")
    if not drained.empty:
        ax.scatter(drained["date"], drained["vh_db_smooth"], s=55, zorder=5,
                   color="#E65100", edgecolor="white", linewidth=0.8, label="낙수 판정 관측")

    ax.axhline(threshold_db, color="#C62828", lw=1.5, ls=":",
               label=f"담수 임계값 {threshold_db:.1f} dB")

    ax.set_xlabel("날짜")
    ax.set_ylabel("Sentinel-1 VH 후방산란 (dB)")
    ax.set_title(
        f"논물관리 이행 검증 — Sentinel-1 VH 시계열\n"
        f"위치 {lat:.5f}, {lon:.5f}  |  기준: 연속 낙수 {min_drain_days}일 이상"
    )
    ax.grid(alpha=0.25, ls="-", lw=0.5)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.legend(loc="best", fontsize=9, framealpha=0.9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

    if verbose:
        print(f"      저장: {out_path}")


# --------------------------------------------------------------------------
# 출력: 리포트
# --------------------------------------------------------------------------

def build_report(
    df: "pd.DataFrame",
    periods: list[DrainPeriod],
    lat: float,
    lon: float,
    start: date,
    end: date,
    threshold_db: float,
    buffer_m: float,
    min_drain_days: int,
    smooth_window: int,
    png_path: Path,
) -> str:
    longest = max((p.days for p in periods), default=0)
    compliant = longest >= min_drain_days
    verdict = "**이행**" if compliant else "**미이행**"

    obs_dates = [d.date() for d in df["date"]]
    gaps = [(b - a).days for a, b in zip(obs_dates, obs_dates[1:])]
    mean_gap = sum(gaps) / len(gaps) if gaps else 0.0
    max_gap = max(gaps) if gaps else 0

    n_drained = int(df["drained"].sum())
    n_ponded = len(df) - n_drained

    lines: list[str] = []
    A = lines.append

    A("# 논물관리(AWD/중간물떼기) 이행 검증 리포트")
    A("")
    A(f"생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    A("")
    A("## 1. 분석 개요")
    A("")
    A("| 항목 | 값 |")
    A("| --- | --- |")
    A(f"| 필지 좌표 | 위도 {lat:.6f}, 경도 {lon:.6f} |")
    A(f"| 분석 영역 | 좌표 중심 반경 {buffer_m:.0f} m 버퍼 폴리곤 |")
    A(f"| 분석 기간 | {start} ~ {end} ({(end - start).days}일) |")
    A(f"| 데이터 | Copernicus Sentinel-1 GRD, {BAND} 편파, sigma0 (dB) |")
    A(f"| 총 관측 횟수 | {len(df)}회 (담수 {n_ponded}회 / 낙수 {n_drained}회) |")
    A(f"| 평균 재방문 간격 | {mean_gap:.1f}일 (최대 {max_gap}일) |")
    A(f"| 담수 판정 임계값 | {threshold_db:.1f} dB 미만 = 담수, 이상 = 낙수 |")
    A(f"| 스무딩 | 이동평균 {smooth_window}점 (중심 정렬) |")
    A(f"| 이행 판정 기준 | 연속 낙수 {min_drain_days}일 이상 |")
    A("")

    A("## 2. 판정 결과")
    A("")
    A(f"### 최종 판정: {verdict}")
    A("")
    A(f"- 최장 연속 낙수 일수: **{longest}일**")
    A(f"- 기준({min_drain_days}일) 대비: {longest - min_drain_days:+d}일")
    if compliant:
        A(f"- 판정 근거: {min_drain_days}일 이상 연속된 낙수 구간이 "
          f"{sum(1 for p in periods if p.days >= min_drain_days)}건 확인됨.")
    elif periods:
        A(f"- 판정 근거: 낙수 구간은 {len(periods)}건 확인되었으나, "
          f"가장 긴 구간이 {longest}일로 기준 {min_drain_days}일에 미달.")
    else:
        A("- 판정 근거: 분석 기간 내 낙수로 분류된 관측이 없음 "
          "(전 기간 상시담수 상태로 추정).")
    A("")

    A("## 3. 낙수 구간 목록")
    A("")
    if periods:
        A("| # | 시작일 | 종료일 | 지속일수 | 관측 수 | 기준 충족 |")
        A("| --- | --- | --- | --- | --- | --- |")
        for k, p in enumerate(periods, 1):
            n_obs = int(((df["date"].dt.date >= p.start) & (df["date"].dt.date <= p.end)).sum())
            mark = "O" if p.days >= min_drain_days else "X"
            A(f"| {k} | {p.start} | {p.end} | {p.days}일 | {n_obs}회 | {mark} |")
    else:
        A("낙수로 분류된 구간이 없습니다.")
    A("")

    A("## 4. 관측 원자료")
    A("")
    A("| 날짜 | VH 원자료 (dB) | 이동평균 (dB) | 판정 |")
    A("| --- | --- | --- | --- |")
    for _, row in df.iterrows():
        A(f"| {row['date'].date()} | {row['vh_db']:.2f} | "
          f"{row['vh_db_smooth']:.2f} | {row['state']} |")
    A("")

    A("## 5. 그래프")
    A("")
    A(f"![VH 시계열]({png_path.name})")
    A("")

    A("## 6. 판정 근거와 한계")
    A("")
    A("### 판정 원리")
    A("")
    A("담수된 논 표면은 경면반사(specular reflection)를 일으켜 레이더 신호 대부분을")
    A("위성 반대 방향으로 반사하므로 후방산란이 매우 낮게 나타납니다. 낙수 후에는")
    A("노출된 토양과 벼 캐노피에서 산란이 증가해 후방산란이 상승합니다. VH 교차편파는")
    A("이 수면/식생 대비에 특히 민감해 담수 판별에 사용했습니다.")
    A("")
    A("### 한계 및 오차 요인")
    A("")
    A(f"1. **관측 간격으로 인한 시점 오차**: Sentinel-1 재방문 간격 때문에 실제 낙수")
    A(f"   시작·종료 시점은 관측일 사이 어딘가입니다. 본 분석의 평균 관측 간격은")
    A(f"   {mean_gap:.1f}일, 최대 {max_gap}일이므로 각 구간의 시작·종료일에")
    A(f"   최대 **±{max_gap}일**의 불확실성이 있습니다. 지속일수는 관측이 확인된")
    A("   범위만 세는 보수적(과소추정) 방식이라 실제 낙수 기간은 더 길 수 있습니다.")
    A("")
    A(f"2. **임계값 가정**: {threshold_db:.1f} dB 는 일반적인 논 담수 판별 경험값입니다.")
    A("   지역·토양·품종·생육단계·입사각에 따라 최적 임계값이 달라지므로, 인근")
    A("   상시담수 필지의 값을 참조해 `--threshold` 로 보정하는 것이 좋습니다.")
    A("")
    A("3. **생육 후기 캐노피 차폐**: 벼가 무성해지면 수면이 캐노피에 가려 담수 상태에도")
    A("   후방산란이 올라갑니다. 출수기 이후 구간은 낙수로 과대판정될 수 있습니다.")
    A("")
    A("4. **궤도·입사각 혼재**: 서로 다른 relative orbit 관측이 섞이면 계통적 dB 차이가")
    A(f"   생깁니다. 본 분석은 {smooth_window}점 이동평균으로 완화했으나 완전히")
    A("   제거되지는 않습니다. 정밀 분석에는 단일 궤도만 사용하는 것이 좋습니다.")
    A("")
    A("5. **강우 영향**: 강한 강우 직후에는 토양 수분 증가로 후방산란이 일시적으로")
    A("   낮아져 담수로 오판될 수 있습니다.")
    A("")
    A(f"6. **공간 평균**: 반경 {buffer_m:.0f} m 버퍼 내 평균값이므로, 필지 경계나 인접")
    A("   수체·도로가 포함되면 값이 왜곡됩니다. 필지 중앙 좌표 사용을 권장합니다.")
    A("")
    A("> 본 리포트는 위성 원격탐사 기반 **추정**이며, 공식 이행 확인을 대체하지 않습니다.")
    A("> 현장 점검·영농일지 등 보조 근거와 함께 활용하십시오.")
    A("")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sentinel-1 SAR 기반 논물관리(AWD/중간물떼기) 이행 검증",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예시:\n"
               "  python paddy_check.py --lat 36.3721 --lon 127.3604 --year 2025\n"
               "  python paddy_check.py --lat 36.3721 --lon 127.3604 "
               "--start 2025-05-01 --end 2025-09-30 --threshold -19\n",
    )
    p.add_argument("--lat", type=float, required=True, help="필지 중심 위도")
    p.add_argument("--lon", type=float, required=True, help="필지 중심 경도")
    p.add_argument("--year", type=int, help="분석 연도 (기본 기간: 5/1 ~ 9/30)")
    p.add_argument("--start", type=str, help="분석 시작일 YYYY-MM-DD (--year 보다 우선)")
    p.add_argument("--end", type=str, help="분석 종료일 YYYY-MM-DD (--year 보다 우선)")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_DB,
                   help=f"담수 판정 임계값 dB (기본 {DEFAULT_THRESHOLD_DB})")
    p.add_argument("--buffer", type=float, default=DEFAULT_BUFFER_M,
                   help=f"버퍼 반경 m (기본 {DEFAULT_BUFFER_M:.0f})")
    p.add_argument("--min-days", type=int, default=DEFAULT_MIN_DRAIN_DAYS,
                   help=f"이행 기준 연속 낙수 일수 (기본 {DEFAULT_MIN_DRAIN_DAYS})")
    p.add_argument("--smooth", type=int, default=DEFAULT_SMOOTH_WINDOW,
                   help=f"이동평균 창 크기, 1이면 스무딩 없음 (기본 {DEFAULT_SMOOTH_WINDOW})")
    p.add_argument("--outdir", type=str, default="output", help="결과 저장 디렉터리")
    p.add_argument("--csv", action="store_true", help="관측 시계열을 CSV로도 저장")
    p.add_argument("--source", choices=["auto", "openeo", "sentinelhub"], default="auto",
                   help="데이터 경로 (기본 auto: openEO 실패 시 Sentinel Hub 폴백)")
    p.add_argument("--non-interactive", action="store_true",
                   help="대화형 로그인을 시도하지 않는다 (웹 서버 등 무인 환경용). "
                        "인증이 만료되었으면 기다리지 않고 바로 실패한다.")
    return p.parse_args(argv)


def resolve_period(args: argparse.Namespace) -> tuple[date, date]:
    if args.start or args.end:
        if not (args.start and args.end):
            raise PaddyCheckError("--start 와 --end 는 함께 지정해야 합니다.")
        try:
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
        except ValueError as exc:
            raise PaddyCheckError(f"날짜 형식이 잘못되었습니다 (YYYY-MM-DD): {exc}") from exc
    else:
        year = args.year or date.today().year
        start, end = date(year, 5, 1), date(year, 9, 30)

    if end <= start:
        raise PaddyCheckError(f"종료일({end})은 시작일({start})보다 뒤여야 합니다.")
    if start > date.today():
        raise PaddyCheckError(f"시작일({start})이 미래입니다. 관측 자료가 없습니다.")
    if end > date.today():
        end = date.today()
        print(f"      * 종료일이 미래여서 오늘({end})로 조정했습니다.")
    return start, end


def main(argv: list[str] | None = None) -> int:
    # openeo 라이브러리가 503 응답마다 찍는 "Failed to parse API error response"
    # 경고는 사용자에게 혼란만 주므로 숨긴다.
    import logging
    logging.getLogger("openeo").setLevel(logging.CRITICAL)

    try:
        args = parse_args(argv)
        start, end = resolve_period(args)

        outdir = Path(args.outdir).expanduser().resolve()
        outdir.mkdir(parents=True, exist_ok=True)
        stem = f"paddy_{args.lat:.4f}_{args.lon:.4f}_{start:%Y%m%d}_{end:%Y%m%d}"
        png_path = outdir / f"{stem}.png"
        md_path = outdir / f"{stem}.md"

        geometry = buffer_polygon(args.lat, args.lon, args.buffer)

        interactive = not args.non_interactive

        # 두 경로가 같은 자격증명을 쓰므로, 죽은 토큰이면 폴백도 실패한다.
        # 헛되이 오래 기다리지 않도록 시작 전에 한 번 확인한다.
        if not interactive:
            state, msg = check_auth()
            if state != AUTH_OK:
                raise PaddyCheckError(msg)

        if args.source == "sentinelhub":
            df = fetch_vh_timeseries_sh(geometry, start, end)
        elif args.source == "openeo":
            conn = connect_openeo(interactive=interactive)
            df = fetch_vh_timeseries(conn, geometry, start, end)
        else:  # auto: openEO 우선, 백엔드 장애 시 즉시 Sentinel Hub 로 폴백
            try:
                conn = connect_openeo(interactive=interactive)
                # 폴백이 있으므로 재시도 없이 첫 실패에 바로 전환한다.
                df = fetch_vh_timeseries(conn, geometry, start, end, attempts=1)
            except PaddyCheckError as exc:
                print(f"      openEO 경로 실패: {str(exc).splitlines()[0]}")
                print("      Sentinel Hub 경로로 전환합니다 (동일 자료·동일 보정).")
                df = fetch_vh_timeseries_sh(geometry, start, end)

        print(f"[3/5] 담수/낙수 판정 (임계값 {args.threshold:.1f} dB)")
        df = smooth(df, args.smooth)
        df = classify(df, args.threshold)
        periods = find_drain_periods(df)
        longest = max((p.days for p in periods), default=0)
        print(f"      낙수 구간 {len(periods)}건, 최장 {longest}일")

        plot_timeseries(df, periods, args.threshold, args.lat, args.lon,
                        png_path, args.min_days)

        print("[5/5] 리포트 작성")
        report = build_report(df, periods, args.lat, args.lon, start, end,
                              args.threshold, args.buffer, args.min_days,
                              args.smooth, png_path)
        md_path.write_text(report, encoding="utf-8")
        print(f"      저장: {md_path}")

        if args.csv:
            csv_path = outdir / f"{stem}.csv"
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"      저장: {csv_path}")

        verdict = "이행" if longest >= args.min_days else "미이행"
        print()
        print("=" * 56)
        print(f"  판정: {verdict}  (최장 연속 낙수 {longest}일 / 기준 {args.min_days}일)")
        print("=" * 56)
        return 0

    except PaddyCheckError as exc:
        print(f"\n[오류] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n중단되었습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
