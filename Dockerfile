# 논물관리 이행 검증 — 웹 서비스
#
# 빌드:  docker build -t paddyproof .
# 실행:  docker run -p 8000:8000 --env-file .env paddyproof
#
# 필요한 환경변수는 .env.example 를 참고하세요.

FROM python:3.12-slim

# matplotlib(폰트), h5py(HDF5), pyproj(PROJ) 가 요구하는 시스템 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends \
        libhdf5-dev \
        libgeos-dev \
        proj-bin \
        fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY paddy_check.py nisar_source.py nisar_evidence.py nisar_compare.py ./
COPY web/ ./web/

# 데모 필지의 NISAR 관측을 미리 담아 둔다. 이게 없으면 첫 교차 검증이
# 원격 HDF5 를 읽느라 수 분 걸리고 작은 인스턴스에서는 메모리가 모자란다.
COPY seed/ ./seed/
RUN mkdir -p /app/output && cp -n seed/nisar_cache.json /app/output/ || true

ENV PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOST=0.0.0.0 \
    PORT=8000 \
    PADDY_RATE_PER_HOUR=20 \
    PADDY_MAX_PLOTS=50 \
    PADDY_MAX_CONCURRENT=2

# 한글 폰트가 없으면 그래프 라벨이 네모로 깨진다. 배포 후에 발견하면 늦으므로
# 빌드에서 잡는다. 겸사겸사 matplotlib 폰트 캐시도 미리 만들어 첫 요청을 앞당긴다.
RUN python -c "\
import sys, matplotlib; matplotlib.use('Agg'); \
from matplotlib import font_manager; \
names={f.name for f in font_manager.fontManager.ttflist}; \
ok=[n for n in ('NanumGothic','AppleGothic','Malgun Gothic') if n in names]; \
print('한글 폰트:', ok); \
sys.exit(0 if ok else '한글 폰트를 찾지 못했습니다. fonts-nanum 설치를 확인하세요.')"

EXPOSE 8000

# 포트를 고정하지 않는다. 배포 플랫폼은 PORT 환경변수로 포트를 지정하며,
# app.py 가 HOST/PORT 를 읽는다. 비밀번호(PADDY_WEB_PASSWORD)를 반드시 설정하세요.
CMD ["python", "web/app.py"]
