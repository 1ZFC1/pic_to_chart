"""图片读取、重采样、掩膜与取色。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


class ImageError(RuntimeError):
    """图片无法读取或参数不合法。"""


def load_image(path: str | Path, max_size: int | None = None) -> Image.Image:
    """读取图片并统一成 RGBA；``max_size`` 限制最长边。"""
    path = Path(path)
    if not path.exists():
        raise ImageError(f"图片不存在：{path}")
    try:
        image = Image.open(path)
        image.load()
    except OSError as exc:  # 损坏 / 格式不支持
        raise ImageError(f"无法读取图片：{path}（{exc}）") from exc

    if image.mode != "RGBA":
        image = image.convert("RGBA")
    if max_size and max(image.size) > max_size:
        scale = max_size / max(image.size)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(size, Image.LANCZOS)
    return image


def as_array(image: Image.Image) -> np.ndarray:
    """RGBA 图片 → ``(h, w, 4)`` uint8 数组。"""
    return np.asarray(image.convert("RGBA"), dtype=np.uint8)


def resample_array(image: Image.Image, cols: int, rows: int) -> np.ndarray:
    """把图片重采样成 ``cols × rows`` 的 RGBA 数组（面积平均，避免锯齿）。"""
    if cols <= 0 or rows <= 0:
        raise ImageError("重采样尺寸必须为正")
    if image.width == cols and image.height == rows:
        return as_array(image)
    return np.asarray(image.resize((cols, rows), Image.BOX), dtype=np.uint8)


def has_alpha_channel(image: Image.Image) -> bool:
    """图片是否存在真正的透明区域。"""
    return int(as_array(image)[..., 3].min()) < 250


def luma(arr: np.ndarray) -> np.ndarray:
    """0-255 的亮度。"""
    rgb = arr[..., :3].astype(np.float32)
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def build_mask(
    arr: np.ndarray,
    *,
    alpha_cut: int = 8,
    ink_threshold: float | None = None,
    invert: bool = False,
) -> np.ndarray:
    """生成“需要绘制”的布尔掩膜。

    * 图片有真正的透明区域时，按 alpha 判断（透明 = 不画）；
    * 图片完全不透明时，按亮度判断，阈值不给就用 Otsu 自动求
      （白底线稿、黑底白字都能用 ``--invert`` 反过来）。
    * 两者都给了阈值时取交集。
    """
    alpha = arr[..., 3]
    has_alpha = int(alpha.min()) < 250
    if ink_threshold is None and has_alpha:
        return alpha > alpha_cut

    bright = luma(arr)
    if ink_threshold is None:
        if not has_alpha:
            from skimage.filters import threshold_otsu

            threshold = float(threshold_otsu(bright))
        else:
            threshold = float(bright.mean())
    else:
        threshold = float(ink_threshold)

    mask = bright > threshold if invert else bright < threshold
    if has_alpha:
        mask = mask & (alpha > alpha_cut)
    if not mask.any():  # 阈值把整张图判空时退一步，避免输出空谱面
        mask = (bright > threshold) if not invert else (bright < threshold)
    return mask


def sample_rgba(arr: np.ndarray, px: float, py: float) -> list[int]:
    """取最近像素的 RGBA（越界时返回完全透明）。"""
    h, w = arr.shape[:2]
    x = int(round(px))
    y = int(round(py))
    if x < 0 or y < 0 or x >= w or y >= h:
        return [0, 0, 0, 0]
    return [int(v) for v in arr[y, x]]


def average_rgba(arr: np.ndarray, px: float, py: float, radius: int = 1) -> list[int]:
    """在 ``radius`` 邻域内对不透明像素取平均色；没有不透明像素时返回透明。"""
    h, w = arr.shape[:2]
    x = int(round(px))
    y = int(round(py))
    x0, x1 = max(0, x - radius), min(w, x + radius + 1)
    y0, y1 = max(0, y - radius), min(h, y + radius + 1)
    if x0 >= x1 or y0 >= y1:
        return [0, 0, 0, 0]

    patch = arr[y0:y1, x0:x1].reshape(-1, 4).astype(np.float32)
    weights = patch[:, 3]
    total = float(weights.sum())
    if total <= 0:
        return [0, 0, 0, 0]
    rgb = (patch[:, :3] * weights[:, None]).sum(axis=0) / total
    alpha = weights.mean()
    return [int(round(rgb[0])), int(round(rgb[1])), int(round(rgb[2])), int(round(alpha))]


def inner_stroke_color(arr: np.ndarray, mask: np.ndarray, px: float, py: float,
                       radius: int = 2) -> list[int]:
    """给轮廓描边取色：在邻域内只统计掩膜内部（墨迹）像素。"""
    h, w = arr.shape[:2]
    x = int(round(px))
    y = int(round(py))
    x0, x1 = max(0, x - radius), min(w, x + radius + 1)
    y0, y1 = max(0, y - radius), min(h, y + radius + 1)
    if x0 >= x1 or y0 >= y1:
        return [0, 0, 0, 255]

    sub = arr[y0:y1, x0:x1].reshape(-1, 4)
    sub_mask = mask[y0:y1, x0:x1].reshape(-1)
    picked = sub[sub_mask]
    if picked.size == 0:
        return sample_rgba(arr, px, py)
    rgb = picked[:, :3].astype(np.float32).mean(axis=0)
    alpha = float(picked[:, 3].mean())
    return [int(round(rgb[0])), int(round(rgb[1])), int(round(rgb[2])), int(round(alpha))]


def hex_to_rgba(value: str, alpha: int = 255) -> list[int]:
    """``#RRGGBB`` / ``#RRGGBBAA`` → ``[r, g, b, a]``。"""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) not in (6, 8):
        raise ImageError(f"颜色格式不支持：{value}")
    try:
        channels = [int(text[i:i + 2], 16) for i in range(0, len(text), 2)]
    except ValueError as exc:
        raise ImageError(f"颜色格式不支持：{value}") from exc
    if len(channels) == 3:
        channels.append(alpha)
    return channels
