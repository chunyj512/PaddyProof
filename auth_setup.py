#!/usr/bin/env python3
"""
Copernicus Data Space openEO 인증 (device flow, 네트워크 재시도 포함)

openeo 클라이언트의 authenticate_oidc() 는 승인 대기 폴링 중 일시적인 네트워크
오류(read timeout, connection reset)가 한 번만 나도 전체 인증을 중단한다.
회선이 불안정한 환경에서는 이 스크립트로 먼저 인증해 refresh token 을 저장한 뒤
paddy_check.py 를 실행하면 된다.

    python auth_setup.py
    python paddy_check.py --lat ... --lon ...
"""

import base64
import hashlib
import os
import sys
import time

import requests
from openeo.rest.auth.config import RefreshTokenStore

ISSUER = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
CLIENT_ID = "sh-b1c3a958-52d4-40fe-a333-153595d1c71e"  # CDSE openEO 기본 클라이언트
SCOPE = "openid email profile"
HTTP_TIMEOUT = 30       # 클라이언트 기본 5초는 너무 짧다
MAX_NET_RETRIES = 8     # 연속 네트워크 오류 허용 횟수


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def post_with_retry(session, url, data, label):
    """네트워크 오류는 재시도하고, HTTP 응답은 그대로 돌려준다."""
    failures = 0
    while True:
        try:
            return session.post(url, data=data, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            failures += 1
            if failures > MAX_NET_RETRIES:
                raise SystemExit(f"[오류] {label} 요청이 {failures}회 연속 실패했습니다: {exc}")
            wait = min(2 ** failures, 15)
            print(f"      네트워크 오류({exc.__class__.__name__}), {wait}초 후 재시도 "
                  f"({failures}/{MAX_NET_RETRIES})")
            time.sleep(wait)


def main() -> int:
    session = requests.Session()

    print("[1/3] OIDC 설정 조회")
    disc = session.get(f"{ISSUER}/.well-known/openid-configuration",
                       timeout=HTTP_TIMEOUT).json()
    device_endpoint = disc["device_authorization_endpoint"]
    token_endpoint = disc["token_endpoint"]

    verifier, challenge = pkce_pair()

    print("[2/3] 디바이스 코드 발급")
    resp = post_with_retry(session, device_endpoint, {
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }, "디바이스 코드")
    if resp.status_code != 200:
        print(f"[오류] 디바이스 코드 발급 실패 ({resp.status_code}): {resp.text[:300]}",
              file=sys.stderr)
        return 1
    dev = resp.json()

    url = dev.get("verification_uri_complete") or dev["verification_uri"]
    interval = max(int(dev.get("interval", 5)), 5)
    expires_in = int(dev.get("expires_in", 600))

    print()
    print("=" * 68)
    print("  브라우저에서 아래 주소를 열고 Copernicus 계정으로 로그인하세요.")
    print()
    print(f"  {url}")
    print()
    print(f"  사용자 코드: {dev['user_code']}")
    print(f"  유효 시간: {expires_in // 60}분")
    print("=" * 68)
    print()

    print("[3/3] 승인 대기 중...")
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        resp = post_with_retry(session, token_endpoint, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": dev["device_code"],
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        }, "토큰 교환")

        if resp.status_code == 200:
            tokens = resp.json()
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                print("[오류] 응답에 refresh_token 이 없습니다.", file=sys.stderr)
                return 1
            RefreshTokenStore().set_refresh_token(
                issuer=ISSUER, client_id=CLIENT_ID, refresh_token=refresh_token
            )
            print("\n인증 완료. refresh token 을 저장했습니다.")
            print("이제 paddy_check.py 를 실행하세요.")
            return 0

        error = resp.json().get("error", "")
        if error == "authorization_pending":
            remaining = int(deadline - time.time())
            print(f"      대기 중... (남은 시간 {remaining}초)")
        elif error == "slow_down":
            interval += 5
            print(f"      서버 요청으로 폴링 간격을 {interval}초로 늘렸습니다.")
        elif error == "expired_token":
            print("\n[오류] 코드가 만료되었습니다. 스크립트를 다시 실행하세요.",
                  file=sys.stderr)
            return 1
        else:
            print(f"\n[오류] 인증 실패: {error} — "
                  f"{resp.json().get('error_description', resp.text[:200])}",
                  file=sys.stderr)
            return 1

    print("\n[오류] 제한 시간 내에 승인되지 않았습니다. 다시 실행하세요.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
