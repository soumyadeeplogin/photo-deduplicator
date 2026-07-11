"""
File scanning and per-photo analysis.

All CPU-bound analysis functions are top-level so they pickle cleanly
for ProcessPoolExecutor.  The Scanner class orchestrates parallel work
and writes results to the database, skipping files that haven't changed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import IMAGE_EXTENSIONS, SCREEN_RESOLUTIONS, Config
from database import Database
from thumbnail import generate_thumbnail_worker

logger = logging.getLogger(__name__)


# ── top-level worker functions (must be picklable) ───────────────────────────

def _analyse_photo(args: tuple) -> dict:
    """
    Analyse one photo file.  Returns a dict of all computed fields
    keyed by DB column name.  Safe to call in a worker process.

    args: (path_str, photo_id)
    """
    path_str, photo_id = args
    path = Path(path_str)
    result: dict = {"id": photo_id, "path": path_str}

    try:
        import cv2
        import exifread
        import imagehash
        from PIL import Image

        # ── MD5 ──────────────────────────────────────────────────────────────
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        result["md5"] = h.hexdigest()

        # ── pHash ─────────────────────────────────────────────────────────────
        try:
            with Image.open(path) as img:
                result["phash"] = str(imagehash.phash(img))
        except Exception:
            result["phash"] = None

        # ── OpenCV sharpness + brightness ─────────────────────────────────────
        img_cv = cv2.imread(str(path))
        if img_cv is not None:
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            result["sharpness"] = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            result["brightness"] = float(gray.mean())
        else:
            result["sharpness"] = None
            result["brightness"] = None

        # ── EXIF ──────────────────────────────────────────────────────────────
        exif = _read_exif(path)
        result.update(exif)

    except Exception as exc:
        logger.debug("Analysis failed %s: %s", path, exc)

    return result


def _read_exif(path: Path) -> dict:
    """Extract EXIF fields.  Returns a dict of DB-column-name → value."""
    out: dict = {
        "width": None, "height": None,
        "shot_time": None,
        "camera_make": None, "camera_model": None,
        "gps_lat": None, "gps_lon": None,
        "has_camera_exif": 0,
    }
    try:
        import exifread
        from PIL import Image

        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)

        out["has_camera_exif"] = int(
            bool(tags.get("Image Make") or tags.get("Image Model"))
        )
        if tags.get("Image Make"):
            out["camera_make"] = str(tags["Image Make"]).strip()
        if tags.get("Image Model"):
            out["camera_model"] = str(tags["Image Model"]).strip()

        # Dimensions
        w = tags.get("EXIF ExifImageWidth") or tags.get("Image ImageWidth")
        h = tags.get("EXIF ExifImageLength") or tags.get("Image ImageLength")
        if w and h:
            out["width"] = int(str(w))
            out["height"] = int(str(h))
        else:
            try:
                with Image.open(path) as img:
                    out["width"], out["height"] = img.width, img.height
            except Exception:
                pass

        # Timestamp
        dt_tag = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
        if dt_tag:
            dt_str = str(dt_tag).strip()
            sub_tag = tags.get("EXIF SubSecTimeOriginal") or tags.get("EXIF SubSecTime")
            if sub_tag:
                subsec = str(sub_tag).strip().ljust(6, "0")[:6]
                try:
                    dt = datetime.strptime(f"{dt_str}.{subsec}", "%Y:%m:%d %H:%M:%S.%f")
                    out["shot_time"] = dt.strftime("%Y-%m-%d %H:%M:%S.%f")
                    return out
                except ValueError:
                    pass
            try:
                dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                out["shot_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        # GPS
        def _gps_dms(tag) -> Optional[float]:
            if tag is None:
                return None
            try:
                vals = tag.values
                d = float(vals[0].num) / float(vals[0].den)
                m = float(vals[1].num) / float(vals[1].den)
                s = float(vals[2].num) / float(vals[2].den)
                return d + m / 60 + s / 3600
            except Exception:
                return None

        lat = _gps_dms(tags.get("GPS GPSLatitude"))
        lon = _gps_dms(tags.get("GPS GPSLongitude"))
        lat_ref = str(tags.get("GPS GPSLatitudeRef", "N"))
        lon_ref = str(tags.get("GPS GPSLongitudeRef", "E"))
        if lat is not None:
            out["gps_lat"] = lat if "N" in lat_ref else -lat
        if lon is not None:
            out["gps_lon"] = lon if "E" in lon_ref else -lon

    except Exception as exc:
        logger.debug("EXIF read failed %s: %s", path, exc)

    return out


def _discover_files(folder: Path) -> list[Path]:
    """Recursively find all image files, ignoring our own output directories."""
    skip_dirs = {"_permanent_delete", "cache", "logs"}
    files: list[Path] = []
    for root, dirs, filenames in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in filenames:
            if Path(fn).suffix.lower() in IMAGE_EXTENSIONS:
                files.append(Path(root) / fn)
    files.sort()
    return files


# ── Scanner ──────────────────────────────────────────────────────────────────

class Scanner:
    def __init__(self, folder: Path, db: Database, cfg: Config) -> None:
        self.folder = folder
        self.db = db
        self.cfg = cfg
        self._workers = cfg.worker_processes or max(1, (os.cpu_count() or 2) - 1)

    def scan(self, progress_cb=None) -> int:
        """
        Discover files, analyse new/changed ones, generate thumbnails.

        progress_cb(current, total, message) is called periodically if provided.
        Returns the count of photos analysed (0 = nothing new).
        """
        logger.info("Scanning %s with %d workers", self.folder, self._workers)

        files = _discover_files(self.folder)
        total = len(files)
        logger.info("Discovered %d image files", total)

        if progress_cb:
            progress_cb(0, total, f"Discovered {total} files")

        # ── upsert stubs for all files ─────────────────────────────────────
        for f in files:
            st = f.stat()
            self.db.upsert_photo_stub(f, st.st_size, st.st_mtime)

        # ── find photos that need analysis ────────────────────────────────
        paths_mtimes = {
            str(f): (f.stat().st_size, f.stat().st_mtime) for f in files
        }
        stale_paths = set(self.db.get_stale_photos(paths_mtimes))
        need_analysis = self.db.get_photos_missing_analysis()
        # combine: stale files + any that have no hash at all
        stale_paths.update(str(p.path) for p in need_analysis)

        todo = [
            (str(p.path), p.id)
            for p in self.db.get_all_photos()
            if str(p.path) in stale_paths
        ]

        if not todo:
            logger.info("All %d photos are up-to-date, skipping analysis", total)
            if progress_cb:
                progress_cb(total, total, "All photos up-to-date")
            return 0

        logger.info("Analysing %d new/changed photos", len(todo))
        if progress_cb:
            progress_cb(0, len(todo), f"Analysing {len(todo)} photos…")

        # ── parallel analysis ──────────────────────────────────────────────
        done = 0
        with ProcessPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(_analyse_photo, args): args for args in todo}
            for future in as_completed(futures):
                result = future.result()
                photo_id = result.pop("id")
                result.pop("path", None)
                self.db.update_photo_analysis(photo_id, **result)
                done += 1
                if progress_cb and done % 10 == 0:
                    progress_cb(done, len(todo), f"Analysed {done}/{len(todo)}")

        if progress_cb:
            progress_cb(len(todo), len(todo), f"Analysis complete ({len(todo)} photos)")

        # ── thumbnail generation ───────────────────────────────────────────
        self._generate_thumbnails(progress_cb)

        return len(todo)

    def _generate_thumbnails(self, progress_cb=None) -> None:
        thumb_dir = self.folder.parent / self.cfg.cache_dir_name / "thumbnails"
        photos = self.db.get_all_photos()
        todo = [p for p in photos if p.thumbnail_path is None]

        if not todo:
            return

        logger.info("Generating %d thumbnails", len(todo))
        if progress_cb:
            progress_cb(0, len(todo), f"Generating {len(todo)} thumbnails…")

        w, h = self.cfg.thumb_size
        args_list = [
            (p.id, str(p.path), str(thumb_dir), w, h, self.cfg.thumb_quality)
            for p in todo
        ]

        done = 0
        with ProcessPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(generate_thumbnail_worker, a): a for a in args_list}
            for future in as_completed(futures):
                photo_id, thumb_rel = future.result()
                if thumb_rel:
                    self.db.update_photo_analysis(photo_id, thumbnail_path=thumb_rel)
                done += 1
                if progress_cb and done % 20 == 0:
                    progress_cb(done, len(todo), f"Thumbnails {done}/{len(todo)}")

        if progress_cb:
            progress_cb(len(todo), len(todo), "Thumbnails done")
