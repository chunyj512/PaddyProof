# 배포 안내

로컬에서 혼자 쓰는 것과, 다른 사람이 접속하는 서버에 올리는 것은 준비물이 다릅니다.
이 문서는 후자를 다룹니다.

**발표자료에 링크를 넣는 것이 목적이라면 바로 아래 '상시 접속되는 주소에 올리기'
로 가세요.** 나머지 절은 직접 서버를 운영할 때 참고하는 내용입니다.

## 왜 개인 계정을 서버에 올리면 안 되는가

이 도구는 위성 데이터를 받기 위해 **Copernicus 계정**을 씁니다. 서버에 올리면
접속한 모든 사람의 요청이 **그 서버에 설정된 계정 하나**로 나갑니다.

| 방식 | 위험 | 적합한 경우 |
| --- | --- | --- |
| 개인 계정 | 며칠 만에 토큰 만료, 처리 할당량 소진, 약관 위반 소지 | 로컬 개발 |
| 서비스 계정 (M2M) | 낮음, 만료 없음 | 배포하는 모든 경우 |

개인 로그인 토큰은 **며칠 쓰지 않으면 만료**됩니다. 만료된 채로 배포된 서비스는
접속은 되는데 분석만 전부 실패하는, 알아채기 어려운 상태가 됩니다.

NASA Earthdata(NISAR)는 서비스 계정을 제공하지 않습니다. 이 서비스 전용 계정을
새로 만들어 `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`로 넣으세요. 넣지 않으면
NISAR 교차검증만 비활성화되고 나머지는 정상 동작합니다.

## 설정 (직접 서버를 운영할 때)

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

## 상시 접속되는 주소에 올리기 (권장)

발표자료에 링크를 넣으려면 이 방법입니다. 노트북이 꺼져 있어도 열립니다.
저장소에 `render.yaml` 과 `Dockerfile` 이 준비되어 있어 CLI 설치는 필요 없습니다.

준비물은 두 가지이고, **둘 다 직접 만드셔야 합니다** (자격증명이라 대신 만들 수 없습니다).

### 1단계 — Copernicus 서비스 계정 만들기

가장 중요한 단계입니다. 개인 로그인 토큰은 며칠 만에 만료되어 배포한 서비스가
조용히 죽습니다. 서비스 계정은 만료가 없습니다.

1. https://dataspace.copernicus.eu 로그인
2. 우상단 계정 메뉴 → **Sentinel Hub** → **OAuth clients**
3. **Create new OAuth client** → 이름 입력 → 생성
4. **Client ID** 와 **Client Secret** 을 복사 (Secret 은 이때 한 번만 보입니다)

### 2단계 — Render 에 올리기

1. https://render.com 가입 (GitHub 계정으로 로그인하면 저장소가 바로 붙습니다)
2. **New → Blueprint** → `chunyj512/PaddyProof` 저장소 선택
3. Render 가 `render.yaml` 을 읽고 아래 값을 물어봅니다. 채워 넣으세요.

   | 항목 | 넣을 값 |
   | --- | --- |
   | `PADDY_WEB_PASSWORD` | 심사위원에게 알려줄 접속 비밀번호 |
   | `OPENEO_AUTH_CLIENT_ID` | 1단계에서 받은 Client ID |
   | `OPENEO_AUTH_CLIENT_SECRET` | 1단계에서 받은 Client Secret |
   | `EARTHDATA_USERNAME` | NASA Earthdata 아이디 (없으면 비워 두기) |
   | `EARTHDATA_PASSWORD` | NASA Earthdata 비밀번호 (없으면 비워 두기) |

4. **Apply** 를 누르면 5~10분 뒤 `https://paddyproof.onrender.com` 같은
   **고정 주소**가 나옵니다. 이 주소를 PPT 에 넣으세요.

배포가 끝나면 그 주소로 들어가 확인하세요. 자격증명이 잘못되었으면 화면 위에
붉은 배너로 알려줍니다 — 분석을 눌러 볼 필요 없습니다.

### 무료 플랜의 함정

Render 무료 플랜은 **15분간 아무도 안 들어오면 잠들고, 깨어나는 데 1분쯤**
걸립니다. 심사위원이 링크를 눌렀는데 1분간 빈 화면이 뜨는 상황이 생깁니다.

발표를 앞두고 있다면 둘 중 하나를 하세요.

- Render 대시보드에서 **Starter 플랜**($7/월)으로 올린다 — 잠들지 않습니다.
- 발표 직전에 링크를 한 번 열어 깨워 둔다 — 공짜지만 15분 지나면 다시 잠듭니다.

`render.yaml` 의 `plan: free` 를 `plan: starter` 로 바꿔 커밋해도 됩니다.

### NISAR 교차 검증과 메모리

NISAR 는 위성 파일을 원격에서 읽어 메모리를 많이 씁니다. 무료 플랜(512 MB)에서
**처음 보는 필지**를 교차 검증하면 실패할 수 있습니다.

그래서 `seed/nisar_cache.json` 에 데모 필지들(익산·논산·김제 등 14곳)의 관측을
미리 담아 두었고, Dockerfile 이 이걸 이미지에 넣습니다. **이 필지들은 즉시
교차 검증이 됩니다.** 시연은 이 좌표로 하세요.

Earthdata 자격증명을 비워 두면 NISAR 만 꺼지고 Sentinel-1 판정은 정상 동작합니다.

### 다른 선택지

| 방식 | 링크 | 비용 | 준비물 |
| --- | --- | --- | --- |
| Render 무료 | 고정, 15분 뒤 잠듦 | 0원 | GitHub 계정 |
| Render Starter | 고정, 상시 | $7/월 | 카드 |
| Hugging Face Spaces | 고정, 상시(48시간 유휴 시 잠듦) | 0원 | HF 계정, Docker SDK 설정 |
| 고정 Cloudflare 터널 | 고정, **맥이 켜져 있을 때만** | 0원 | Cloudflare 계정 + 도메인 |

## 고정 주소 — 노트북에서 바로 띄울 때

`./demo.sh` 를 그냥 실행하면 `https-무작위문자.trycloudflare.com` 형태의 **임시**
주소가 나옵니다. 실행할 때마다 바뀌므로 PPT·문서에 적으면 안 됩니다.

주소를 고정하려면 이름 있는 터널(named tunnel)을 한 번 만들어 둡니다.
Cloudflare 계정과 그 계정에 등록된 도메인이 필요합니다.

```bash
cloudflared tunnel login
```

```bash
cloudflared tunnel create paddyproof
```

```bash
cloudflared tunnel route dns paddyproof demo.내도메인.com
```

이후에는 아래처럼 실행하면 늘 같은 주소로 열립니다.

```bash
PADDY_TUNNEL=paddyproof PADDY_TUNNEL_HOSTNAME=demo.내도메인.com ./demo.sh
```

두 값을 `.env` 에 넣어 두면 매번 타이프하지 않아도 됩니다.

### 주의 — 노트북이 꺼지면 링크도 죽습니다

터널은 이 맥에서 도는 서버로 연결을 넘겨줄 뿐입니다. 주소는 고정되어도
**맥이 꺼지거나 잠자기로 들어가면 접속되지 않습니다.** 발표 중에만 켜 두는
용도라면 충분하지만, 심사위원이 아무 때나 열어 보는 링크로 쓸 생각이라면
아래처럼 상시 켜져 있는 곳에 올리세요.

| 방식 | 링크 안정성 | 준비물 |
| --- | --- | --- |
| 임시 터널 | 실행마다 바뀜 | 없음 |
| 고정 터널 (위 절차) | 주소 고정, 맥이 켜져 있을 때만 접속 | Cloudflare 계정 + 도메인 |
| 클라우드 배포 (Render/Fly/Railway 등) | 항상 접속 | 계정 + 서비스 계정 자격증명 |

클라우드에 올릴 때는 이 저장소의 `Dockerfile` 을 그대로 쓰면 됩니다. 앱은
`PORT` 환경변수를 읽으므로 대부분의 플랫폼에서 별도 설정 없이 뜹니다.
`.env` 의 값들은 그 플랫폼의 환경변수 화면에 넣으세요. **개인 계정 토큰이 아니라
서비스 계정(client_credentials)을 쓰는 것이 중요합니다** — 개인 로그인은 며칠 만에
만료되어 배포된 서비스가 조용히 죽습니다.

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
