# 배포 안내

로컬에서 혼자 쓰는 것과, 다른 사람이 접속하는 서버에 올리는 것은 준비물이 다릅니다.
이 문서는 후자를 다룹니다.

## 먼저 읽어야 할 것 — 계정 문제

이 도구는 위성 데이터를 받기 위해 **Copernicus 계정**을 씁니다. 서버에 올리면
접속한 모든 사람의 요청이 **그 서버에 설정된 계정 하나**로 나갑니다.

| 방식 | 위험 | 적합한 경우 |
| --- | --- | --- |
| 개인 계정 | 처리 할당량 소진, 남용 시 계정 정지, 약관 위반 소지 | 비밀번호 건 소규모 데모 |
| 서비스 계정 (M2M) | 낮음 | 공개 서비스 |

**공개 서비스로 운영할 계획이면 개인 계정을 쓰지 마세요.**

### 서비스 계정 만드는 법

1. https://dataspace.copernicus.eu 로그인
2. 우상단 계정 메뉴 → **Sentinel Hub** → **OAuth clients**
3. **Create new OAuth client** → 이름 입력 → 생성
4. 표시되는 **Client ID**와 **Client Secret**을 복사 (Secret은 이때 한 번만 보입니다)
5. `.env`에 아래처럼 넣습니다

```bash
OPENEO_AUTH_METHOD=client_credentials
OPENEO_AUTH_CLIENT_ID=발급받은_ID
OPENEO_AUTH_CLIENT_SECRET=발급받은_시크릿
OPENEO_AUTH_PROVIDER_ID=CDSE
```

NASA Earthdata(NISAR)는 서비스 계정을 제공하지 않습니다. 이 서비스 전용 계정을
새로 만들어 `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`로 넣으세요. 넣지 않으면
NISAR 교차검증만 비활성화되고 나머지는 정상 동작합니다.

## 설정

```bash
cp .env.example .env
# .env 를 열어 값을 채웁니다
```

`.env`는 `.gitignore`에 있습니다. **절대 커밋하지 마세요.**

반드시 설정할 것:

```bash
PADDY_WEB_PASSWORD=충분히_긴_비밀번호
```

비밀번호를 비워두면 인증 없이 누구나 접속합니다. 외부에 열 때는 필수입니다.

## Docker로 실행

```bash
docker build -t paddyproof .
docker run -d -p 8000:8000 --env-file .env --name paddyproof paddyproof
```

로그 확인:

```bash
docker logs -f paddyproof
```

## Docker 없이 실행

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a && source .env && set +a
.venv/bin/python web/app.py --host 0.0.0.0 --port 8000
```

## 남용 방지

기본 상한은 `.env`에서 조정합니다.

| 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `PADDY_RATE_PER_HOUR` | 20 | IP당 시간당 분석 요청 (0이면 무제한) |
| `PADDY_MAX_PLOTS` | 50 | 일괄 분석 1회 최대 필지 수 |
| `PADDY_MAX_CONCURRENT` | 2 | IP당 동시 실행 작업 수 |

필지 하나에 30초~5분이 걸립니다. 무제한으로 열면 서버가 금방 멈춥니다.
공개 데모라면 `PADDY_MAX_PLOTS`를 10 이하로 낮추는 것을 권합니다.

## HTTPS

비밀번호를 평문으로 주고받지 않으려면 리버스 프록시로 TLS를 붙이세요.
Caddy가 가장 간단합니다.

```
your-domain.com {
    reverse_proxy localhost:8000
}
```

앱은 `X-Forwarded-For` 헤더를 읽어 실제 클라이언트 IP로 요청 수를 셉니다.

## 문제가 생겼을 때

**계정이 남용되는 것 같다면** — CDSE 대시보드에서 해당 OAuth 클라이언트를
삭제하면 즉시 차단됩니다. 개인 계정을 쓰고 있다면 비밀번호를 바꾸고
`refresh-tokens.json`을 지우세요.

**분석이 느리거나 멈춘다면** — Copernicus 백엔드 장애일 수 있습니다.
앱이 자동으로 Sentinel Hub 경로로 전환하지만, 둘 다 죽으면 기다려야 합니다.

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://openeo.dataspace.copernicus.eu/.well-known/openeo
```

**메모리 부족** — NISAR 교차검증은 위성 파일을 원격에서 읽어 메모리를 씁니다.
무료 클라우드 티어(512MB 등)에서는 실패할 수 있습니다. 그런 환경에서는
Earthdata 자격증명을 비워 NISAR 기능을 끄고 운영하세요.

## 운영 시 주의

- `output/` 폴더에 분석 결과가 계속 쌓입니다. 사용자가 입력한 **필지 좌표**가
  파일명에 들어가므로, 공개 서버에서는 주기적으로 정리하거나 별도 볼륨에
  두는 것이 좋습니다.
- 결과 파일에는 접근 제어가 없습니다. 파일명을 아는 사람은 누구나
  `/api/files/...`로 받을 수 있습니다(비밀번호 통과 후). 민감한 필지를 다룬다면
  사용자별 분리가 필요합니다.
