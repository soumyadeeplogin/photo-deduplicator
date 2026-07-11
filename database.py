"""SQLite persistence layer.

Schema owns photos, duplicate_groups, group_members, move_history.
All expensive analysis results are stored here — second scan only
re-analyses files whose (path, mtime, size_bytes) changed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from models import DuplicateGroup, DuplicateReason, MoveRecord, PhotoRecord, ReviewStatus

logger = logging.getLogger(__name__)

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS photos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    path             TEXT    NOT NULL UNIQUE,
    folder           TEXT    NOT NULL,
    filename         TEXT    NOT NULL,
    size_bytes       INTEGER NOT NULL,
    mtime            REAL    NOT NULL,

    -- computed (nullable until analysed)
    md5              TEXT,
    phash            TEXT,
    sharpness        REAL,
    brightness       REAL,

    -- EXIF
    width            INTEGER,
    height           INTEGER,
    shot_time        TEXT,
    camera_make      TEXT,
    camera_model     TEXT,
    gps_lat          REAL,
    gps_lon          REAL,
    has_camera_exif  INTEGER NOT NULL DEFAULT 0,

    -- state
    thumbnail_path   TEXT,
    review_status    TEXT NOT NULL DEFAULT 'pending',
    move_destination TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_photos_md5    ON photos(md5);
CREATE INDEX IF NOT EXISTS idx_photos_phash  ON photos(phash);
CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(review_status);
CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reason        TEXT    NOT NULL,
    notes         TEXT    NOT NULL DEFAULT '',
    confidence    REAL    NOT NULL DEFAULT 0.5,
    keeper_id     INTEGER REFERENCES photos(id),
    review_status TEXT    NOT NULL DEFAULT 'pending',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_groups_reason ON duplicate_groups(reason);
CREATE INDEX IF NOT EXISTS idx_groups_status ON duplicate_groups(review_status);

CREATE TABLE IF NOT EXISTS group_members (
    group_id  INTEGER NOT NULL REFERENCES duplicate_groups(id) ON DELETE CASCADE,
    photo_id  INTEGER NOT NULL REFERENCES photos(id)           ON DELETE CASCADE,
    PRIMARY KEY (group_id, photo_id)
);

CREATE INDEX IF NOT EXISTS idx_members_photo ON group_members(photo_id);

CREATE TABLE IF NOT EXISTS move_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id  INTEGER NOT NULL REFERENCES photos(id),
    src_path  TEXT    NOT NULL,
    dst_path  TEXT    NOT NULL,
    action    TEXT    NOT NULL,
    timestamp TEXT    NOT NULL DEFAULT (datetime('now')),
    undone    INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(DDL)
        self._conn.commit()
        logger.debug("Database opened: %s", self._path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @contextmanager
    def tx(self) -> Generator[sqlite3.Cursor, None, None]:
        assert self._conn, "call connect() first"
        cursor = self._conn.cursor()
        try:
            yield cursor
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ── photo ──────────────────────────────────────────────────────────────

    def upsert_photo_stub(self, path: Path, size_bytes: int, mtime: float) -> int:
        """Insert or update the file-identity columns; return the row id."""
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO photos (path, folder, filename, size_bytes, mtime)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    mtime      = excluded.mtime
                """,
                (str(path), str(path.parent), path.name, size_bytes, mtime),
            )
            row = c.execute("SELECT id FROM photos WHERE path = ?", (str(path),)).fetchone()
            return row["id"]

    def update_photo_analysis(self, photo_id: int, **fields: object) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(
                f"UPDATE photos SET {cols} WHERE id = ?",
                (*fields.values(), photo_id),
            )

    def get_photo(self, photo_id: int) -> Optional[PhotoRecord]:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        return _row_to_photo(row) if row else None

    def get_photo_by_path(self, path: Path) -> Optional[PhotoRecord]:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM photos WHERE path = ?", (str(path),)
        ).fetchone()
        return _row_to_photo(row) if row else None

    def get_all_photos(self) -> list[PhotoRecord]:
        assert self._conn
        rows = self._conn.execute("SELECT * FROM photos ORDER BY path").fetchall()
        return [_row_to_photo(r) for r in rows]

    def get_stale_photos(self, paths_mtimes: dict[str, tuple[int, float]]) -> list[str]:
        """Return paths that need re-analysis (new file or mtime/size changed)."""
        assert self._conn
        stale = []
        rows = self._conn.execute(
            "SELECT path, size_bytes, mtime, md5 FROM photos"
        ).fetchall()
        known = {r["path"]: r for r in rows}
        for path, (size, mtime) in paths_mtimes.items():
            row = known.get(path)
            if row is None or row["size_bytes"] != size or abs(row["mtime"] - mtime) > 0.01:
                stale.append(path)
        return stale

    def get_photos_missing_analysis(self) -> list[PhotoRecord]:
        """Photos already in the DB but lacking MD5/pHash/sharpness."""
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM photos WHERE md5 IS NULL OR phash IS NULL"
        ).fetchall()
        return [_row_to_photo(r) for r in rows]

    def get_photos_by_ids(self, ids: list[int]) -> list[PhotoRecord]:
        assert self._conn
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT * FROM photos WHERE id IN ({placeholders})", ids
        ).fetchall()
        return [_row_to_photo(r) for r in rows]

    # ── groups ─────────────────────────────────────────────────────────────

    def insert_group(
        self,
        reason: DuplicateReason,
        notes: str,
        confidence: float,
        keeper_id: Optional[int],
        member_ids: list[int],
    ) -> int:
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO duplicate_groups (reason, notes, confidence, keeper_id)
                VALUES (?, ?, ?, ?)
                """,
                (reason.value, notes, confidence, keeper_id),
            )
            group_id = c.lastrowid
            c.executemany(
                "INSERT OR IGNORE INTO group_members (group_id, photo_id) VALUES (?, ?)",
                [(group_id, pid) for pid in member_ids],
            )
        return group_id

    def clear_groups(self) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM group_members")
            c.execute("DELETE FROM duplicate_groups")

    def get_group(self, group_id: int) -> Optional[DuplicateGroup]:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM duplicate_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if not row:
            return None
        return _row_to_group(row, self._get_member_ids(group_id))

    def get_all_groups(
        self,
        reason: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        order_by: str = "created_at",
        limit: int = 100,
        offset: int = 0,
    ) -> list[DuplicateGroup]:
        assert self._conn
        where: list[str] = []
        params: list[object] = []

        if reason:
            where.append("dg.reason = ?")
            params.append(reason)
        if status:
            where.append("dg.review_status = ?")
            params.append(status)
        if search:
            where.append(
                "EXISTS (SELECT 1 FROM group_members gm "
                "JOIN photos p ON p.id = gm.photo_id "
                "WHERE gm.group_id = dg.id AND (p.filename LIKE ? OR p.folder LIKE ?))"
            )
            like = f"%{search}%"
            params.extend([like, like])

        safe_order = {
            "created_at", "confidence", "reason", "review_status"
        }
        col = order_by if order_by in safe_order else "created_at"
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._conn.execute(
            f"""
            SELECT dg.* FROM duplicate_groups dg
            {where_sql}
            ORDER BY dg.{col} DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        return [
            _row_to_group(r, self._get_member_ids(r["id"]))
            for r in rows
        ]

    def count_groups(
        self,
        reason: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        assert self._conn
        where: list[str] = []
        params: list[object] = []
        if reason:
            where.append("reason = ?")
            params.append(reason)
        if status:
            where.append("review_status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM duplicate_groups {where_sql}", params
        ).fetchone()
        return row["n"]

    def update_group_status(self, group_id: int, status: ReviewStatus) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE duplicate_groups SET review_status = ? WHERE id = ?",
                (status.value, group_id),
            )

    def update_group_keeper(self, group_id: int, keeper_id: int) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE duplicate_groups SET keeper_id = ? WHERE id = ?",
                (keeper_id, group_id),
            )

    def _get_member_ids(self, group_id: int) -> list[int]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT photo_id FROM group_members WHERE group_id = ?", (group_id,)
        ).fetchall()
        return [r["photo_id"] for r in rows]

    # ── move history ───────────────────────────────────────────────────────

    def record_move(
        self,
        photo_id: int,
        src: str,
        dst: str,
        action: str,
    ) -> int:
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO move_history (photo_id, src_path, dst_path, action)
                VALUES (?, ?, ?, ?)
                """,
                (photo_id, src, dst, action),
            )
            return c.lastrowid

    def get_move_history(
        self, limit: int = 50, undone: Optional[bool] = None
    ) -> list[MoveRecord]:
        assert self._conn
        where = ""
        params: list[object] = []
        if undone is not None:
            where = "WHERE undone = ?"
            params.append(1 if undone else 0)
        rows = self._conn.execute(
            f"SELECT * FROM move_history {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_row_to_move(r) for r in rows]

    def mark_move_undone(self, move_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE move_history SET undone = 1 WHERE id = ?", (move_id,))

    # ── stats ──────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        assert self._conn
        r = self._conn
        total = r.execute("SELECT COUNT(*) AS n, SUM(size_bytes) AS b FROM photos").fetchone()
        by_reason = r.execute(
            "SELECT reason, COUNT(*) AS n FROM duplicate_groups GROUP BY reason"
        ).fetchall()
        by_status = r.execute(
            "SELECT review_status, COUNT(*) AS n FROM duplicate_groups GROUP BY review_status"
        ).fetchall()
        flagged = r.execute(
            """
            SELECT SUM(p.size_bytes) AS b FROM photos p
            JOIN group_members gm ON gm.photo_id = p.id
            JOIN duplicate_groups dg ON dg.id = gm.group_id
            WHERE p.id != dg.keeper_id OR dg.keeper_id IS NULL
            """
        ).fetchone()
        return {
            "total_photos": total["n"] or 0,
            "total_size_bytes": total["b"] or 0,
            "flagged_size_bytes": flagged["b"] or 0,
            "by_reason": {r["reason"]: r["n"] for r in by_reason},
            "by_status": {r["review_status"]: r["n"] for r in by_status},
        }


# ── row converters ──────────────────────────────────────────────────────────

def _row_to_photo(row: sqlite3.Row) -> PhotoRecord:
    return PhotoRecord(
        id=row["id"],
        path=Path(row["path"]),
        folder=row["folder"],
        filename=row["filename"],
        size_bytes=row["size_bytes"],
        mtime=row["mtime"],
        md5=row["md5"],
        phash=row["phash"],
        sharpness=row["sharpness"],
        brightness=row["brightness"],
        width=row["width"],
        height=row["height"],
        shot_time=_parse_dt(row["shot_time"]),
        camera_make=row["camera_make"],
        camera_model=row["camera_model"],
        gps_lat=row["gps_lat"],
        gps_lon=row["gps_lon"],
        has_camera_exif=bool(row["has_camera_exif"]),
        thumbnail_path=row["thumbnail_path"],
        review_status=ReviewStatus(row["review_status"]),
        move_destination=row["move_destination"],
    )


def _row_to_group(row: sqlite3.Row, member_ids: list[int]) -> DuplicateGroup:
    return DuplicateGroup(
        id=row["id"],
        reason=DuplicateReason(row["reason"]),
        notes=row["notes"],
        confidence=row["confidence"],
        keeper_id=row["keeper_id"],
        member_ids=member_ids,
        review_status=ReviewStatus(row["review_status"]),
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_move(row: sqlite3.Row) -> MoveRecord:
    return MoveRecord(
        id=row["id"],
        photo_id=row["photo_id"],
        src_path=row["src_path"],
        dst_path=row["dst_path"],
        action=row["action"],
        timestamp=_parse_dt(row["timestamp"]) or datetime.now(),
        undone=bool(row["undone"]),
    )


def _parse_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None
