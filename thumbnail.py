"""Thumbnail generation — cached JPEG files, never base64 in HTML."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def thumbnail_filename(photo_path: Path) -> str:
    """Stable filename for a photo's thumbnail, based on its absolute path."""
    key = hashlib.md5(str(photo_path).encode()).hexdigest()
    return f"{key}.jpg"


def _open_as_pil(photo_path: Path):
    """Open any supported image as a PIL Image (RGB). Returns None on failure."""
    from PIL import Image
    from config import RAW_EXTENSIONS
    suffix = photo_path.suffix.lower()
    if suffix in RAW_EXTENSIONS:
        try:
            import rawpy
            with rawpy.imread(str(photo_path)) as raw:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    half_size=True,
                    no_auto_bright=False,
                    output_bps=8,
                )
            return Image.fromarray(rgb).convert("RGB")
        except Exception:
            return None
    else:
        try:
            return Image.open(photo_path).convert("RGB")
        except Exception:
            return None


def generate_thumbnail(
    photo_path: Path,
    thumb_dir: Path,
    size: tuple[int, int] = (300, 300),
    quality: int = 75,
    force: bool = False,
) -> Optional[str]:
    """
    Generate a JPEG thumbnail in thumb_dir.

    Returns the relative path string (e.g. 'thumbnails/abc123.jpg')
    that can be served as a static file, or None on failure.
    """
    try:
        thumb_dir.mkdir(parents=True, exist_ok=True)
        fname = thumbnail_filename(photo_path)
        dest = thumb_dir / fname

        if dest.exists() and not force:
            return f"thumbnails/{fname}"

        img = _open_as_pil(photo_path)
        if img is None:
            return None
        with img:
            img.thumbnail(size, Image.LANCZOS)
            img.save(dest, format="JPEG", quality=quality, optimize=True)

        return f"thumbnails/{fname}"
    except Exception as exc:
        logger.debug("Thumbnail failed for %s: %s", photo_path, exc)
        return None


def generate_thumbnail_worker(args: tuple) -> tuple[int, Optional[str]]:
    """
    Top-level function so it pickles for ProcessPoolExecutor.

    args: (photo_id, photo_path_str, thumb_dir_str, size_w, size_h, quality)
    Returns: (photo_id, relative_thumb_path or None)
    """
    photo_id, path_str, thumb_dir_str, w, h, quality = args
    result = generate_thumbnail(
        Path(path_str),
        Path(thumb_dir_str),
        size=(w, h),
        quality=quality,
    )
    return photo_id, result
