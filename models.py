"""Shared data models used across all modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED_DELETE = "approved_delete"
    KEPT = "kept"
    SKIPPED = "skipped"
    MOVED = "moved"          # physically in _permanent_delete/
    DELETED = "deleted"      # permanently removed from disk


class DuplicateReason(str, Enum):
    EXACT_DUPLICATE = "exact_duplicate"
    BURST = "burst"
    BLURRY = "blurry"
    DARK = "dark"
    OVEREXPOSED = "overexposed"
    SCREENSHOT = "screenshot"
    SIMILAR = "similar"


@dataclass
class PhotoRecord:
    """Full in-memory representation of one photo — mirrors the DB row."""
    id: int
    path: Path
    folder: str
    filename: str
    size_bytes: int
    mtime: float

    # computed
    md5: Optional[str] = None
    phash: Optional[str] = None
    sharpness: Optional[float] = None          # center-weighted Laplacian variance
    highlight_clipping: Optional[float] = None  # % pixels > 250
    shadow_clipping: Optional[float] = None     # % pixels < 5

    # EXIF — basic
    width: Optional[int] = None
    height: Optional[int] = None
    shot_time: Optional[datetime] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    has_camera_exif: bool = False

    # EXIF — extended
    exposure_time: Optional[str] = None   # stored as fraction string e.g. "1/1000"
    f_number: Optional[float] = None
    iso: Optional[int] = None
    lens_model: Optional[str] = None
    shutter_count: Optional[int] = None

    # state
    thumbnail_path: Optional[str] = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    move_destination: Optional[str] = None
    google_photos_url: Optional[str] = None
    rating: Optional[int] = None              # 1–5 stars, user-assigned

    @property
    def megapixels(self) -> Optional[float]:
        if self.width and self.height:
            return round(self.width * self.height / 1_000_000, 1)
        return None

    @property
    def resolution_str(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "unknown"


@dataclass
class DuplicateGroup:
    """A set of photos identified as duplicates/culls by one pass."""
    id: int
    reason: DuplicateReason
    notes: str
    confidence: float           # 0.0 – 1.0
    keeper_id: Optional[int]    # photo_id of the recommended keeper (None for quality culls)
    member_ids: list[int] = field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PENDING
    created_at: Optional[datetime] = None


@dataclass
class MoveRecord:
    """History entry for every file movement (for undo)."""
    id: int
    photo_id: int
    src_path: str
    dst_path: str
    action: str         # "approve_delete" | "move_to_permanent" | "restore"
    timestamp: datetime
    undone: bool = False
