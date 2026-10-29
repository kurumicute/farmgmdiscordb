# 監控網站、介紹網站、Discord Bot 共用的 Linux 映像；由 docker-compose.yml 以不同指令啟動（見 web／site／bot 服務）
FROM python:3.13.18-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先只複製依賴清單，程式碼變動時不必重新安裝套件
COPY requirements.txt .
RUN pip install -r requirements.txt

# 以非 root 使用者執行；data/ 存音樂的伺服器設定（compose 掛載 Volume farmgm_data）
RUN useradd --create-home --uid 10001 farmgm && mkdir -p /app/data && chown farmgm:farmgm /app/data

# 啟動檢查會比對 Migration 版本，所以 alembic.ini 與 migrations/ 也要放進映像
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

USER farmgm
