FROM python:3.11-slim

# ═══════════════════════════════════════════════════════════════════════════════
#  ENV
# ═══════════════════════════════════════════════════════════════════════════════
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOAD_DIR=/app/downloads \
    BGUTIL_PORT=4416

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1: FFmpeg + Node.js 22 (binary — tiết kiệm ~200MB so với apt)
# ═══════════════════════════════════════════════════════════════════════════════
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 LTS — cài trực tiếp binary (không cần nodesource repo)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then NODE_ARCH="arm64"; else NODE_ARCH="x64"; fi && \
    curl -fsSL "https://nodejs.org/dist/v22.18.0/node-v22.18.0-linux-${NODE_ARCH}.tar.gz" \
    | tar -xzf - -C /usr/local --strip-components=1

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2: bgutil server (clone + build)
# ═══════════════════════════════════════════════════════════════════════════════
RUN git clone --single-branch --branch 1.3.2 --depth 1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc \
    # Xóa node_modules/.cache + dev deps để giảm kích thước image
    && rm -rf /opt/bgutil/server/node_modules/.cache \
             /opt/bgutil/.git

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3: Python deps
# ═══════════════════════════════════════════════════════════════════════════════
WORKDIR /app

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

# ═══════════════════════════════════════════════════════════════════════════════
#  CMD: bgutil server (background, --max-old-space-size=64) → python bot
# ═══════════════════════════════════════════════════════════════════════════════
CMD ["./start.sh"]
