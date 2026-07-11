"""Central configuration — all tunable defaults live here."""

from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class Config:
    # ── scan ──────────────────────────────────────────────────────────────────
    passes: list[str] = field(default_factory=lambda: [
        "exact", "burst", "blur", "screenshot", "similar"
    ])

    # ── burst detection ───────────────────────────────────────────────────────
    burst_window_sec: float = 3.0
    phash_threshold: int = 10       # Hamming distance ≤ this → burst duplicate

    # ── quality cull ──────────────────────────────────────────────────────────
    blur_threshold: float = 80.0    # Laplacian variance below this → blurry
    brightness_min: float = 20.0    # mean pixel value below this → too dark
    brightness_max: float = 235.0   # mean pixel value above this → overexposed

    # ── near-duplicate (cross-session) ────────────────────────────────────────
    similar_threshold: int = 8      # stricter than burst; pHash distance

    # ── thumbnail ─────────────────────────────────────────────────────────────
    thumb_size: tuple[int, int] = (300, 300)
    thumb_quality: int = 75

    # ── server ────────────────────────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8080

    # ── paths (resolved at runtime relative to album folder) ─────────────────
    db_name: str = "review.db"
    cache_dir_name: str = "cache"
    permanent_delete_dir_name: str = "_permanent_delete"
    logs_dir_name: str = "logs"

    # ── performance ───────────────────────────────────────────────────────────
    worker_processes: int = 0       # 0 = auto (cpu_count - 1, min 1)
    batch_size: int = 64            # files per worker batch


IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".heic", ".heif",
    ".tiff", ".tif", ".webp", ".bmp",
})

SCREEN_RESOLUTIONS: frozenset[tuple[int, int]] = frozenset({
    # iPhone
    (1170, 2532), (1284, 2778), (1125, 2436), (828, 1792), (750, 1334),
    (1242, 2688), (1080, 2340), (1290, 2796), (1179, 2556),
    # Android
    (1080, 2400), (1080, 2280), (720, 1560), (1440, 3200),
    (1080, 2376), (1080, 1920), (1440, 2560), (1080, 2160),
    # iPad / tablet
    (2048, 2732), (1668, 2388), (2160, 2160), (1620, 2160),
    # Desktop
    (1920, 1080), (2560, 1440), (3840, 2160), (2560, 1600), (1440, 900),
})

REASON_LABELS: dict[str, str] = {
    "exact_duplicate": "Exact Duplicate",
    "burst": "Burst Shot",
    "blurry": "Blurry",
    "dark": "Too Dark",
    "overexposed": "Overexposed",
    "screenshot": "Screenshot",
    "similar": "Near-Duplicate",
}

REASON_ORDER: list[str] = [
    "exact_duplicate", "burst", "blurry", "dark",
    "overexposed", "screenshot", "similar",
]

# Shared singleton — callers import this and mutate it before use
default_config = Config()
