#!/usr/bin/env bash
# 데모용 공개 주소 만들기 — 웹앱과 터널을 한 번에 띄운다.
#
#   ./demo.sh              비밀번호 자동 생성
#   ./demo.sh 내비밀번호     비밀번호 직접 지정
#
# Ctrl+C 를 누르면 둘 다 정리하고 종료한다.

set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PASSWORD="${1:-}"

# ---------------------------------------------------------------- 사전 점검
if [ ! -x .venv/bin/python ]; then
  echo "오류: .venv 가 없습니다. 먼저 아래를 실행하세요."
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "오류: cloudflared 가 없습니다. 먼저 설치하세요."
  echo "  brew install cloudflared"
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "오류: $PORT 포트를 이미 쓰고 있습니다. 기존 서버를 끄거나"
  echo "      PORT=8080 ./demo.sh 처럼 다른 포트를 지정하세요."
  exit 1
fi

# 비밀번호를 안 주면 매번 새로 만든다 (재사용으로 인한 노출 방지)
if [ -z "$PASSWORD" ]; then
  PASSWORD="paddy-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 6)"
fi

LOGDIR="$(mktemp -d)"
APP_LOG="$LOGDIR/app.log"
TUNNEL_LOG="$LOGDIR/tunnel.log"

cleanup() {
  echo
  echo "정리 중..."
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null
  [ -n "${TUNNEL_PID:-}" ] && kill "$TUNNEL_PID" 2>/dev/null
  wait 2>/dev/null
  rm -rf "$LOGDIR"
  echo "종료했습니다. 공개 주소는 더 이상 접속되지 않습니다."
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------- 웹앱 실행
echo "[1/2] 웹앱 시작..."
PADDY_WEB_PASSWORD="$PASSWORD" \
PADDY_MAX_PLOTS="${PADDY_MAX_PLOTS:-10}" \
PADDY_RATE_PER_HOUR="${PADDY_RATE_PER_HOUR:-20}" \
PYTHONUNBUFFERED=1 \
  .venv/bin/python web/app.py --port "$PORT" >"$APP_LOG" 2>&1 &
APP_PID=$!

for _ in $(seq 1 40); do
  curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/" && break
  sleep 0.5
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "웹앱이 시작하지 못했습니다:"
    tail -20 "$APP_LOG"
    exit 1
  fi
done
echo "      준비 완료 (로컬 http://127.0.0.1:$PORT)"

# ---------------------------------------------------------------- 터널 실행
#
# 두 가지 방식이 있다.
#
#   고정 주소 (PADDY_TUNNEL 설정) — 이름 있는 터널. 주소가 늘 같으므로
#     발표자료·문서에 넣어도 된다. 최초 1회 설정이 필요하다 (DEPLOY.md 참고).
#   임시 주소 (기본)               — 실행할 때마다 새 주소가 나온다.
#     빠르지만 주소를 어디에도 적어 둘 수 없다.
#
if [ -n "${PADDY_TUNNEL:-}" ]; then
  if [ -z "${PADDY_TUNNEL_HOSTNAME:-}" ]; then
    echo "오류: PADDY_TUNNEL 을 쓰려면 PADDY_TUNNEL_HOSTNAME 도 지정해야 합니다."
    echo "      예: PADDY_TUNNEL=paddyproof PADDY_TUNNEL_HOSTNAME=demo.example.com ./demo.sh"
    exit 1
  fi
  echo "[2/2] 고정 주소 터널 연결 중... (터널: $PADDY_TUNNEL)"
  cloudflared tunnel run --url "http://localhost:$PORT" "$PADDY_TUNNEL" \
    >"$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!
  URL="https://$PADDY_TUNNEL_HOSTNAME"
  FIXED=1

  # 연결이 실제로 성립했는지 확인한다
  for _ in $(seq 1 30); do
    grep -q "Registered tunnel connection" "$TUNNEL_LOG" && break
    sleep 1
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
      echo "터널 연결에 실패했습니다:"
      tail -20 "$TUNNEL_LOG"
      exit 1
    fi
  done
else
  echo "[2/2] 임시 공개 주소 생성 중... (10초쯤 걸립니다)"
  cloudflared tunnel --url "http://localhost:$PORT" >"$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!
  FIXED=0

  URL=""
  for _ in $(seq 1 60); do
    URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)
    [ -n "$URL" ] && break
    sleep 1
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
      echo "터널 생성에 실패했습니다:"
      tail -20 "$TUNNEL_LOG"
      exit 1
    fi
  done

  if [ -z "$URL" ]; then
    echo "터널 주소를 얻지 못했습니다. 로그:"
    tail -20 "$TUNNEL_LOG"
    exit 1
  fi
fi

# 실제로 밖에서 접속되는지 확인
sleep 3
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$URL" || echo 000)

echo
echo "================================================================"
echo "  공개 주소   $URL"
echo "  주소 유형   $([ "$FIXED" = "1" ] && echo "고정 — 발표자료에 넣어도 됩니다" \
                                        || echo "임시 — 실행할 때마다 바뀝니다")"
echo "  비밀번호    $PASSWORD"
echo "  외부 접속   $([ "$CODE" = "200" ] && echo "정상 (200)" || echo "확인 중 ($CODE) — 잠시 뒤 다시 열어보세요")"
echo "================================================================"
echo
if [ "$FIXED" != "1" ]; then
  echo "  ⚠ 이 주소는 임시입니다. 발표자료·문서에 적지 마세요."
  echo "    고정 주소를 만들려면 DEPLOY.md 의 '고정 주소' 절을 보세요."
  echo
fi
echo "  이 주소를 공유하세요. 접속하면 비밀번호를 묻습니다."
echo "  창을 닫거나 Ctrl+C 를 누르면 접속이 끊깁니다."
echo "  맥이 잠자기로 들어가도 끊기니 전원·절전 설정을 확인하세요."
echo

wait "$APP_PID"
