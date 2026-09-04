# 🚀 Telegram Media Downloader Bot

Bot Telegram chuyên dụng tải video chất lượng cao, **không dính watermark / logo** từ các nền tảng phổ biến (**TikTok**, **Facebook**, **YouTube**) được xây dựng bằng Python, `yt-dlp` và `python-telegram-bot` v20+ (async/await).

---

## 🌟 Tính Năng Nổi Bật

- 🎵 **TikTok No-Watermark:** Tự động phát hiện và trích xuất luồng video gốc không logo / watermark từ TikTok.
- 📘 **Facebook HD:** Tải video Facebook với độ phân giải cao nhất (SD/HD).
- 📺 **YouTube Full Audio & Video:** Tự động hợp nhất video và audio chất lượng cao nhất bằng FFmpeg sang định dạng MP4 chuẩn.
- ⚡ **Xử lý Bất đồng bộ (Async/Await):** Không block bot event loop khi xử lý nhiều người dùng cùng lúc (`asyncio.to_thread`).
- 🛡️ **Kiểm soát dung lượng an toàn (Telegram 50MB Limit):** Bắt và xử lý lỗi tệp vượt quá 50MB rõ ràng, không gây crash bot.
- 🧹 **Tự động dọn dẹp (Storage Cleanup):** Khối `finally` đảm bảo xóa file tạm thời trên ổ cứng ngay sau khi gửi hoặc khi phát sinh lỗi.
- 🐳 **Dockerized:** Đã kèm `Dockerfile` và `docker-compose.yml` tối ưu hóa, sẵn sàng deploy lên Koyeb, VPS, Railway.

---

## 📁 Cấu Trúc Dự Án

```
telegram-media-downloader/
├── .github/workflows/    # CI/CD Pipeline (kiểm thử & tự động build Docker)
│   └── ci-cd.yml
├── render.yaml           # Cấu hình tự động triển khai lên Render.com (Blueprint)
├── koyeb.yaml            # Cấu hình triển khai tự động lên Koyeb PaaS
├── config.py             # Quản lý cấu hình, biến môi trường, dung lượng tối đa
├── downloader.py         # Module cốt lõi yt-dlp & FFmpeg xử lý trích xuất/tải
├── bot.py                # Điểm khởi chạy (Entrypoint), quản lý các handlers của Telegram
├── requirements.txt      # Danh sách thư viện Python cần thiết
├── Dockerfile            # Cấu hình container Python 3.11 + FFmpeg
├── docker-compose.yml    # File compose để khởi chạy nhanh trên local / server
├── .env.example          # Mẫu biến môi trường
├── .gitignore            # Bỏ qua file nhạy cảm và file tạm thời
└── README.md             # Hướng dẫn chi tiết
```

---

## 🔑 Hướng Dẫn Lấy Telegram Bot Token

1. Mở ứng dụng Telegram và tìm kiếm bot: **[@BotFather](https://t.me/BotFather)**.
2. Gửi lệnh `/newbot`.
3. Nhập **Tên hiển thị** cho Bot (ví dụ: `My Video Downloader`).
4. Nhập **Username** kết thúc bằng chữ `bot` (ví dụ: `my_video_dl_bot`).
5. Copy chuỗi token được cấp (dạng: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).

---

## 💻 Hướng Dẫn Chạy Trên Môi Trường Local

### Yêu Cầu Tiên Quyết
- **Python 3.10+** (khuyên dùng Python 3.11).
- **FFmpeg** đã được cài đặt và thêm vào PATH hệ thống:
  - **Ubuntu/Debian:** `sudo apt install ffmpeg`
  - **macOS:** `brew install ffmpeg`
  - **Windows:** Tải từ [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) hoặc dùng `winget install Gyan.FFmpeg`.

### Các Bước Cài Đặt

1. **Clone mã nguồn hoặc mở thư mục dự án:**
   ```bash
   cd telegram-media-downloader
   ```

2. **Tạo và kích hoạt môi trường ảo (Virtualenv):**
   ```bash
   # Linux/macOS
   python3 -m venv venv
   source venv/bin/activate

   # Windows (PowerShell)
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

3. **Cài đặt các gói phụ thuộc:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Thiết lập biến môi trường:**
   Tạo file `.env` từ `.env.example`:
   ```bash
   cp .env.example .env
   ```
   Mở file `.env` và điền token của bạn:
   ```env
   BOT_TOKEN=your_actual_bot_token_here
   LOG_LEVEL=INFO
   ```

5. **Khởi chạy bot:**
   ```bash
   python bot.py
   ```

---

## 🐳 Triển Khai Bằng Docker & Docker Compose

### Cách 1: Sử dụng Docker Compose (Khuyên dùng)

1. Điền token vào file `.env`:
   ```bash
   cp .env.example .env
   # Sửa BOT_TOKEN trong file .env
   ```

2. Khởi chạy container ngầm:
   ```bash
   docker compose up -d --build
   ```

3. Xem logs hoạt động:
   ```bash
   docker compose logs -f
   ```

4. Dừng container:
   ```bash
   docker compose down
   ```

### Cách 2: Sử dụng Docker thuần

```bash
# Build image
docker build -t telegram-media-downloader .

# Chạy container
docker run -d --name telegram-downloader \
  -e BOT_TOKEN="your_actual_bot_token_here" \
  telegram-media-downloader
```

---

## 🟣 Hướng Dẫn Triển Khai Lên Render.com (Khuyên Dùng)

Dự án đã được tối ưu hóa đặc biệt cho **Render.com** (tích hợp sẵn HTTP Health-Check server trong [bot.py](file:///d:/Tiktok_dowload/telegram-media-downloader/bot.py) để đáp ứng kiểm tra Port của Render và hỗ trợ gói **Free Tier**).

### Cách 1: Triển khai tự động bằng Blueprint (Nhanh nhất)
1. Đẩy mã nguồn dự án lên GitHub repository của bạn.
2. Đăng nhập [Render Dashboard](https://dashboard.render.com/).
3. Nhấn **New +** -> Chọn **Blueprint**.
4. Kết nối đến GitHub repository của bạn. Render sẽ tự động đọc file [render.yaml](file:///d:/Tiktok_dowload/telegram-media-downloader/render.yaml).
5. Render sẽ nhắc bạn nhập biến môi trường `BOT_TOKEN` -> Điền token bot của bạn.
6. Nhấn **Apply**. Render sẽ tự động build Docker image và khởi chạy bot.

### Cách 2: Tạo thủ công Web Service trên Render
1. Nhấn **New +** -> Chọn **Web Service**.
2. Kết nối với GitHub Repository của bạn.
3. Cấu hình cơ bản:
   - **Name:** `telegram-media-downloader`
   - **Region:** `Singapore` (hoặc khu vực gần bạn nhất)
   - **Language / Runtime:** Chọn **Docker** (Render sẽ dùng `Dockerfile` để cài FFmpeg)
   - **Instance Type:** Chọn gói **Free**
4. Tại mục **Environment Variables**, thêm:
   - `BOT_TOKEN`: `<Token_Telegram_Của_Bạn>`
   - `PYTHONUNBUFFERED`: `1`
5. Nhấn **Create Web Service**. 
6. *(Tùy chọn chống ngủ đông cho gói Free)*: Render Free Web Service sẽ ngủ sau 15 phút không có request. Bạn có thể copy URL của service (dạng `https://ten-bot.onrender.com`) và dán vào các dịch vụ ping miễn phí như [UptimeRobot](https://uptimerobot.com/) hoặc [Cron-job.org](https://cron-job.org/) (ping mỗi 5-10 phút vào endpoint `/`) để giữ bot hoạt động liên tục 24/7!

---

## ☁️ Hướng Dẫn Triển Khai Lên Cloud PaaS (Koyeb)

[Koyeb](https://www.koyeb.com/) là nền tảng đám mây hiện đại hỗ trợ chạy Dockerfile trực tiếp từ GitHub rất thuận tiện:

1. **Đẩy mã nguồn lên GitHub repository** của bạn.
2. Đăng nhập vào **Koyeb Console** -> Chọn **Create App**.
3. Chọn nguồn triển khai: **GitHub**.
4. Chọn repository của bạn.
5. Tại mục **Builder**, chọn **Dockerfile** (Koyeb sẽ tự động nhận diện `Dockerfile`).
6. Tại mục **Environment Variables**, thêm biến:
   - Key: `BOT_TOKEN`
   - Value: `<Token_Telegram_Của_Bạn>`
7. Tại mục **Instance type**, chọn cấu hình mong muốn (ví dụ gói Free Nano hoặc Micro).
8. Nhấn **Deploy**. Koyeb sẽ tự động build image có chứa FFmpeg và chạy bot của bạn 24/7!

> **Gợi ý:** Bạn cũng có thể dùng file cấu hình [koyeb.yaml](file:///d:/Tiktok_dowload/telegram-media-downloader/koyeb.yaml) đã chuẩn bị sẵn để deploy qua Koyeb CLI:
> ```bash
> koyeb service create --app telegram-media-downloader --name bot --instance-type nano
> ```

---

## 🔄 CI/CD Pipeline (GitHub Actions)

Dự án đã tích hợp sẵn workflow tại [.github/workflows/ci-cd.yml](file:///d:/Tiktok_dowload/telegram-media-downloader/.github/workflows/ci-cd.yml):
- **Tự động chạy Unit Tests** trên mỗi lần `push` hoặc `pull_request`.
- **Tự động kiểm tra Docker Build** để đảm bảo container image luôn build thành công.
- **Tự động kích hoạt deploy** nếu có token `KOYEB_TOKEN`.

## ❓ Câu Hỏi Thường Gặp (FAQ) & Xử Lý Sự Cố

- **Hỏi: Tại sao video YouTube dài hơn 20 phút không gửi được?**
  - **Đáp:** Telegram Bot API giới hạn các bot thông thường chỉ được gửi tệp tối đa 50MB. Video quá dài có dung lượng > 50MB sẽ được bot tự động thông báo lỗi mà không làm sập bot.
- **Hỏi: Video TikTok có bị dính ID hay logo mờ không?**
  - **Đáp:** Không. Bộ trích xuất của `yt-dlp` tự động gọi API trực tiếp đến máy chủ CDN của TikTok để lấy luồng MP4 nguyên bản không logo.
- **Hỏi: Ổ cứng máy chủ có bị đầy sau nhiều lượt tải không?**
  - **Đáp:** Không. Tất cả các file video sau khi gửi (hoặc khi gặp lỗi) đều được dọn dẹp bằng hàm `os.remove()` trong khối `finally`.
