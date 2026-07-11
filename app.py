"""
Photo Deduplicator — main entry point.

Modes:
  python app.py --folder ~/Photos/MyAlbum --review
      Scan (if needed) then launch the web UI at http://localhost:8080

  python app.py --folder ~/Photos/MyAlbum --scan
      Scan only, no web server (background indexing)

  python app.py --folder ~/Photos/MyAlbum --execute
      Legacy CLI mode: scan + move files directly (backwards compatible)

  python app.py --folder ~/Photos/MyAlbum --report
      Generate static HTML/CSV/JSON reports without launching the server
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from pathlib import Path

from config import Config, default_config
from database import Database
from duplicate_engine import DuplicateEngine
from scanner import Scanner


def _configure_logging(verbose: bool, debug: bool) -> None:
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy third-party loggers
    for noisy in ("PIL", "exifread", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _build_config(args: argparse.Namespace) -> Config:
    cfg = Config(
        passes=[p.strip() for p in args.passes.split(",") if p.strip()],
        burst_window_sec=args.burst_window,
        phash_threshold=args.phash_threshold,
        blur_threshold=args.blur_threshold,
        brightness_min=args.brightness_min,
        brightness_max=args.brightness_max,
        similar_threshold=args.similar_threshold,
        host=args.host,
        port=args.port,
    )
    return cfg


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
    """Backwards-compatible mode: scan + optionally move files directly."""
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
    print(f"  Review {perm_dir} then run --delete to permanently remove them.")


def run_report(folder: Path, db: Database) -> None:
    from datetime import datetime

    out_dir = folder.parent
    all_groups = db.get_all_groups(limit=100000)
    stats = db.get_stats()

    # CSV
    csv_path = out_dir / "dedup_report.csv"
    lines = ["group_id,reason,confidence,status,keeper_id,photo_id,filename,size_bytes,sharpness,shot_time"]
    for g in all_groups:
        photos = db.get_photos_by_ids(g.member_ids)
        for p in photos:
            lines.append(
                f"{g.id},{g.reason.value},{g.confidence:.2f},{g.review_status.value},"
                f"{g.keeper_id or ''},"
                f"{p.id},{p.filename},{p.size_bytes},"
                f"{p.sharpness or ''},"
                f"{p.shot_time or ''}"
            )
    csv_path.write_text("\n".join(lines))
    print(f"  CSV report: {csv_path}")

    # JSON
    import json
    json_path = out_dir / "dedup_report.json"
    out = []
    for g in all_groups:
        photos = db.get_photos_by_ids(g.member_ids)
        out.append({
            "group_id": g.id,
            "reason": g.reason.value,
            "confidence": g.confidence,
            "notes": g.notes,
            "photos": [
                {"id": p.id, "filename": p.filename, "path": str(p.path)}
                for p in photos
            ],
        })
    json_path.write_text(json.dumps(out, indent=2))
    print(f"  JSON report: {json_path}")
    print(f"\n  Stats: {stats}")


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

    p.add_argument("--folder", required=True, help="Path to the downloaded album folder")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--review", action="store_true",
                      help="Scan if needed, then launch interactive web UI (default)")
    mode.add_argument("--scan", action="store_true",
                      help="Scan only, do not start web server")
    mode.add_argument("--execute", action="store_true",
                      help="Legacy CLI: scan + move files (backwards compatible)")
    mode.add_argument("--report", action="store_true",
                      help="Generate HTML/CSV/JSON reports without web server")

    p.add_argument("--passes", default="exact,burst,blur,screenshot,similar",
                   help="Comma-separated detection passes")
    p.add_argument("--burst-window", type=float, default=3.0, metavar="SEC")
    p.add_argument("--phash-threshold", type=int, default=10, metavar="N")
    p.add_argument("--blur-threshold", type=float, default=80.0, metavar="N")
    p.add_argument("--brightness-min", type=float, default=20.0, metavar="N")
    p.add_argument("--brightness-max", type=float, default=235.0, metavar="N")
    p.add_argument("--similar-threshold", type=int, default=8, metavar="N")

    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--no-browser", action="store_true",
                   help="Don't auto-open browser when starting web UI")
    p.add_argument("--rescan", action="store_true",
                   help="Force re-analyse all photos (ignore cache)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--quiet", action="store_true")

    args = p.parse_args()
    _configure_logging(args.verbose or not args.quiet, args.debug)

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"Error: folder not found: {folder}", file=sys.stderr)
        sys.exit(1)

    cfg = _build_config(args)
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

        else:
            # Default: --review (also the fallback with no mode flag)
            run_scan(folder, db, cfg)

            from review_server import create_app
            import uvicorn

            app = create_app(folder, db, cfg)
            url = f"http://{args.host}:{args.port}"
            print(f"\n  Starting web UI at {url}")
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
    finally:
        db.close()


if __name__ == "__main__":
    main()
