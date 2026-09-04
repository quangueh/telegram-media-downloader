FROM python:3.11-slim

# ═══════════════════════════════════════════════════════════════════════════════
#  ENV
# ═══════════════════════════════════════════════════════════════════════════════
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOAD_DIR=/app/downloads

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1: System deps — FFmpeg only (không cần Node.js trên free tier 512MB)
# ═══════════════════════════════════════════════════════════════════════════════
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2: Python deps
# ═══════════════════════════════════════════════════════════════════════════════
WORKDIR /app

# BUILD_DATE tạo layer mới mỗi lần build — đảm bảo yt-dlp luôn mới nhất
ARG BUILD_DATE
LABEL build_date=$BUILD_DATE

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --upgrade -r requirements.txt \
    && pip install --no-cache-dir --upgrade yt-dlp

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3: Copy source
# ═══════════════════════════════════════════════════════════════════════════════
COPY . .

RUN mkdir -p /app/downloads

# ═══════════════════════════════════════════════════════════════════════════════
#  CMD: Chạy Python bot trực tiếp (không cần start.sh / bgutil server)
# ═══════════════════════════════════════════════════════════════════════════════
CMD ["python", "-u", "bot.py"]
