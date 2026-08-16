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

RUN mkdir -p /app/output

ENV PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PADDY_RATE_PER_HOUR=20 \
    PADDY_MAX_PLOTS=50 \
    PADDY_MAX_CONCURRENT=2

EXPOSE 8000

# 컨테이너에서는 외부 접속을 받아야 하므로 0.0.0.0 에 바인딩한다.
# 비밀번호(PADDY_WEB_PASSWORD)를 반드시 설정하세요.
CMD ["python", "web/app.py", "--host", "0.0.0.0", "--port", "8000"]
