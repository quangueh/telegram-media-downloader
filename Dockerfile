FROM python:3.11-slim

# Thiết lập biến môi trường
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOAD_DIR=/app/downloads

# Cài đặt FFmpeg và các công cụ hỗ trợ cần thiết
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Thiết lập thư mục làm việc
WORKDIR /app

# Sao chép và cài đặt các phụ thuộc Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép toàn bộ mã nguồn vào container
COPY . .

# Tạo thư mục downloads để lưu trữ file tạm thời
RUN mkdir -p /app/downloads

# Lệnh khởi chạy ứng dụng unbuffered
CMD ["python", "-u", "bot.py"]
