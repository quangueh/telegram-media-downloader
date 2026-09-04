#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════════
#  start.sh — Khởi động bgutil PO-token server (background) → Python bot
# ═══════════════════════════════════════════════════════════════════════════════
#
# bgutil server (https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
# listens on http://127.0.0.1:4416 — yt-dlp plugin auto-detects it.
# Generates PO tokens via BotGuard to bypass "Sign in to confirm you're not a bot"
# on YouTube when running on datacenter IPs (Render, AWS, etc.)
#
set -e

echo "══════════════════════════════════════════════════════════════════"
echo " Starting bgutil PO-token server on port ${BGUTIL_PORT:-4416}..."
echo "══════════════════════════════════════════════════════════════════"

# Khởi chạy Node.js server ở background
cd /opt/bgutil/server
node build/main.js --port ${BGUTIL_PORT:-4416} &
BGUTIL_PID=$!
echo "bgutil PID: $BGUTIL_PID"

# Đợi server sẵn sàng (kiểm tra TCP port mỗi 0.5s, tối đa 30s)
PORT="${BGUTIL_PORT:-4416}"
echo "Đợi bgutil server lắng nghe trên port ${PORT}..."
for i in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${PORT}/ping" >/dev/null 2>&1; then
        echo "✓ bgutil server sẵn sàng! (sau ${i} lần kiểm tra)"
        break
    fi
    sleep 0.5
done

# Kiểm tra xem bgutil server có còn chạy không
if ! kill -0 $BGUTIL_PID 2>/dev/null; then
    echo "⚠ WARNING: bgutil server process đã thoát. Kiểm tra log để biết lỗi."
else
    echo "✓ bgutil server đang chạy."
fi

echo "══════════════════════════════════════════════════════════════════"
echo " Starting Telegram Bot (Python)..."
echo "══════════════════════════════════════════════════════════════════"

# Chạy Python bot
exec python -u bot.py
