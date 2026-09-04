FROM python:3.11-slim

# ═══════════════════════════════════════════════════════════════════════════════
#  ENV
# ═══════════════════════════════════════════════════════════════════════════════
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOAD_DIR=/app/downloads \
    BGUTIL_PORT=4416

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1: System deps — FFmpeg + Node.js 20
# ═══════════════════════════════════════════════════════════════════════════════
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    git \
    # Node.js 20 LTS — cần cho bgutil PO-token server
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2: bgutil-ytdlp-pot-provider (clone + build)
# ═══════════════════════════════════════════════════════════════════════════════
# Source: https://github.com/Brainicism/bgutil-ytdlp-pot-provider
# Server listens on http://127.0.0.1:4416 — yt-dlp plugin auto-detects it
RUN git clone --single-branch --branch 1.3.2 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3: Python deps
# ═══════════════════════════════════════════════════════════════════════════════
WORKDIR /app

# BUILD_DATE tạo layer mới mỗi lần build — đảm bảo yt-dlp + bgutil plugin luôn mới nhất
ARG BUILD_DATE
LABEL build_date=$BUILD_DATE

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --upgrade -r requirements.txt \
    && pip install --no-cache-dir --upgrade yt-dlp \
    && pip install --no-cache-dir --upgrade bgutil-ytdlp-pot-provider

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 4: Copy source
# ═══════════════════════════════════════════════════════════════════════════════
COPY . .

RUN mkdir -p /app/downloads

# Đảm bảo start.sh executable
RUN chmod +x start.sh

# ═══════════════════════════════════════════════════════════════════════════════
#  CMD: Khởi chạy bgutil server (background) → python bot
# ═══════════════════════════════════════════════════════════════════════════════
CMD ["./start.sh"]
