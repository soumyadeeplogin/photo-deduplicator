"""
FastAPI review server.

New routes (folder picker):
  GET  /setup                   → folder selection page
  POST /api/folder              → set folder, start scan, return SSE job id
  GET  /api/browse              → list sub-directories of a path (for browser widget)

Existing routes (unchanged):
  GET  /                        → dashboard (redirects to /setup if no folder)
  GET  /review                  → group list
  GET  /review/{group_id}       → group detail
  POST /review/{group_id}/approve|unapprove|skip|keeper/{pid}
  POST /process                 → move approved → _permanent_delete/
  POST /delete                  → permanently delete
  POST /undo/last|group/{id}|all
  GET  /stats
  GET  /report/html|csv|json
  GET  /cache/thumbnails/{fname}
  GET  /photo/{photo_id}/original
  GET  /api/scan/status         → SSE scan progress
  POST /api/scan                → trigger rescan
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from typing import Annotated
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import Config, REASON_LABELS, REASON_ORDER
from database import Database
from duplicate_engine import DuplicateEngine
from models import ReviewStatus
from mover import Mover
from scanner import Scanner

logger = logging.getLogger(__name__)

# ── shared scan progress ──────────────────────────────────────────────────────

_scan_progress: dict = {"current": 0, "total": 0, "message": "", "done": True, "error": ""}
_scan_lock = threading.Lock()

# ── shared export progress ────────────────────────────────────────────────────

_export_progress: dict = {"current": 0, "total": 0, "message": "", "done": True, "error": "", "dest": ""}
_export_lock = threading.Lock()


def _progress_cb(current: int, total: int, message: str) -> None:
    with _scan_lock:
        _scan_progress.update(current=current, total=total, message=message, done=False, error="")


# ── app state (mutable after folder is chosen) ────────────────────────────────

class AppState:
    """Holds the active folder, DB connection, and derived objects.
    Replaced atomically when the user selects a new folder."""

    def __init__(self, folder: Path, cfg: Config) -> None:
        self.folder = folder
        self.cfg = cfg
        self.db_path = folder.parent / cfg.db_name
        self.db = Database(self.db_path)
        self.db.connect()
        self.cache_dir = folder.parent / cfg.cache_dir_name
        self.thumb_dir = self.cache_dir / "thumbnails"
        self.perm_dir = folder.parent / cfg.permanent_delete_dir_name
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.perm_dir.mkdir(parents=True, exist_ok=True)
        self.scanner = Scanner(folder, self.db, cfg)
        self.engine = DuplicateEngine(self.db, cfg)
        self.mover = Mover(self.db, self.perm_dir)

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass


# ── app factory ───────────────────────────────────────────────────────────────

def create_app(initial_folder: Optional[Path], cfg: Config) -> FastAPI:
    app = FastAPI(title="Photo Deduplicator", docs_url=None, redoc_url=None)

    base_dir = Path(__file__).parent
    templates = Jinja2Templates(directory=str(base_dir / "templates"))
    static_dir = base_dir / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Mutable state — replaced when user selects a new folder
    state: list[Optional[AppState]] = [None]
    _state_lock = threading.Lock()

    # ── helpers ────────────────────────────────────────────────────────────

    def _fmt_size(b: Optional[int]) -> str:
        if b is None:
            return "?"
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} TB"

    templates.env.filters["fmt_size"] = _fmt_size
    templates.env.globals["reason_labels"] = REASON_LABELS
    templates.env.globals["reason_order"] = REASON_ORDER
    templates.env.globals["ReviewStatus"] = ReviewStatus

    def _get_state() -> Optional[AppState]:
        return state[0]

    def _require_state() -> AppState:
        s = state[0]
        if s is None:
            raise HTTPException(503, "No folder selected yet")
        return s

    def _set_folder_and_scan(folder: Path) -> None:
        """Replace AppState and run a full scan in the background."""
        with _state_lock:
            old = state[0]
            new_state = AppState(folder, cfg)
            state[0] = new_state
            if old:
                old.close()

        with _scan_lock:
            _scan_progress.update(current=0, total=0, message="Starting scan…",
                                  done=False, error="")
        try:
            new_state.scanner.scan(progress_cb=_progress_cb)
            new_state.engine.run(progress_cb=_progress_cb)
            with _scan_lock:
                _scan_progress.update(done=True, message="Scan complete")
        except Exception as exc:
            logger.exception("Scan failed: %s", exc)
            with _scan_lock:
                _scan_progress.update(done=True, error=str(exc),
                                      message=f"Scan failed: {exc}")

    # ── thumbnail route (dynamic — folder changes at runtime) ──────────────

    @app.get("/cache/thumbnails/{filename}")
    async def serve_thumbnail(filename: str):
        s = _get_state()
        if s is None:
            raise HTTPException(404, "No folder selected")
        thumb = s.thumb_dir / filename
        if not thumb.exists():
            raise HTTPException(404, "Thumbnail not found")
        return FileResponse(str(thumb), media_type="image/jpeg")

    # ── setup / folder picker ──────────────────────────────────────────────

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        s = _get_state()
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"current_folder": str(s.folder) if s else ""},
        )

    @app.post("/api/folder")
    async def set_folder(body: Annotated[dict, Body()]):
        folder_str = body.get("folder", "").strip()
        if not folder_str:
            raise HTTPException(400, "folder is required")
        folder = Path(folder_str).expanduser().resolve()
        if not folder.is_dir():
            raise HTTPException(400, f"Folder not found: {folder}")
        # kick off scan in background thread
        t = threading.Thread(target=_set_folder_and_scan, args=(folder,), daemon=True)
        t.start()
        return {"ok": True, "folder": str(folder)}

    @app.get("/api/browse")
    async def browse(path: str = ""):
        """Return sub-directories of `path` for the folder browser widget."""
        if not path:
            # Return sensible default roots per platform
            if sys.platform == "win32":
                import string
                drives = [f"{d}:\\" for d in string.ascii_uppercase
                          if Path(f"{d}:\\").exists()]
                return {"path": "", "parent": None,
                        "dirs": [{"name": d, "path": d} for d in drives]}
            else:
                path = str(Path.home())

        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise HTTPException(400, "Not a directory")

        try:
            subdirs = sorted(
                [d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")],
                key=lambda x: x.name.lower(),
            )
        except PermissionError:
            subdirs = []

        parent = str(p.parent) if p.parent != p else None
        return {
            "path": str(p),
            "parent": parent,
            "dirs": [{"name": d.name, "path": str(d)} for d in subdirs],
        }

    # ── dashboard ──────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        s = _get_state()
        if s is None:
            return RedirectResponse("/setup")
        stats = s.db.get_stats()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"stats": stats, "folder": str(s.folder)},
        )

    # ── review list ────────────────────────────────────────────────────────

    @app.get("/review", response_class=HTMLResponse)
    async def review_list(
        request: Request,
        reason: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        order: str = "created_at",
        page: int = 1,
        per_page: int = 20,
    ):
        s = _require_state()
        offset = (page - 1) * per_page
        groups = s.db.get_all_groups(
            reason=reason, status=status, search=search,
            order_by=order, limit=per_page, offset=offset,
        )
        total = s.db.count_groups(reason=reason, status=status)
        total_pages = max(1, (total + per_page - 1) // per_page)

        enriched = []
        for g in groups:
            photos = s.db.get_photos_by_ids(g.member_ids)
            photo_map = {p.id: p for p in photos}
            enriched.append({"group": g, "photos": photo_map})

        return templates.TemplateResponse(
            request,
            "review_list.html",
            {
                "groups": enriched,
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "filters": {"reason": reason, "status": status,
                            "search": search, "order": order},
                "stats": s.db.get_stats(),
            },
        )

    # ── group detail ───────────────────────────────────────────────────────

    @app.get("/review/{group_id}", response_class=HTMLResponse)
    async def review_detail(request: Request, group_id: int):
        s = _require_state()
        group = s.db.get_group(group_id)
        if not group:
            raise HTTPException(404, "Group not found")
        photos = s.db.get_photos_by_ids(group.member_ids)
        photo_map = {p.id: p for p in photos}

        all_ids = [g.id for g in s.db.get_all_groups(limit=10000)]
        try:
            idx = all_ids.index(group_id)
            prev_id = all_ids[idx - 1] if idx > 0 else None
            next_id = all_ids[idx + 1] if idx < len(all_ids) - 1 else None
        except ValueError:
            prev_id = next_id = None

        return templates.TemplateResponse(
            request,
            "review_detail.html",
            {"group": group, "photos": photo_map,
             "prev_id": prev_id, "next_id": next_id},
        )

    # ── review actions ─────────────────────────────────────────────────────

    @app.post("/review/{group_id}/approve")
    async def approve(group_id: int):
        s = _require_state()
        try:
            count = s.mover.approve_group(group_id)
            return {"ok": True, "approved": count}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/review/{group_id}/unapprove")
    async def unapprove(group_id: int):
        s = _require_state()
        try:
            count = s.mover.unapprove_group(group_id)
            return {"ok": True, "restored": count}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/review/{group_id}/skip")
    async def skip(group_id: int):
        s = _require_state()
        s.mover.skip_group(group_id)
        return {"ok": True}

    @app.post("/review/{group_id}/keeper/{photo_id}")
    async def change_keeper(group_id: int, photo_id: int):
        s = _require_state()
        try:
            s.mover.change_keeper(group_id, photo_id)
            return {"ok": True}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/review/{group_id}/keep_all")
    async def keep_all(group_id: int):
        """Mark all photos in the group as skipped (keep everything, delete nothing)."""
        s = _require_state()
        try:
            s.mover.keep_all_group(group_id)
            return {"ok": True}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/review/{group_id}/delete_all")
    async def delete_all(group_id: int):
        """Queue every photo in the group for deletion (no keeper)."""
        s = _require_state()
        try:
            count = s.mover.delete_all_group(group_id)
            return {"ok": True, "approved": count}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/photo/{photo_id}/rate/{stars}")
    async def rate_photo(photo_id: int, stars: int):
        s = _require_state()
        if stars < 0 or stars > 5:
            raise HTTPException(400, "Rating must be 0-5 (0 clears the rating)")
        s.db.update_photo_rating(photo_id, stars if stars > 0 else None)
        return {"ok": True, "photo_id": photo_id, "rating": stars if stars > 0 else None}

    # ── batch approve ──────────────────────────────────────────────────────

    class BatchApproveBody(BaseModel):
        min_confidence: float = 0.75
        reason: Optional[str] = None
        group_ids: Optional[list[int]] = None

    @app.post("/api/batch/approve")
    async def batch_approve(body: BatchApproveBody):
        s = _require_state()
        if body.group_ids is not None:
            g, p = s.mover.approve_groups_by_ids(body.group_ids)
        elif body.reason:
            g, p = s.mover.approve_all_by_reason(body.reason)
        else:
            g, p = s.mover.approve_all_high_confidence(body.min_confidence)
        return {"ok": True, "groups_approved": g, "photos_approved": p}

    # ── history page ───────────────────────────────────────────────────────

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request, limit: int = 100):
        s = _require_state()
        records = s.db.get_move_history(limit=limit)
        # Enrich with photo info
        enriched = []
        for rec in records:
            photo = s.db.get_photo(rec.photo_id)
            enriched.append({"record": rec, "photo": photo})
        return templates.TemplateResponse(
            request,
            "history.html",
            {"records": enriched, "stats": s.db.get_stats()},
        )

    # ── process & delete ───────────────────────────────────────────────────

    @app.post("/process")
    async def process_approved():
        s = _require_state()
        count = s.mover.process_approved()
        return {"ok": True, "moved": count}

    class DeleteBody(BaseModel):
        photo_ids: Optional[list[int]] = None

    @app.post("/delete")
    async def delete_permanent(body: DeleteBody):
        s = _require_state()
        count = s.mover.permanent_delete(body.photo_ids)
        return {"ok": True, "deleted": count}

    # ── undo ───────────────────────────────────────────────────────────────

    @app.post("/undo/last")
    async def undo_last():
        s = _require_state()
        msg = s.mover.undo_last()
        return {"ok": bool(msg), "message": msg or "Nothing to undo"}

    @app.post("/undo/group/{group_id}")
    async def undo_group(group_id: int):
        s = _require_state()
        count = s.mover.undo_group(group_id)
        return {"ok": True, "restored": count}

    @app.post("/undo/all")
    async def undo_all():
        s = _require_state()
        count = s.mover.undo_all()
        return {"ok": True, "restored": count}

    # ── stats ──────────────────────────────────────────────────────────────

    @app.get("/stats")
    async def stats():
        s = _require_state()
        return s.db.get_stats()

    # ── scan ───────────────────────────────────────────────────────────────

    @app.post("/api/scan")
    async def trigger_scan():
        s = _require_state()
        def _run():
            with _scan_lock:
                _scan_progress.update(done=False, message="Starting…", error="")
            s.scanner.scan(progress_cb=_progress_cb)
            s.engine.run(progress_cb=_progress_cb)
            with _scan_lock:
                _scan_progress.update(done=True, message="Complete")
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "message": "Scan started"}

    @app.get("/api/scan/status")
    async def scan_status_sse():
        def event_stream():
            while True:
                with _scan_lock:
                    data = dict(_scan_progress)
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("done"):
                    break
                time.sleep(0.4)
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # ── serve original image ───────────────────────────────────────────────

    @app.get("/photo/{photo_id}/original")
    async def serve_original(photo_id: int):
        s = _require_state()
        photo = s.db.get_photo(photo_id)
        if not photo or not photo.path.exists():
            raise HTTPException(404, "Photo not found")
        return FileResponse(str(photo.path))

    # ── reports ────────────────────────────────────────────────────────────

    @app.get("/report/html", response_class=HTMLResponse)
    async def report_html(request: Request):
        s = _require_state()
        groups = s.db.get_all_groups(limit=10000)
        stats = s.db.get_stats()
        enriched = []
        for g in groups:
            photos = s.db.get_photos_by_ids(g.member_ids)
            photo_map = {p.id: p for p in photos}
            enriched.append({"group": g, "photos": photo_map})
        return templates.TemplateResponse(
            request,
            "report.html",
            {"groups": enriched, "stats": stats, "folder": str(s.folder)},
        )

    @app.get("/report/csv")
    async def report_csv():
        s = _require_state()
        groups = s.db.get_all_groups(limit=10000)
        lines = ["group_id,reason,confidence,status,keeper_id,photo_id,filename,size_bytes,sharpness,brightness,shot_time"]
        for g in groups:
            photos = s.db.get_photos_by_ids(g.member_ids)
            for p in photos:
                lines.append(
                    f"{g.id},{g.reason.value},{g.confidence:.2f},{g.review_status.value},"
                    f"{g.keeper_id or ''},{p.id},{p.filename},{p.size_bytes},"
                    f"{p.sharpness or ''},{p.brightness or ''},{p.shot_time or ''}"
                )
        return PlainTextResponse(
            "\n".join(lines),
            headers={"Content-Disposition": "attachment; filename=dedup_report.csv"},
        )

    @app.get("/report/json")
    async def report_json():
        s = _require_state()
        groups = s.db.get_all_groups(limit=10000)
        out = []
        for g in groups:
            photos = s.db.get_photos_by_ids(g.member_ids)
            out.append({
                "group_id": g.id, "reason": g.reason.value,
                "confidence": g.confidence, "status": g.review_status.value,
                "keeper_id": g.keeper_id, "notes": g.notes,
                "photos": [
                    {"id": p.id, "filename": p.filename, "path": str(p.path),
                     "size_bytes": p.size_bytes, "sharpness": p.sharpness,
                     "brightness": p.brightness,
                     "shot_time": str(p.shot_time) if p.shot_time else None,
                     "resolution": p.resolution_str}
                    for p in photos
                ],
            })
        return JSONResponse(out)

    # ── RAW → JPEG export (independent — no active scan required) ───────────

    @app.get("/export", response_class=HTMLResponse)
    async def export_page(request: Request):
        s = _get_state()
        current_folder = str(s.folder) if s else ""
        suggested = str(Path(current_folder).parent / "Exported_JPEGs") if current_folder else ""
        return templates.TemplateResponse(
            request, "export.html",
            {"suggested_dest": suggested, "current_folder": current_folder},
        )

    class ExportBody(BaseModel):
        src: str = ""           # source folder (independent of scanned folder)
        scope: str = "all"      # "all" | "raw"
        dest: str = ""
        quality: int = 90
        preserve_structure: bool = True

    @app.post("/api/export")
    async def start_export(body: ExportBody):
        if not body.src.strip():
            raise HTTPException(400, "src (source folder) is required")
        if not body.dest.strip():
            raise HTTPException(400, "dest is required")
        if body.quality < 1 or body.quality > 100:
            raise HTTPException(400, "quality must be 1-100")

        src_folder = Path(body.src.strip()).expanduser().resolve()
        if not src_folder.is_dir():
            raise HTTPException(400, f"Source folder not found: {src_folder}")

        dest = Path(body.dest.strip()).expanduser().resolve()

        def _run_export():
            from config import IMAGE_EXTENSIONS, RAW_EXTENSIONS
            from thumbnail import _open_as_pil

            with _export_lock:
                _export_progress.update(
                    current=0, total=0, message="Gathering files…",
                    done=False, error="", dest=str(dest)
                )

            try:
                dest.mkdir(parents=True, exist_ok=True)

                # Discover files directly from source folder — no DB needed
                scope = body.scope
                exts = RAW_EXTENSIONS if scope == "raw" else IMAGE_EXTENSIONS
                photo_paths = sorted(
                    p for p in src_folder.rglob("*")
                    if p.is_file() and p.suffix.lower() in exts
                )

                total = len(photo_paths)
                with _export_lock:
                    _export_progress.update(total=total, message=f"Exporting {total} photos…")

                done = 0
                for photo_path in photo_paths:
                    try:
                        if body.preserve_structure:
                            try:
                                rel_dir = photo_path.parent.relative_to(src_folder)
                            except ValueError:
                                rel_dir = Path()
                            out_dir = dest / rel_dir
                        else:
                            out_dir = dest

                        out_dir.mkdir(parents=True, exist_ok=True)
                        out_name = photo_path.stem + ".jpg"
                        out_path = out_dir / out_name
                        counter = 1
                        while out_path.exists():
                            counter += 1
                            out_path = out_dir / f"{photo_path.stem}_{counter}.jpg"

                        img = _open_as_pil(photo_path)
                        if img is not None:
                            with img:
                                img.save(str(out_path), format="JPEG",
                                         quality=body.quality, optimize=True)
                    except Exception as exc:
                        logger.warning("Export skipped %s: %s", photo_path, exc)

                    done += 1
                    with _export_lock:
                        _export_progress.update(
                            current=done,
                            message=f"Exported {done}/{total}: {photo_path.name}",
                        )

                with _export_lock:
                    _export_progress.update(done=True, message=f"Done — {done} files exported")

            except Exception as exc:
                logger.exception("Export failed: %s", exc)
                with _export_lock:
                    _export_progress.update(done=True, error=str(exc),
                                            message=f"Export failed: {exc}")

        threading.Thread(target=_run_export, daemon=True).start()
        return {"ok": True}

    @app.get("/api/export/status")
    async def export_status_sse():
        def event_stream():
            while True:
                with _export_lock:
                    data = dict(_export_progress)
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("done"):
                    break
                time.sleep(0.5)
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # ── initial scan if folder provided at startup ─────────────────────────

    if initial_folder:
        t = threading.Thread(
            target=_set_folder_and_scan, args=(initial_folder,), daemon=True
        )
        t.start()

    return app
