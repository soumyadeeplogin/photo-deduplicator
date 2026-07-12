"""
File movement and undo logic.

Two-phase workflow:
  Phase 1 — approve_delete():   DB status → approved_delete  (file stays on disk)
  Phase 2 — process_approved(): files move → _permanent_delete/  (DB status → moved)
  Phase 3 — permanent_delete(): actual unlink from _permanent_delete/

Undo is possible for any action until permanent_delete() is called.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from database import Database
from models import ReviewStatus

logger = logging.getLogger(__name__)


class Mover:
    def __init__(self, db: Database, permanent_delete_dir: Path) -> None:
        self.db = db
        self.perm_dir = permanent_delete_dir
        self.perm_dir.mkdir(parents=True, exist_ok=True)

    # ── phase 1: approve ───────────────────────────────────────────────────

    def approve_group(self, group_id: int) -> int:
        """
        Mark all non-keeper members of a group as approved_delete.
        Returns count of photos approved.
        """
        group = self.db.get_group(group_id)
        if not group:
            raise ValueError(f"Group {group_id} not found")

        approved = 0
        for photo_id in group.member_ids:
            if photo_id == group.keeper_id:
                self._set_photo_status(photo_id, ReviewStatus.KEPT)
                continue
            self._set_photo_status(photo_id, ReviewStatus.APPROVED_DELETE)
            self.db.record_move(photo_id, "", "", "approve_delete")
            approved += 1

        self.db.update_group_status(group_id, ReviewStatus.APPROVED_DELETE)
        logger.info("Approved group %d: %d photos queued", group_id, approved)
        return approved

    def unapprove_group(self, group_id: int) -> int:
        """Revert a group back to pending — files never moved, just status reset."""
        group = self.db.get_group(group_id)
        if not group:
            raise ValueError(f"Group {group_id} not found")

        for photo_id in group.member_ids:
            self._set_photo_status(photo_id, ReviewStatus.PENDING)
        self.db.update_group_status(group_id, ReviewStatus.PENDING)
        logger.info("Unapproved group %d", group_id)
        return len(group.member_ids)

    def skip_group(self, group_id: int) -> None:
        self.db.update_group_status(group_id, ReviewStatus.SKIPPED)

    def change_keeper(self, group_id: int, new_keeper_id: int) -> None:
        group = self.db.get_group(group_id)
        if not group:
            raise ValueError(f"Group {group_id} not found")
        if new_keeper_id not in group.member_ids:
            raise ValueError(f"Photo {new_keeper_id} is not a member of group {group_id}")
        self.db.update_group_keeper(group_id, new_keeper_id)
        logger.info("Group %d keeper changed to photo %d", group_id, new_keeper_id)

    # ── batch operations ───────────────────────────────────────────────────

    def approve_all_high_confidence(self, min_confidence: float = 0.75) -> tuple[int, int]:
        """
        Approve all pending groups with confidence >= min_confidence.
        Returns (groups_approved, photos_approved).
        """
        groups = self.db.get_pending_groups(min_confidence=min_confidence)
        groups_count = 0
        photos_count = 0
        for group in groups:
            photos_count += self.approve_group(group.id)
            groups_count += 1
        logger.info(
            "Batch approve (≥%.0f%%): %d groups, %d photos",
            min_confidence * 100, groups_count, photos_count,
        )
        return groups_count, photos_count

    def approve_all_by_reason(self, reason: str) -> tuple[int, int]:
        """
        Approve all pending groups matching a given reason string.
        Returns (groups_approved, photos_approved).
        """
        groups = self.db.get_pending_groups(reason=reason)
        groups_count = 0
        photos_count = 0
        for group in groups:
            photos_count += self.approve_group(group.id)
            groups_count += 1
        logger.info(
            "Batch approve reason=%s: %d groups, %d photos",
            reason, groups_count, photos_count,
        )
        return groups_count, photos_count

    def approve_groups_by_ids(self, group_ids: list[int]) -> tuple[int, int]:
        """Approve a specific list of group IDs. Returns (groups, photos)."""
        groups_count = 0
        photos_count = 0
        for gid in group_ids:
            try:
                photos_count += self.approve_group(gid)
                groups_count += 1
            except ValueError:
                pass
        return groups_count, photos_count

    # ── phase 2: move to _permanent_delete ────────────────────────────────

    def process_approved(self, progress_cb=None) -> int:
        """
        Move all approved_delete photos into _permanent_delete/.
        Returns count of files moved.
        """
        photos = [
            p for p in self.db.get_all_photos()
            if p.review_status == ReviewStatus.APPROVED_DELETE
        ]
        total = len(photos)
        logger.info("Moving %d approved photos to %s", total, self.perm_dir)

        moved = 0
        for i, photo in enumerate(photos):
            if not photo.path.exists():
                logger.warning("File already gone: %s", photo.path)
                self._set_photo_status(photo.id, ReviewStatus.MOVED)
                continue

            dest = self._unique_dest(photo.path)
            try:
                shutil.move(str(photo.path), str(dest))
                self.db.update_photo_analysis(
                    photo.id,
                    review_status=ReviewStatus.MOVED.value,
                    move_destination=str(dest),
                )
                self.db.record_move(photo.id, str(photo.path), str(dest), "move_to_permanent")
                moved += 1
                if progress_cb:
                    progress_cb(i + 1, total, f"Moved {moved}/{total}")
            except Exception as exc:
                logger.error("Failed to move %s: %s", photo.path, exc)

        logger.info("Moved %d files", moved)
        return moved

    # ── phase 3: permanent delete ──────────────────────────────────────────

    def permanent_delete(self, photo_ids: Optional[list[int]] = None) -> int:
        """
        Permanently delete files from _permanent_delete/.
        If photo_ids is None, deletes all MOVED photos.
        THIS CANNOT BE UNDONE.
        Returns count deleted.
        """
        if photo_ids:
            photos = self.db.get_photos_by_ids(photo_ids)
        else:
            photos = [
                p for p in self.db.get_all_photos()
                if p.review_status == ReviewStatus.MOVED
            ]

        deleted = 0
        for photo in photos:
            dest = Path(photo.move_destination) if photo.move_destination else None
            if dest and dest.exists():
                try:
                    dest.unlink()
                    self.db.update_photo_analysis(
                        photo.id, review_status=ReviewStatus.DELETED.value
                    )
                    self.db.record_move(
                        photo.id, str(dest), "", "permanent_delete"
                    )
                    deleted += 1
                except Exception as exc:
                    logger.error("Failed to delete %s: %s", dest, exc)
            else:
                self.db.update_photo_analysis(
                    photo.id, review_status=ReviewStatus.DELETED.value
                )
                deleted += 1

        logger.info("Permanently deleted %d files", deleted)
        return deleted

    # ── undo ───────────────────────────────────────────────────────────────

    def undo_last(self) -> Optional[str]:
        """Undo the most recent non-undone move. Returns description or None."""
        history = self.db.get_move_history(limit=50, undone=False)
        # find most recent actual file move (not approve_delete)
        for record in history:
            if record.action not in ("move_to_permanent",):
                continue
            return self._undo_record(record)
        return None

    def undo_group(self, group_id: int) -> int:
        """Restore all moved files in a group."""
        group = self.db.get_group(group_id)
        if not group:
            return 0
        restored = 0
        for photo_id in group.member_ids:
            history = self.db.get_move_history(limit=100, undone=False)
            for record in history:
                if record.photo_id == photo_id and record.action == "move_to_permanent":
                    if self._undo_record(record):
                        restored += 1
                    break
        if restored:
            self.db.update_group_status(group_id, ReviewStatus.PENDING)
        return restored

    def undo_all(self) -> int:
        """Restore every file currently in _permanent_delete/. Nuclear option."""
        history = self.db.get_move_history(limit=10000, undone=False)
        restored = 0
        for record in history:
            if record.action == "move_to_permanent":
                if self._undo_record(record):
                    restored += 1
        return restored

    def _undo_record(self, record) -> bool:
        src = Path(record.dst_path)
        dst = Path(record.src_path)
        if not src.exists():
            logger.warning("Undo: source no longer exists: %s", src)
            return False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            self.db.update_photo_analysis(
                record.photo_id,
                review_status=ReviewStatus.PENDING.value,
                move_destination=None,
            )
            self.db.mark_move_undone(record.id)
            logger.info("Restored %s → %s", src, dst)
            return True
        except Exception as exc:
            logger.error("Undo failed %s: %s", src, exc)
            return False

    # ── helpers ────────────────────────────────────────────────────────────

    def _unique_dest(self, src: Path) -> Path:
        dest = self.perm_dir / src.name
        if not dest.exists():
            return dest
        stem, suffix = src.stem, src.suffix
        i = 1
        while True:
            dest = self.perm_dir / f"{stem}__{i}{suffix}"
            if not dest.exists():
                return dest
            i += 1

    def _set_photo_status(self, photo_id: int, status: ReviewStatus) -> None:
        self.db.update_photo_analysis(photo_id, review_status=status.value)
