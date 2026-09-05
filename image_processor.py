"""
Module xử lý ảnh — làm nét & làm đẹp bằng Pillow (nhẹ, không cần PyTorch).

Chức năng:
  - enhance_image(): làm nét — sharpen + denoise + saturation boost
  - beautify_image(): làm đẹp — mịn da + sáng + ấm + hồng

Yêu cầu: Pillow>=10.0, numpy>=1.24
"""

from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageFilter, ImageEnhance


# Kích thước tối đa output (giữ RAM thấp trên Render free tier)
MAX_DIMENSION = 4096
JPEG_QUALITY = 95

# Kiểu callback: on_step(pct: float, text: str)
StepCB = Optional[Callable[[float, str], None]]


def _resize_if_needed(img: Image.Image) -> Image.Image:
    """Resize ảnh nếu lớn hơn MAX_DIMENSION (giữ tỷ lệ)."""
    w, h = img.size
    if max(w, h) <= MAX_DIMENSION:
        return img
    ratio = MAX_DIMENSION / max(w, h)
    new_size = (int(w * ratio), int(h * ratio))
    return img.resize(new_size, Image.LANCZOS)


def _add_pink_tone(img: Image.Image, amount: int = 8) -> Image.Image:
    """Thêm tông hồng/ấm nhẹ bằng cách điều chỉnh kênh RGB."""
    arr = np.array(img, dtype=np.int16)
    # R +amount, G +amount//2, B -amount → tông hồng ấm
    arr[:, :, 0] = np.clip(arr[:, :, 0] + amount, 0, 255)
    arr[:, :, 1] = np.clip(arr[:, :, 1] + amount // 2, 0, 255)
    arr[:, :, 2] = np.clip(arr[:, :, 2] - amount, 0, 255)
    return Image.fromarray(arr.astype(np.uint8))


def enhance_image(input_path: str, output_path: str, on_step: StepCB = None) -> str:
    """
    Làm nét ảnh:
    1. Denoise nhẹ (MedianFilter)
    2. Sharpen mạnh (UnsharpMask)
    3. Tăng saturation +30%
    4. Tăng contrast +10%
    Trả về đường dẫn file output. on_step(pct, text) — tùy chọn.
    """
    def report(pct: float, text: str) -> None:
        if on_step:
            try:
                on_step(pct, text)
            except Exception:
                pass

    report(5, "📥 Đang đọc ảnh...")
    img = Image.open(input_path).convert("RGB")
    img = _resize_if_needed(img)

    # Bước 1: Denoise nhẹ — loại bỏ noise trước khi sharpen
    report(25, "🧹 Đang khử nhiễu...")
    img = img.filter(ImageFilter.MedianFilter(size=3))

    # Bước 2: Sharpen mạnh
    report(50, "🔪 Đang làm nét...")
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))

    # Bước 3: Tăng saturation (màu sắc sống động hơn)
    report(70, "🎨 Đang tăng màu sắc...")
    img = ImageEnhance.Color(img).enhance(1.3)

    # Bước 4: Tăng contrast nhẹ
    report(85, "🌗 Đang tăng tương phản...")
    img = ImageEnhance.Contrast(img).enhance(1.1)

    # Lưu output
    report(95, "💾 Đang lưu...")
    img.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return output_path


def beautify_image(input_path: str, output_path: str, on_step: StepCB = None) -> str:
    """
    Làm đẹp ảnh:
    1. Mịn da nhẹ (GaussianBlur)
    2. Sáng hơn +15%
    3. Warmth +20% (saturation)
    4. Contrast +10%
    5. Thêm tông hồng nhẹ
    Trả về đường dẫn file output. on_step(pct, text) — tùy chọn.
    """
    def report(pct: float, text: str) -> None:
        if on_step:
            try:
                on_step(pct, text)
            except Exception:
                pass

    report(5, "📥 Đang đọc ảnh...")
    img = Image.open(input_path).convert("RGB")
    img = _resize_if_needed(img)

    # Bước 1: Mịn da nhẹ — GaussianBlur radius=1 giữ chi tiết
    report(20, "✨ Đang mịn da...")
    img = img.filter(ImageFilter.GaussianBlur(radius=1))

    # Bước 2: Sáng hơn
    report(40, "☀️ Đang tăng sáng...")
    img = ImageEnhance.Brightness(img).enhance(1.15)

    # Bước 3: Tăng warmth (saturation)
    report(55, "🎨 Đang tăng độ ấm...")
    img = ImageEnhance.Color(img).enhance(1.2)

    # Bước 4: Contrast nhẹ
    report(70, "🌗 Đang tăng tương phản...")
    img = ImageEnhance.Contrast(img).enhance(1.1)

    # Bước 5: Thêm tông hồng ấm
    report(85, "🌸 Đang thêm tông hồng...")
    img = _add_pink_tone(img, amount=8)

    # Lưu output
    report(95, "💾 Đang lưu...")
    img.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return output_path
