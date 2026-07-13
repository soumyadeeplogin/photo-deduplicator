"""
Duplicate detection engine.

All five passes from the original dedupe.py are preserved and refactored
to work against PhotoRecord objects from the database rather than raw files.
Near-duplicate detection uses a BK-tree for O(n log n) instead of O(n²).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from config import Config, SCREEN_RESOLUTIONS
from database import Database
from models import DuplicateGroup, DuplicateReason, PhotoRecord, ReviewStatus

logger = logging.getLogger(__name__)


# ── BK-tree for Hamming-distance pHash queries ───────────────────────────────

class BKTree:
    """
    Burkhard-Keller tree over hex pHash strings.
    Supports O(log n) approximate nearest-neighbour search under Hamming distance.
    """

    def __init__(self) -> None:
        self._root: Optional[tuple] = None  # (hash_int, photo_id, children_dict)

    @staticmethod
    def _hamming(a: int, b: int) -> int:
        return bin(a ^ b).count("1")

    def insert(self, phash_hex: str, photo_id: int) -> None:
        val = int(phash_hex, 16)
        if self._root is None:
            self._root = (val, photo_id, {})
            return
        node = self._root
        while True:
            root_val, _, children = node
            d = self._hamming(val, root_val)
            if d in children:
                node = children[d]
            else:
                children[d] = (val, photo_id, {})
                break

    def query(self, phash_hex: str, threshold: int) -> list[tuple[int, int]]:
        """Return [(distance, photo_id)] for all entries within threshold."""
        target = int(phash_hex, 16)
        results: list[tuple[int, int]] = []
        stack = [self._root]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            node_val, node_id, children = node
            d = self._hamming(target, node_val)
            if d <= threshold:
                results.append((d, node_id))
            lo, hi = d - threshold, d + threshold
            for child_d, child_node in children.items():
                if lo <= child_d <= hi:
                    stack.append(child_node)
        return results


# ── keeper selection ─────────────────────────────────────────────────────────

def _pick_keeper(photos: list[PhotoRecord]) -> PhotoRecord:
    """
    Pick the best photo to keep from a group.
    Priority: highest user rating > highest center-weighted sharpness.
    """
    rated = [p for p in photos if p.rating is not None]
    if rated:
        return max(rated, key=lambda x: (x.rating or 0, x.sharpness or 0))
    return max(photos, key=lambda x: x.sharpness or 0)


# ── confidence scoring ────────────────────────────────────────────────────────

def _confidence_burst(
    photos: list[PhotoRecord],
    phash_distances: list[int],
) -> float:
    """
    Score 0-1 based on:
    - pHash distances (lower = more confident)
    - time delta between first and last frame
    """
    if not phash_distances:
        return 0.5
    avg_d = sum(phash_distances) / len(phash_distances)
    phash_score = max(0.0, 1.0 - avg_d / 64)

    # time tightness: 0.3s = 1.0, 3s = 0.7, 10s+ = 0.3
    timed = [p for p in photos if p.shot_time]
    if len(timed) >= 2:
        timed.sort(key=lambda x: x.shot_time)
        span = (timed[-1].shot_time - timed[0].shot_time).total_seconds()
        time_score = max(0.3, 1.0 - span / 15)
    else:
        time_score = 0.7

    return round(phash_score * 0.6 + time_score * 0.4, 3)


def _confidence_exact() -> float:
    return 1.0


def _confidence_similar(distance: int, max_dist: int = 8) -> float:
    return round(max(0.3, 1.0 - distance / (max_dist * 2)), 3)


def _confidence_quality(value: float, threshold: float, direction: str) -> float:
    """
    direction='low': value well below threshold → high confidence
    direction='high': value well above threshold → high confidence
    """
    if direction == "low":
        ratio = max(0.0, 1.0 - value / threshold)
    else:
        ratio = max(0.0, (value - threshold) / threshold)
    return round(0.5 + ratio * 0.5, 3)


# ── pass functions ────────────────────────────────────────────────────────────

def pass_exact_duplicates(
    photos: list[PhotoRecord],
) -> list[tuple[DuplicateReason, str, float, Optional[int], list[int]]]:
    """
    Returns list of (reason, notes, confidence, keeper_id, member_ids).
    """
    logger.info("[Pass 1] Exact duplicates (%d photos)", len(photos))
    groups: dict[str, list[PhotoRecord]] = defaultdict(list)
    for p in photos:
        if p.md5:
            groups[p.md5].append(p)

    results = []
    for md5, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: (
            x.shot_time or datetime.max,
            -x.size_bytes,
            str(x.path),
        ))
        keeper = group[0]
        all_ids = [p.id for p in group]
        notes = f"MD5: {md5[:12]}… — {len(group)} identical files"
        results.append((
            DuplicateReason.EXACT_DUPLICATE,
            notes,
            _confidence_exact(),
            keeper.id,
            all_ids,
        ))

    logger.info("  Found %d exact-duplicate groups", len(results))
    return results


def pass_burst_shots(
    photos: list[PhotoRecord],
    burst_window_sec: float,
    phash_threshold: int,
) -> list[tuple[DuplicateReason, str, float, Optional[int], list[int]]]:
    logger.info(
        "[Pass 2] Burst shots (window=%.1fs, pHash≤%d, %d photos)",
        burst_window_sec, phash_threshold, len(photos),
    )
    timed = [p for p in photos if p.shot_time is not None and p.phash is not None]
    timed.sort(key=lambda x: x.shot_time)
    logger.info("  %d/%d photos have timestamp+pHash", len(timed), len(photos))

    # time clustering
    clusters: list[list[PhotoRecord]] = []
    current: list[PhotoRecord] = []
    for photo in timed:
        if not current:
            current.append(photo)
            continue
        delta = (photo.shot_time - current[-1].shot_time).total_seconds()
        if delta <= burst_window_sec:
            current.append(photo)
        else:
            if len(current) > 1:
                clusters.append(current)
            current = [photo]
    if len(current) > 1:
        clusters.append(current)

    results = []
    for cluster in clusters:
        # verify at least one similar pair
        distances = []
        similar = False
        for i in range(len(cluster)):
            for j in range(i + 1, len(cluster)):
                pi, pj = cluster[i], cluster[j]
                if pi.phash and pj.phash:
                    d = _hamming_hex(pi.phash, pj.phash)
                    if d <= phash_threshold:
                        similar = True
                        distances.append(d)
        if not similar:
            continue

        keeper = _pick_keeper(cluster)
        ts = keeper.shot_time.strftime("%H:%M:%S") if keeper.shot_time else "unknown"
        conf = _confidence_burst(cluster, distances)
        notes = (
            f"Burst at {ts} — {len(cluster)} frames, "
            f"keeper sharpness={keeper.sharpness:.0f}"
            if keeper.sharpness else f"Burst at {ts} — {len(cluster)} frames"
        )
        results.append((
            DuplicateReason.BURST,
            notes,
            conf,
            keeper.id,
            [p.id for p in cluster],
        ))

    logger.info("  Found %d burst groups", len(results))
    return results


def pass_quality_cull(
    photos: list[PhotoRecord],
    blur_threshold: float,
    highlight_threshold: float,
    shadow_threshold: float,
) -> list[tuple[DuplicateReason, str, float, Optional[int], list[int]]]:
    """
    Quality cull using center-weighted sharpness and histogram-based exposure.

    Overexposed: > highlight_threshold % of pixels are blown (>250)
    Too dark:    > shadow_threshold % of pixels are crushed (<5)
    Blurry:      center-weighted Laplacian variance < blur_threshold
    """
    logger.info("[Pass 3] Quality cull (%d photos)", len(photos))
    results = []
    for p in photos:
        if p.sharpness is not None and p.sharpness < blur_threshold:
            conf = _confidence_quality(p.sharpness, blur_threshold, "low")
            notes = f"Center sharpness={p.sharpness:.1f} (threshold {blur_threshold})"
            results.append((DuplicateReason.BLURRY, notes, conf, None, [p.id]))
        elif p.highlight_clipping is not None and p.highlight_clipping > highlight_threshold:
            conf = round(min(1.0, 0.5 + (p.highlight_clipping - highlight_threshold) / 20), 3)
            notes = f"Highlights clipped: {p.highlight_clipping:.1f}% pixels > 250 (threshold {highlight_threshold}%)"
            results.append((DuplicateReason.OVEREXPOSED, notes, conf, None, [p.id]))
        elif p.shadow_clipping is not None and p.shadow_clipping > shadow_threshold:
            conf = round(min(1.0, 0.5 + (p.shadow_clipping - shadow_threshold) / 20), 3)
            notes = f"Shadows crushed: {p.shadow_clipping:.1f}% pixels < 5 (threshold {shadow_threshold}%)"
            results.append((DuplicateReason.DARK, notes, conf, None, [p.id]))

    logger.info("  Flagged %d low-quality photos", len(results))
    return results


def pass_screenshots(
    photos: list[PhotoRecord],
) -> list[tuple[DuplicateReason, str, float, Optional[int], list[int]]]:
    import re
    logger.info("[Pass 4] Screenshots (%d photos)", len(photos))
    results = []
    for p in photos:
        name_lower = p.filename.lower()
        is_screenshot_name = (
            name_lower.startswith("screenshot")
            or name_lower.startswith("screen_shot")
            or name_lower.startswith("capture")
            or bool(re.match(r"^screenshot[_\-\s]", name_lower))
        )
        no_camera = not p.has_camera_exif
        dims = (p.width, p.height) if p.width and p.height else None
        dims_sw = (p.height, p.width) if p.width and p.height else None
        matches_screen = bool(
            dims and (dims in SCREEN_RESOLUTIONS or dims_sw in SCREEN_RESOLUTIONS)
        )

        score = sum([is_screenshot_name, no_camera, matches_screen])
        if score < 2:
            continue

        parts = []
        if is_screenshot_name:
            parts.append("filename matches")
        if no_camera:
            parts.append("no camera EXIF")
        if matches_screen:
            parts.append(f"screen resolution {p.width}×{p.height}")
        conf = round(0.5 + score * 0.15, 3)
        results.append((DuplicateReason.SCREENSHOT, ", ".join(parts), conf, None, [p.id]))

    logger.info("  Found %d screenshots", len(results))
    return results


def pass_near_duplicates(
    photos: list[PhotoRecord],
    phash_threshold: int,
    burst_window_sec: float,
) -> list[tuple[DuplicateReason, str, float, Optional[int], list[int]]]:
    logger.info(
        "[Pass 5] Near-duplicates (pHash≤%d, %d photos)",
        phash_threshold, len(photos),
    )
    hashable = [p for p in photos if p.phash]

    # Build BK-tree
    tree = BKTree()
    for p in hashable:
        tree.insert(p.phash, p.id)

    id_to_photo = {p.id: p for p in hashable}
    assigned: set[int] = set()
    results = []

    for p in hashable:
        if p.id in assigned:
            continue
        neighbours = tree.query(p.phash, phash_threshold)
        # Filter out self, already-assigned, and burst-window neighbours
        cluster_pairs: list[tuple[PhotoRecord, int]] = []
        for dist, nid in neighbours:
            if nid == p.id or nid in assigned:
                continue
            neighbour = id_to_photo[nid]
            # Skip if same shooting session (burst pass covers those)
            if p.shot_time and neighbour.shot_time:
                delta = abs((p.shot_time - neighbour.shot_time).total_seconds())
                if delta <= burst_window_sec:
                    continue
            cluster_pairs.append((neighbour, dist))

        if not cluster_pairs:
            continue

        cluster = [p] + [cp[0] for cp in cluster_pairs]
        distances = [cp[1] for cp in cluster_pairs]
        assigned.update(c.id for c in cluster)

        keeper = _pick_keeper(cluster)
        avg_d = sum(distances) / len(distances) if distances else phash_threshold
        conf = _confidence_similar(int(avg_d), phash_threshold)
        notes = f"pHash distance ≤ {phash_threshold}, taken at different times"
        results.append((
            DuplicateReason.SIMILAR,
            notes,
            conf,
            keeper.id,
            [c.id for c in cluster],
        ))

    logger.info("  Found %d near-duplicate groups", len(results))
    return results


# ── orchestrator ──────────────────────────────────────────────────────────────

class DuplicateEngine:
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg

    def run(self, passes: Optional[list[str]] = None, progress_cb=None) -> int:
        """
        Run requested passes, write groups to DB.
        Returns total number of groups created.
        """
        passes = passes or self.cfg.passes
        all_photos = self.db.get_all_photos()

        # Clear previous groups before re-running
        self.db.clear_groups()

        trashed_ids: set[int] = set()
        total_groups = 0

        def eligible(photos: list[PhotoRecord]) -> list[PhotoRecord]:
            return [p for p in photos if p.id not in trashed_ids]

        def save(items):
            nonlocal total_groups
            for reason, notes, confidence, keeper_id, member_ids in items:
                self.db.insert_group(reason, notes, confidence, keeper_id, member_ids)
                # mark duplicates (non-keepers) as used
                for mid in member_ids:
                    if mid != keeper_id:
                        trashed_ids.add(mid)
                total_groups += 1

        if progress_cb:
            progress_cb(0, len(passes), "Running duplicate detection…")

        for i, pass_name in enumerate(passes):
            if pass_name == "exact":
                save(pass_exact_duplicates(eligible(all_photos)))
            elif pass_name == "burst":
                save(pass_burst_shots(
                    eligible(all_photos),
                    self.cfg.burst_window_sec,
                    self.cfg.phash_threshold,
                ))
            elif pass_name == "blur":
                save(pass_quality_cull(
                    eligible(all_photos),
                    self.cfg.blur_threshold,
                    self.cfg.highlight_threshold,
                    self.cfg.shadow_threshold,
                ))
            elif pass_name == "screenshot":
                save(pass_screenshots(eligible(all_photos)))
            elif pass_name == "similar":
                save(pass_near_duplicates(
                    eligible(all_photos),
                    self.cfg.similar_threshold,
                    self.cfg.burst_window_sec,
                ))
            if progress_cb:
                progress_cb(i + 1, len(passes), f"Pass '{pass_name}' complete")

        logger.info("Detection complete: %d groups created", total_groups)
        return total_groups


# ── helpers ───────────────────────────────────────────────────────────────────

def _hamming_hex(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 64
