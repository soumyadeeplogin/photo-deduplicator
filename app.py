"""
Photo Deduplicator — main entry point.

No arguments needed:
  python app.py
      Opens http://localhost:8080 where you pick your folder in the browser.

With a folder pre-selected (power users / scripting):
  python app.py --folder ~/Photos/MyAlbum --review
  python app.py --folder ~/Photos/MyAlbum --scan
  python app.py --folder ~/Photos/MyAlbum --execute   (legacy CLI)
  python app.py --folder ~/Photos/MyAlbum --report
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import webbrowser
from pathlib import Path

from config import Config
from database import Database
from duplicate_engine import DuplicateEngine
from scanner import Scanner


def _configure_logging(verbose: bool, debug: bool) -> None:
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    # File handler — logs/ next to wherever the app is launched from
    log_dir = Path.cwd() / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "app.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    # File always logs at INFO or above regardless of console verbosity
    file_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)

    for noisy in ("PIL", "exifread", "urllib3", "asyncio", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info("Log file: %s", log_file)


def _build_config(args: argparse.Namespace) -> Config:
    return Config(
        passes=[p.strip() for p in args.passes.split(",") if p.strip()],
        burst_window_sec=args.burst_window,
        phash_threshold=args.phash_threshold,
        blur_threshold=args.blur_threshold,
        highlight_threshold=args.highlight_threshold,
        shadow_threshold=args.shadow_threshold,
        similar_threshold=args.similar_threshold,
        host=args.host,
        port=args.port,
    )


def _progress(current: int, total: int, message: str) -> None:
    if total > 0:
        pct = int(current / total * 100)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"\r  [{bar}] {pct:3d}%  {message}", end="", flush=True)
    else:
        print(f"\r  {message}", end="", flush=True)


def run_scan(folder: Path, db: Database, cfg: Config) -> None:
    print("=" * 60)
    print(f"  Scanning: {folder}")
    print("=" * 60)

    scanner = Scanner(folder, db, cfg)
    analysed = scanner.scan(progress_cb=_progress)
    print()

    print(f"\n  Running duplicate detection…")
    engine = DuplicateEngine(db, cfg)
    groups = engine.run(progress_cb=_progress)
    print()
    if analysed > 0:
        print(f"  Analysed {analysed} new/changed photos.")
    print(f"  Created {groups} duplicate groups.")

    stats = db.get_stats()
    print(f"\n  Total photos : {stats['total_photos']}")
    print(f"  Total size   : {_fmt_size(stats['total_size_bytes'])}")
    print(f"  Groups found : {sum(stats['by_reason'].values())}")
    print(f"  Space to save: {_fmt_size(stats['flagged_size_bytes'])}")


def run_legacy_cli(folder: Path, db: Database, cfg: Config, execute: bool) -> None:
    run_scan(folder, db, cfg)
    if not execute:
        print("\n  Dry run complete — use --execute to move files, or --review for the web UI.")
        return

    from mover import Mover
    perm_dir = folder.parent / cfg.permanent_delete_dir_name
    mover = Mover(db, perm_dir)
    groups = db.get_all_groups(limit=100000)
    approved = 0
    for g in groups:
        approved += mover.approve_group(g.id)
    print(f"\n  Approved {approved} files for deletion.")
    moved = mover.process_approved(progress_cb=_progress)
    print(f"\n  Moved {moved} files to {perm_dir}")


def run_report(folder: Path, db: Database) -> None:
    import json as _json
    out_dir = folder.parent
    all_groups = db.get_all_groups(limit=100000)

    csv_path = out_dir / "dedup_report.csv"
    lines = ["group_id,reason,confidence,status,keeper_id,photo_id,filename,size_bytes,sharpness,shot_time"]
    for g in all_groups:
        photos = db.get_photos_by_ids(g.member_ids)
        for p in photos:
            lines.append(
                f"{g.id},{g.reason.value},{g.confidence:.2f},{g.review_status.value},"
                f"{g.keeper_id or ''},{p.id},{p.filename},{p.size_bytes},"
                f"{p.sharpness or ''},{p.shot_time or ''}"
            )
    csv_path.write_text("\n".join(lines))
    print(f"  CSV report: {csv_path}")

    json_path = out_dir / "dedup_report.json"
    out = []
    for g in all_groups:
        photos = db.get_photos_by_ids(g.member_ids)
        out.append({
            "group_id": g.id, "reason": g.reason.value,
            "confidence": g.confidence, "notes": g.notes,
            "photos": [{"id": p.id, "filename": p.filename, "path": str(p.path)} for p in photos],
        })
    json_path.write_text(_json.dumps(out, indent=2))
    print(f"  JSON report: {json_path}")


def _fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def main() -> None:
    p = argparse.ArgumentParser(
        description="Photo Deduplicator — offline duplicate photo manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # --folder is now optional; omitting it launches the folder-picker UI
    p.add_argument("--folder", default=None,
                   help="Path to the photo folder (optional — pick in browser if omitted)")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--review", action="store_true",
                      help="Scan if needed, then launch interactive web UI (default)")
    mode.add_argument("--scan", action="store_true",
                      help="Scan only, no web server (requires --folder)")
    mode.add_argument("--execute", action="store_true",
                      help="Legacy CLI: scan + move files (requires --folder)")
    mode.add_argument("--report", action="store_true",
                      help="Generate CSV/JSON reports, no web server (requires --folder)")

    p.add_argument("--passes", default="exact,burst,blur,screenshot,similar")
    p.add_argument("--burst-window",      type=float, default=3.0,   metavar="SEC")
    p.add_argument("--phash-threshold",   type=int,   default=10,    metavar="N")
    p.add_argument("--blur-threshold",      type=float, default=80.0, metavar="N")
    p.add_argument("--highlight-threshold", type=float, default=5.0,  metavar="N",
                   help="%%pixels>250 above this → overexposed (default 5.0)")
    p.add_argument("--shadow-threshold",    type=float, default=15.0, metavar="N",
                   help="%%pixels<5 above this → too dark (default 15.0)")
    p.add_argument("--similar-threshold", type=int,   default=8,     metavar="N")
    p.add_argument("--host",       default="127.0.0.1")
    p.add_argument("--port",       type=int, default=8080)
    p.add_argument("--no-browser", action="store_true",
                   help="Don't auto-open browser when starting web UI")
    p.add_argument("--verbose",    action="store_true")
    p.add_argument("--debug",      action="store_true")
    p.add_argument("--quiet",      action="store_true")

    args = p.parse_args()
    _configure_logging(args.verbose or not args.quiet, args.debug)
    cfg = _build_config(args)

    # ── CLI-only modes require --folder ──────────────────────────────────────
    if args.scan or args.execute or args.report:
        if not args.folder:
            print("Error: --folder is required with --scan / --execute / --report",
                  file=sys.stderr)
            sys.exit(1)
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            print(f"Error: folder not found: {folder}", file=sys.stderr)
            sys.exit(1)
        db_path = folder.parent / cfg.db_name
        db = Database(db_path)
        db.connect()
        try:
            if args.scan:
                run_scan(folder, db, cfg)
            elif args.execute:
                run_legacy_cli(folder, db, cfg, execute=True)
            elif args.report:
                run_scan(folder, db, cfg)
                run_report(folder, db)
        finally:
            db.close()
        return

    # ── Web UI mode (default) ─────────────────────────────────────────────────
    # If --folder was given, do an initial scan before opening the browser.
    # If not given, start the server immediately — user picks folder in the UI.
    from review_server import create_app
    import uvicorn

    initial_folder: Path | None = None
    if args.folder:
        initial_folder = Path(args.folder).expanduser().resolve()
        if not initial_folder.is_dir():
            print(f"Error: folder not found: {initial_folder}", file=sys.stderr)
            sys.exit(1)

    app = create_app(initial_folder=initial_folder, cfg=cfg)
    url = f"http://{args.host}:{args.port}"

    if initial_folder:
        print(f"\n  Folder  : {initial_folder}")
    print(f"  Web UI  : {url}")
    print(f"  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        import threading
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning" if not args.debug else "debug",
    )


if __name__ == "__main__":
    main()
