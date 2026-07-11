"""
FastAPI review server.

Routes:
  GET  /                        → dashboard
  GET  /review                  → group list (filterable)
  GET  /review/{group_id}       → single group detail
  POST /review/{group_id}/approve       → approve delete
  POST /review/{group_id}/unapprove     → revert to pending
  POST /review/{group_id}/skip          → skip
  POST /review/{group_id}/keeper/{pid}  → change keeper
  POST /process                 → move approved → _permanent_delete/
  POST /delete                  → permanently delete (JSON body: {photo_ids: [...]})
  POST /undo/last               → undo last move
  POST /undo/group/{group_id}   → undo group
  POST /undo/all                → undo all
  GET  /stats                   → statistics JSON
  GET  /report/html             → static HTML report
  GET  /report/csv              → CSV download
  GET  /report/json             → JSON download
  GET  /cache/thumbnails/{fname}→ thumbnail images
  GET  /photo/{photo_id}/original → serve original image
  GET  /api/scan/status         → SSE progress stream
  POST /api/scan                → trigger rescan
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
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

# ── shared state ──────────────────────────────────────────────────────────────

_scan_progress: dict = {"current": 0, "total": 0, "message": "", "done": True}
_scan_lock = threading.Lock()


def _progress_cb(current: int, total: int, message: str) -> None:
    with _scan_lock:
        _scan_progress.update(current=current, total=total, message=message, done=False)


# ── app factory ───────────────────────────────────────────────────────────────

def create_app(
    folder: Path,
    db: Database,
    cfg: Config,
) -> FastAPI:
    app = FastAPI(title="Photo Deduplicator", docs_url=None, redoc_url=None)

    base_dir = Path(__file__).parent
    templates = Jinja2Templates(directory=str(base_dir / "templates"))
    cache_dir = folder.parent / cfg.cache_dir_name
    perm_dir = folder.parent / cfg.permanent_delete_dir_name
    thumb_dir = cache_dir / "thumbnails"

    scanner = Scanner(folder, db, cfg)
    engine = DuplicateEngine(db, cfg)
    mover = Mover(db, perm_dir)

    # Serve thumbnail files
    thumb_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/cache/thumbnails", StaticFiles(directory=str(thumb_dir)), name="thumbnails")

    # Serve static assets (CSS, JS)
    static_dir = base_dir / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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

    # ── dashboard ─────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        stats = db.get_stats()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"stats": stats, "folder": str(folder)},
        )

    # ── review list ───────────────────────────────────────────────────────

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
        offset = (page - 1) * per_page
        groups = db.get_all_groups(
            reason=reason, status=status, search=search,
            order_by=order, limit=per_page, offset=offset,
        )
        total = db.count_groups(reason=reason, status=status)
        total_pages = max(1, (total + per_page - 1) // per_page)

        # Attach photo records to each group
        enriched = []
        for g in groups:
            photos = db.get_photos_by_ids(g.member_ids)
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
                "filters": {
                    "reason": reason, "status": status,
                    "search": search, "order": order,
                },
                "stats": db.get_stats(),
            },
        )

    # ── group detail ──────────────────────────────────────────────────────

    @app.get("/review/{group_id}", response_class=HTMLResponse)
    async def review_detail(request: Request, group_id: int):
        group = db.get_group(group_id)
        if not group:
            raise HTTPException(404, "Group not found")
        photos = db.get_photos_by_ids(group.member_ids)
        photo_map = {p.id: p for p in photos}

        # previous / next group ids for navigation
        all_ids = [g.id for g in db.get_all_groups(limit=10000)]
        try:
            idx = all_ids.index(group_id)
            prev_id = all_ids[idx - 1] if idx > 0 else None
            next_id = all_ids[idx + 1] if idx < len(all_ids) - 1 else None
        except ValueError:
            prev_id = next_id = None

        return templates.TemplateResponse(
            request,
            "review_detail.html",
            {
                "group": group,
                "photos": photo_map,
                "prev_id": prev_id,
                "next_id": next_id,
            },
        )

    # ── review actions ────────────────────────────────────────────────────

    @app.post("/review/{group_id}/approve")
    async def approve(group_id: int):
        try:
            count = mover.approve_group(group_id)
            return {"ok": True, "approved": count}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/review/{group_id}/unapprove")
    async def unapprove(group_id: int):
        try:
            count = mover.unapprove_group(group_id)
            return {"ok": True, "restored": count}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/review/{group_id}/skip")
    async def skip(group_id: int):
        mover.skip_group(group_id)
        return {"ok": True}

    @app.post("/review/{group_id}/keeper/{photo_id}")
    async def change_keeper(group_id: int, photo_id: int):
        try:
            mover.change_keeper(group_id, photo_id)
            return {"ok": True}
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── process & delete ──────────────────────────────────────────────────

    @app.post("/process")
    async def process_approved():
        count = mover.process_approved()
        return {"ok": True, "moved": count}

    class DeleteBody(BaseModel):
        photo_ids: Optional[list[int]] = None

    @app.post("/delete")
    async def delete_permanent(body: DeleteBody):
        count = mover.permanent_delete(body.photo_ids)
        return {"ok": True, "deleted": count}

    # ── undo ──────────────────────────────────────────────────────────────

    @app.post("/undo/last")
    async def undo_last():
        msg = mover.undo_last()
        return {"ok": bool(msg), "message": msg or "Nothing to undo"}

    @app.post("/undo/group/{group_id}")
    async def undo_group(group_id: int):
        count = mover.undo_group(group_id)
        return {"ok": True, "restored": count}

    @app.post("/undo/all")
    async def undo_all():
        count = mover.undo_all()
        return {"ok": True, "restored": count}

    # ── stats ─────────────────────────────────────────────────────────────

    @app.get("/stats")
    async def stats():
        return db.get_stats()

    # ── scan ──────────────────────────────────────────────────────────────

    @app.post("/api/scan")
    async def trigger_scan():
        def _run():
            with _scan_lock:
                _scan_progress.update(done=False, message="Starting…")
            scanner.scan(progress_cb=_progress_cb)
            engine.run(progress_cb=_progress_cb)
            with _scan_lock:
                _scan_progress.update(done=True, message="Complete")

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {"ok": True, "message": "Scan started"}

    @app.get("/api/scan/status")
    async def scan_status_sse():
        """Server-Sent Events stream for scan progress."""
        def event_stream():
            while True:
                with _scan_lock:
                    data = dict(_scan_progress)
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("done"):
                    break
                time.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # ── serve original image ──────────────────────────────────────────────

    @app.get("/photo/{photo_id}/original")
    async def serve_original(photo_id: int):
        photo = db.get_photo(photo_id)
        if not photo or not photo.path.exists():
            raise HTTPException(404, "Photo not found")
        return FileResponse(str(photo.path))

    # ── reports ───────────────────────────────────────────────────────────

    @app.get("/report/html", response_class=HTMLResponse)
    async def report_html(request: Request):
        groups = db.get_all_groups(limit=10000)
        all_photos = db.get_all_photos()
        stats = db.get_stats()
        enriched = []
        for g in groups:
            photos = db.get_photos_by_ids(g.member_ids)
            photo_map = {p.id: p for p in photos}
            enriched.append({"group": g, "photos": photo_map})
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "groups": enriched,
                "stats": stats,
                "folder": str(folder),
            },
        )

    @app.get("/report/csv")
    async def report_csv():
        groups = db.get_all_groups(limit=10000)
        lines = ["group_id,reason,confidence,status,keeper_id,photo_id,filename,size_bytes,sharpness,brightness,shot_time"]
        for g in groups:
            photos = db.get_photos_by_ids(g.member_ids)
            for p in photos:
                lines.append(
                    f"{g.id},{g.reason.value},{g.confidence:.2f},{g.review_status.value},"
                    f"{g.keeper_id or ''},"
                    f"{p.id},{p.filename},{p.size_bytes},"
                    f"{p.sharpness or ''},"
                    f"{p.brightness or ''},"
                    f"{p.shot_time or ''}"
                )
        content = "\n".join(lines)
        return PlainTextResponse(
            content,
            headers={"Content-Disposition": "attachment; filename=dedup_report.csv"},
        )

    @app.get("/report/json")
    async def report_json():
        groups = db.get_all_groups(limit=10000)
        out = []
        for g in groups:
            photos = db.get_photos_by_ids(g.member_ids)
            out.append({
                "group_id": g.id,
                "reason": g.reason.value,
                "confidence": g.confidence,
                "status": g.review_status.value,
                "keeper_id": g.keeper_id,
                "notes": g.notes,
                "photos": [
                    {
                        "id": p.id,
                        "filename": p.filename,
                        "path": str(p.path),
                        "size_bytes": p.size_bytes,
                        "sharpness": p.sharpness,
                        "brightness": p.brightness,
                        "shot_time": str(p.shot_time) if p.shot_time else None,
                        "resolution": p.resolution_str,
                    }
                    for p in photos
                ],
            })
        return JSONResponse(out)

    return app
