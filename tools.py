"""
Module tools — 10 utility tools cho Telegram Bot.

Tools:
  1. generate_qr()          — Tạo QR code từ text/URL
  2. make_sticker()         — Ảnh → sticker Telegram 512x512
  3. video_to_gif()         — Video clip → GIF (FFmpeg)
  4. add_meme_text()        — Thêm text meme lên ảnh
  5. compress_image()       — Nén ảnh giảm dung lượng
  6. extract_colors()      — Trích xuất bảng màu từ ảnh
  7. add_watermark()        — Thêm watermark text lên ảnh
  8. get_youtube_thumbnail()— Tải thumbnail YouTube
  9. roll_dice()            — Random dice (pure logic, không file)
 10. image_to_ascii()       — Chuyển ảnh thành ASCII art

Yêu cầu: Pillow, NumPy, httpx, qrcode, FFmpeg (system)
"""

import random
import re
import subprocess
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
JPEG_QUALITY = 95
MAX_DIMENSION = 4096

# Font bold cho meme text — thử nhiều path (DejaVu có sẵn trong Debian slim)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",   # Windows (dev machine)
    "C:/Windows/Fonts/impact.ttf",
]


def _load_bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load font bold, fallback về default font nếu không tìm thấy."""
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 1: QR CODE
# ═══════════════════════════════════════════════════════════════════════════════
def generate_qr(text: str, output_path: str) -> str:
    """Tạo QR code từ text/URL, trả về đường dẫn file PNG."""
    import qrcode

    qr = qrcode.QRCode(
        version=None,  # Auto-fit độ lớn theo data
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(output_path)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 2: STICKER MAKER
# ═══════════════════════════════════════════════════════════════════════════════
def make_sticker(input_path: str, output_path: str) -> str:
    """
    Chuyển ảnh thành sticker Telegram chuẩn 512x512 PNG.
    - Resize giữ tỷ lệ, fit vào 512x512
    - Không crop — thêm padding trong suốt
    """
    img = Image.open(input_path).convert("RGBA")
    img.thumbnail((512, 512), Image.LANCZOS)

    # Canvas trong suốt 512x512, ảnh căn giữa
    canvas = Image.new("RGBA", (512, 512), (255, 255, 255, 0))
    offset = ((512 - img.width) // 2, (512 - img.height) // 2)
    canvas.paste(img, offset)

    canvas.save(output_path, "PNG")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 3: VIDEO → GIF
# ═══════════════════════════════════════════════════════════════════════════════
def video_to_gif(
    input_path: str,
    output_path: str,
    start: float = 0.0,
    duration: float = 5.0,
    on_progress=None,
) -> str:
    """
    Chuyển video → GIF bằng FFmpeg.
    - Mặc định lấy 5 giây đầu
    - 12fps, scale 480px width, palette-gen để GIF đẹp màu
    - on_progress(pct, text) — optional, đọc từ -progress pipe
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", input_path,
        "-vf",
        "fps=12,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
        "-progress", "pipe:1", "-nostats",
        "-loglevel", "error",
        output_path,
    ]

    def report(pct, text):
        if on_progress:
            try:
                on_progress(pct, text)
            except Exception:
                pass

    report(2, "🎞️ Đang khởi động FFmpeg...")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    last_report_time = 0.0
    try:
        # Đọc -progress pipe: các dòng key=value (out_time_us, out_time_ms, speed...)
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    out_us = int(line.split("=", 1)[1])
                    pct = min(99.0, out_us / (duration * 1_000_000) * 100)
                    # Throttle: chỉ report khi >= 3% tiến bộ
                    now = time.monotonic()
                    if now - last_report_time >= 2.0:
                        last_report_time = now
                        report(pct, f"🎞️ Đang chuyển: {pct:.0f}% ({duration}s)")
                except ValueError:
                    pass
        proc.wait(timeout=30)
    except Exception:
        proc.kill()
        raise

    if proc.returncode != 0:
        stderr = proc.stderr.read()[-300:] if proc.stderr else "unknown"
        raise RuntimeError(f"FFmpeg lỗi: {stderr}")

    report(100, "✅ Hoàn tất GIF")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 4: MEME GENERATOR
# ═════════════════════════════════════════════════════════════════════════════════
def add_meme_text(
    input_path: str,
    output_path: str,
    top_text: str = "",
    bottom_text: str = "",
) -> str:
    """
    Thêm text meme style (trắng + viền đen, UPPERCASE) lên ảnh.
    Text tự wrap nếu quá dài.
    """
    img = Image.open(input_path).convert("RGB")
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    draw = ImageDraw.Draw(img)
    font_size = max(20, img.width // 12)
    font = _load_bold_font(font_size)

    def draw_text_block(text: str, y_start: int, from_bottom: bool = False) -> int:
        """Vẽ block text wrap theo width, trả về tổng height đã vẽ."""
        if not text:
            return 0
        text = text.upper()

        # Wrap text theo từng từ
        max_text_width = img.width - 20
        lines, current = [], ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if bbox[2] - bbox[0] <= max_text_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)

        # Tính tổng height
        line_height = font_size + 8
        total_h = line_height * len(lines)

        y = y_start if not from_bottom else y_start - total_h

        # Vẽ từng line: viền đen + chữ trắng
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            x = (img.width - (bbox[2] - bbox[0])) // 2
            stroke_w = max(2, font_size // 15)
            draw.text(
                (x, y), line, font=font,
                fill="white", stroke_width=stroke_w, stroke_fill="black",
            )
            y += line_height
        return total_h

    draw_text_block(top_text, 10, from_bottom=False)
    if bottom_text:
        bbox = draw.textbbox((0, 0), "Ag", font=font)
        line_h = font_size + 8
        draw_text_block(bottom_text, img.height - 10 - line_h, from_bottom=True)

    img.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 5: IMAGE COMPRESSOR
# ═══════════════════════════════════════════════════════════════════════════════
def compress_image(
    input_path: str,
    output_path: str,
    quality: int = 60,
    max_width: int = 1280,
) -> tuple[str, int, int]:
    """
    Nén ảnh: resize max width + JPEG quality thấp.
    Trả về (output_path, size_before, size_after) bytes.
    """
    img = Image.open(input_path).convert("RGB")

    # Resize nếu rộng hơn max_width
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

    img.save(output_path, "JPEG", quality=quality, optimize=True)

    size_before = Path(input_path).stat().st_size
    size_after = Path(output_path).stat().st_size
    return output_path, size_before, size_after


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 6: COLOR PALETTE EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════
def extract_colors(input_path: str, num_colors: int = 6) -> list[dict]:
    """
    Trích xuất N màu chủ đạo từ ảnh.
    Trả về list dict: [{"hex": "#RRGGBB", "percent": 32.5}, ...]
    """
    img = Image.open(input_path).convert("RGB")
    img.thumbnail((200, 200))  # Giảm sample size cho nhanh

    # Quantize về num_colors palette
    quantized = img.quantize(colors=num_colors)
    palette = quantized.getpalette()
    counts = sorted(quantized.getcolors(maxcolors=num_colors * 1000), reverse=True)

    total_pixels = sum(c for c, _ in counts) or 1
    colors = []
    for count, _ in counts[:num_colors]:
        idx = _ * 3
        r, g, b = palette[idx], palette[idx + 1], palette[idx + 2]
        colors.append({
            "hex": f"#{r:02X}{g:02X}{b:02X}",
            "percent": round(count / total_pixels * 100, 1),
        })
    return colors


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 7: WATERMARK
# ═══════════════════════════════════════════════════════════════════════════════
def add_watermark(
    input_path: str,
    output_path: str,
    text: str = "@MyBot",
) -> str:
    """Thêm watermark text semi-transparent ở góc dưới phải."""
    img = Image.open(input_path).convert("RGBA")
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    # Font size theo kích thước ảnh (2.5% width, min 14)
    font_size = max(14, img.width // 40)
    font = _load_bold_font(font_size)

    # Tạo layer watermark trong suốt
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    # Vị trí: góc dưới phải, padding 3% kích thước ảnh
    padding = max(10, img.width // 30)
    x = img.width - text_w - padding
    y = img.height - text_h - padding

    # Text trắng mờ 50% với viền đen mờ
    draw.text(
        (x, y), text, font=font,
        fill=(255, 255, 255, 128),
        stroke_width=max(1, font_size // 10),
        stroke_fill=(0, 0, 0, 128),
    )

    # Composite: ảnh gốc + watermark layer
    result = Image.alpha_composite(img, layer)
    result = result.convert("RGB")
    result.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return output_path


# ══════════════════════════════════════════════════════════════════════════════════
#  TOOL 8: YOUTUBE THUMBNAIL
# ══════════════════════════════════════════════════════════════════════════════════
def get_youtube_thumbnail(video_id: str, output_path: str) -> str:
    """
    Tải thumbnail YouTube chất lượng cao nhất có thể.
    Thử maxresdefault (1280x720) → fallback hqdefault (480x360).
    """
    urls = [
        f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    ]
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = client.get(url)
                # Thumbnail thật ≥ 2KB; YouTube trả placeholder 1KB khi không có
                if resp.status_code == 200 and len(resp.content) > 2000:
                    Path(output_path).write_bytes(resp.content)
                    return output_path
            except httpx.HTTPError:
                continue
    raise ValueError(f"Không tải được thumbnail cho video ID: {video_id}")


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 9: DICE ROLL
# ═══════════════════════════════════════════════════════════════════════════════
def roll_dice(expression: str = "1d6") -> str:
    """
    Roll dice theo cú pháp NdM (VD: 2d6, 1d20, 3d4).
    Trả về chuỗi kết quả đã format.
    """
    expr = expression.strip().lower().replace(" ", "")

    # Parse NdM hoặc N hoặc M (mặc định 1d6)
    match = re.match(r"^(\d*)d(\d+)$", expr)
    if match:
        count = int(match.group(1) or 1)
        sides = int(match.group(2))
    elif expr.isdigit():
        count, sides = 1, int(expr)
    else:
        count, sides = 1, 6

    # Giới hạn chống spam
    count = max(1, min(count, 20))
    sides = max(2, min(sides, 1000))

    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls)

    if count == 1:
        return f"🎲 {count}d{sides} → <b>{total}</b>"
    roll_str = " + ".join(str(r) for r in rolls)
    return f"🎲 {count}d{sides} → {roll_str} = <b>{total}</b>"


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOL 10: ASCII ART
# ═══════════════════════════════════════════════════════════════════════════════
ASCII_CHARS = " .:-=+*#%@"


def image_to_ascii(input_path: str, width: int = 60) -> str:
    """
    Chuyển ảnh thành ASCII art.
    - Grayscale → map brightness → ASCII chars
    - width 60 chars (Telegram fit trong code block)
    """
    img = Image.open(input_path).convert("L")

    # Char cao gấp ~2x rộng → giảm height tương ứng
    aspect = img.height / img.width
    height = max(1, int(width * aspect * 0.55))
    img = img.resize((width, height))

    pixels = list(img.getdata())
    lines = []
    for row in range(height):
        line_chars = []
        for col in range(width):
            brightness = pixels[row * width + col]
            idx = brightness * (len(ASCII_CHARS) - 1) // 255
            line_chars.append(ASCII_CHARS[idx])
        lines.append("".join(line_chars))

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  EXTRACT YOUTUBE ID — helper cho /thumb
# ═══════════════════════════════════════════════════════════════════════════════

_YT_ID_PATTERNS = [
    r"(?:v=|/v/|/embed/|/shorts/)([A-Za-z0-9_-]{11})",
    r"^([A-Za-z0-9_-]{11})$",
    r"youtu\.be/([A-Za-z0-9_-]{11})",
]


def extract_youtube_id(url_or_id: str) -> str | None:
    """Trích xuất video ID 11 ký tự từ URL YouTube hoặc ID trực tiếp."""
    for pattern in _YT_ID_PATTERNS:
        match = re.search(pattern, url_or_id.strip())
        if match:
            return match.group(1)
    return None
