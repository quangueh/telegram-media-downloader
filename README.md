# 🚀 Telegram Media Downloader Bot

Bot Telegram chuyên dụng tải video chất lượng cao từ các nền tảng phổ biến (**TikTok**, **Facebook**, **YouTube**, **Douyin**) được xây dựng bằng Python, `yt-dlp` và `python-telegram-bot` v22+ (async/await).

---

## 🌟 Tính Năng Nổi Bật

- 🎵 **TikTok:** Tự động phát hiện link video/photo post và chọn nguồn stream phù hợp; khả năng bỏ watermark phụ thuộc nguồn gốc.
- 🇨🇳 **Douyin:** Hỗ trợ link Douyin (v.douyin.com / www.douyin.com) qua yt-dlp.
- 📘 **Facebook HD:** Tải video Facebook với độ phân giải cao nhất (SD/HD).
- 📺 **YouTube Full Audio & Video:** Tự động hợp nhất video và audio chất lượng cao nhất bằng FFmpeg sang định dạng MP4 chuẩn (H.264 + AAC, phát được mọi thiết bị).
- ⚡ **Xử lý bất đồng bộ:** HTTP, FFmpeg, xử lý ảnh và yt-dlp không chặn event loop; có giới hạn số job đồng thời và rate-limit Telegram.
- 🛡️ **An toàn URL:** Chặn SSRF, mạng nội bộ, metadata endpoint, URL không hợp lệ và kiểm tra lại từng redirect.
- 📦 **Giới hạn tài nguyên:** Giới hạn dung lượng video/ảnh/audio, số ảnh album, thời lượng video, pixel ảnh và kích thước input.
- 🧹 **Dọn dẹp theo job:** Mỗi request dùng thư mục tạm riêng, tự xóa cả file, cookie tạm và thư mục con khi thành công hoặc lỗi.
- 🎞️ **Tương thích phát:** FFmpeg chuẩn hóa H.264/AAC, xử lý tiến trình an toàn và chọn stream tốt nhất còn vừa giới hạn.
- 🩺 **Vận hành sẵn sàng:** Health-check liveness/readiness, Docker chạy non-root, có resource limit và CI lint/test/build.

---

## 📁 Cấu Trúc Dự Án

```
telegram-media-downloader/
├── .github/workflows/    # CI/CD Pipeline (kiểm thử & tự động build Docker)
│   └── ci-cd.yml
├── render.yaml           # Cấu hình tự động triển khai lên Render.com (Blueprint)
├── koyeb.yaml            # Cấu hình triển khai tự động lên Koyeb PaaS
├── config.py             # Cấu hình, giới hạn tài nguyên và biến môi trường
├── security.py           # Validate URL công khai, chặn SSRF và redacts URL
├── downloader.py         # Router yt-dlp/API/FFmpeg, job temp và giới hạn media
├── image_processor.py    # Xử lý ảnh an toàn theo pixel/dimension
├── tools.py              # QR, sticker, GIF, meme, nén ảnh, màu, thumbnail
├── progress.py           # Progress message Telegram có throttle
├── bot.py                # Entrypoint, handlers, limiter, health-check
├── tests/                # Unit test cho security, downloader và media tools
├── requirements.txt      # Dependency runtime có giới hạn phiên bản
├── pyproject.toml        # Cấu hình Ruff
├── Dockerfile            # Container non-root + FFmpeg + healthcheck
├── docker-compose.yml    # Chạy local với resource limit
├── .env.example          # Mẫu biến môi trường
├── .gitignore            # Bỏ qua secret, cache và file tạm
└── README.md             # Hướng dẫn vận hành
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

5. *(Tùy chọn — khi IP server bị YouTube chặn)* Ưu tiên proxy sạch/residential hoặc PO-token provider; không gửi secret vào log hoặc commit:
   ```env
   YOUTUBE_PROXY=http://user:password@proxy-host:port
   YOUTUBE_POT_PROVIDER_URL=http://provider-host:4416
   ```
   `YOUTUBE_COOKIES`/`YOUTUBE_COOKIES_B64` vẫn được hỗ trợ cho trường hợp tự chịu rủi ro, nhưng không nên dùng cookie tài khoản Google chính vì YouTube có thể khóa tài khoản. `TIKTOK_COOKIES` dùng cho cookie TikTok.


6. **Khởi chạy bot:**
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

Dự án đã được tối ưu hóa đặc biệt cho **Render.com** (tích hợp HTTP health-check trong `bot.py` để đáp ứng kiểm tra port và hỗ trợ gói Free Tier).

### Cách 1: Triển khai tự động bằng Blueprint (Nhanh nhất)
1. Đẩy mã nguồn dự án lên GitHub repository của bạn.
2. Đăng nhập [Render Dashboard](https://dashboard.render.com/).
3. Nhấn **New +** -> Chọn **Blueprint**.
4. Kết nối đến GitHub repository của bạn. Render sẽ tự động đọc file `render.yaml`.
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

> **Gợi ý:** Bạn cũng có thể dùng file cấu hình `koyeb.yaml` đã chuẩn bị sẵn để deploy qua Koyeb CLI:
> ```bash
> koyeb service create --app telegram-media-downloader --name bot --instance-type nano
> ```

---

## 🔄 CI/CD Pipeline (GitHub Actions)

Dự án đã tích hợp sẵn workflow tại `.github/workflows/ci-cd.yml`:
- **Tự động chạy compile, Ruff lint và Unit Tests** trên mỗi lần `push` hoặc `pull_request`.
- **Tự động kiểm tra Docker Build** để đảm bảo container image luôn build thành công.
- **Không tự động chạy lệnh deploy có side effect**; triển khai qua cấu hình Render/Koyeb hoặc pipeline riêng.

## ❓ Câu Hỏi Thường Gặp (FAQ) & Xử Lý Sự Cố

- **Hỏi: Tại sao video YouTube dài hơn 20 phút không gửi được?**
  - **Đáp:** Telegram Bot API giới hạn các bot thông thường chỉ được gửi tệp tối đa 50MB. Video quá dài có dung lượng > 50MB sẽ được bot tự động thông báo lỗi mà không làm sập bot.
- **Hỏi: Video TikTok có bị dính ID hay logo mờ không?**
  - **Đáp:** Tùy nguồn phát, thuật toán có thể không lấy được bản không watermark. Bot không cam kết xóa mọi watermark của nền tảng.
- **Hỏi: Ổ cứng máy chủ có bị đầy sau nhiều lượt tải không?**
  - **Đáp:** Mỗi job dùng thư mục tạm riêng và được dọn dẹp trong `finally`; Docker local còn giới hạn không gian tạm và số tiến trình.
- **Hỏi: Có thể dùng URL nội bộ để test không?**
  - **Đáp:** Mặc định bị chặn để chống SSRF. Chỉ bật `ALLOW_PRIVATE_URLS=true` trong môi trường test nội bộ, không dùng trên production.
