# Photo Deduplicator

An offline, local desktop web application that finds and removes duplicate and low-quality photos from a downloaded Google Photos album.

**Nothing is ever deleted automatically.** Every decision goes through an interactive browser-based review. Files move in stages — original → `_permanent_delete/` → manually confirmed delete — and every step is reversible until the final confirmation.

No AI license. No cloud. No telemetry. No external APIs. Everything runs on your machine.

---

## Why This Exists

Google Photos storage fills up fast. The biggest culprits are:

- **Burst shots** — your camera fires 5–10 near-identical frames; you only want the sharpest one
- **Exact duplicates** — the same file backed up from multiple devices or shared via WhatsApp
- **Blurry / dark photos** — motion blur, accidental shots, poorly lit frames
- **Screenshots** — years of app screenshots that were never meant to be kept
- **Near-duplicates** — you photographed the same thing twice on different occasions

---

## How It Works

```
Downloaded Album
       │
       ▼
┌──────────────────────────────────────────────────────┐
│ Pass 1 — Exact Duplicates  (MD5 hash)                │
│ Pass 2 — Burst Shots       (EXIF time + pHash + sharpness) │
│ Pass 3 — Quality Cull      (blurry / dark / overexposed)   │
│ Pass 4 — Screenshots       (filename + EXIF + resolution)  │
│ Pass 5 — Near-Duplicates   (BK-tree pHash, cross-session)  │
└──────────────────────────────────────────────────────┘
       │
       ▼  nothing moves yet
       │
  http://localhost:8080  ← interactive review in your browser
       │
       ▼  you click "Approve Delete"
       │
  _permanent_delete/   ← staging area, still on disk
       │
       ▼  you click "Delete Permanently"
       │
  file unlinked
```

Each pass only sees photos not already flagged by a prior pass — no file appears in two groups.

---

## Architecture

The project is split into focused modules:

| File | Responsibility |
|---|---|
| `app.py` | Entry point, CLI argument parsing, mode dispatch |
| `config.py` | All tunable defaults in one place |
| `models.py` | `PhotoRecord`, `DuplicateGroup`, `MoveRecord`, enums |
| `database.py` | SQLite schema, CRUD, caching layer |
| `scanner.py` | Parallel file discovery and per-photo analysis |
| `duplicate_engine.py` | Five detection passes + BK-tree |
| `thumbnail.py` | Cached JPEG thumbnails (files, not base64) |
| `mover.py` | Two-phase approval workflow + full undo |
| `review_server.py` | FastAPI routes, SSE scan progress |
| `templates/` | Jinja2 HTML templates |
| `static/` | CSS (dark/light mode) + vanilla JS (no CDN) |

### Database as cache

All analysis results — MD5, pHash, sharpness, brightness, EXIF, thumbnails, review decisions, move history — live in `review.db` (SQLite) next to your album folder. A second scan only re-analyses files whose size or modification time changed. On a 5,000-photo album, a second scan typically completes in seconds.

### BK-tree near-duplicate detection

The near-duplicates pass uses a Burkhard-Keller tree over pHash values, reducing the search from O(n²) to O(n log n). For 10,000 photos with an 8-bit Hamming threshold this is the difference between ~10 minutes and ~5 seconds.

### Parallel analysis

MD5, pHash, sharpness, brightness, and thumbnail generation all run in a `ProcessPoolExecutor` using all available CPU cores (auto-detected, configurable). Worker functions are at module level so they pickle cleanly.

---

## Requirements

- Python 3.9 or later
- macOS or Windows

### Install

```bash
pip install -r requirements.txt
```

| Package | Purpose |
|---|---|
| `Pillow` | Image decoding, thumbnail generation |
| `imagehash` | Perceptual hash (pHash) computation |
| `opencv-python` | Laplacian sharpness + brightness measurement |
| `exifread` | EXIF metadata (timestamps, camera, GPS) |
| `fastapi` | Web server framework |
| `uvicorn` | ASGI server |
| `jinja2` | HTML template rendering |
| `pydantic` | Request/response validation |

---

## Quickstart

### Step 1 — Download your album from Google Photos

1. Open [Google Photos](https://photos.google.com)
2. Select an album (or all photos)
3. Click the three-dot menu → **Download**
4. Extract the zip to a local folder, e.g. `~/Downloads/MyAlbum`

### Step 2 — Launch the web app

```bash
python app.py --folder ~/Downloads/MyAlbum --review
```

This will:
1. Scan and analyse all photos (parallel, cached after first run)
2. Run all five detection passes
3. Start `http://localhost:8080` and open it in your browser automatically

### Step 3 — Review groups in the browser

The web UI shows every detected group with:
- Thumbnails of all photos
- **KEEP** highlighted in green, **TRASH** in red
- Sharpness score, file size, resolution, EXIF data, GPS
- Confidence score (0–100%) coloured green / amber / red
- Reason the group was flagged

### Step 4 — Approve what you agree with

Click **Approve Delete** on each group you agree with. Files stay on disk — only the database status changes. Use **Change Keeper** to pick a different photo to keep if the auto-selection is wrong. Use **Skip** to leave a group for later.

### Step 5 — Process approved groups

Click **Process Approved** in the sidebar (or `POST /process`). All approved photos move to `_permanent_delete/` next to your album folder. Still reversible — use Undo to bring them back.

### Step 6 — Permanently delete

Open `_permanent_delete/`, review the files manually, then click **Delete Permanently** in the web UI. This is the only step that actually frees disk space.

### Step 7 — Remove from Google Photos

Once you're satisfied:
1. In Google Photos, select the corresponding photos
2. Move them to the Google Photos Trash
3. Empty the Trash to reclaim storage quota

---

## Project Layout (at runtime)

```
~/Downloads/
├── MyAlbum/                   ← your photos (untouched until Step 5)
│   ├── IMG_3201.jpg
│   └── ...
├── review.db                  ← SQLite database (photos, groups, history)
├── _permanent_delete/         ← approved files staged here (still recoverable)
├── cache/
│   └── thumbnails/            ← cached JPEG thumbnails (never base64 in HTML)
├── logs/
├── dedup_report.csv           ← generated by --report mode
└── dedup_report.json
```

---

## All CLI Options

```
python app.py --folder PATH [mode] [options]

modes (mutually exclusive):
  --review          Scan if needed, then open web UI (default)
  --scan            Scan only, no web server
  --execute         Legacy CLI: scan + move files immediately
  --report          Generate CSV + JSON reports, no web server

scan options:
  --passes LIST     Comma-separated passes: exact,burst,blur,screenshot,similar
                    Default: all five

burst detection:
  --burst-window SEC    Max seconds between frames to consider a burst (default 3.0)
  --phash-threshold N   Hamming distance ≤ N = visually identical (default 10)

quality cull:
  --blur-threshold N    Laplacian variance below this = blurry (default 80.0)
  --brightness-min N    Mean pixel value below this = too dark (default 20.0)
  --brightness-max N    Mean pixel value above this = overexposed (default 235.0)

near-duplicates:
  --similar-threshold N pHash distance for cross-session dedup (default 8)

server:
  --host ADDR       Bind address (default 127.0.0.1)
  --port N          Port (default 8080)
  --no-browser      Don't auto-open browser on start

output:
  --verbose         Info-level logging
  --debug           Debug-level logging
  --quiet           Warnings only
```

---

## Web UI Features

### Dashboard
- Total photos, album size, potential space savings
- Groups by category with counts
- Workflow guide
- Keyboard shortcut reference

### Review List (`/review`)
- Filter by category, status, search filename/folder
- Sort by date, confidence, category
- Paginated (20 per page)
- Per-group approve / skip / detail actions inline

### Review Detail (`/review/{id}`)
- Large photo previews with click-to-zoom lightbox
- Full EXIF panel per photo: resolution, megapixels, sharpness, brightness, camera, GPS, capture date
- Confidence score with colour coding
- Change Keeper button per photo
- Previous / Next navigation
- All keyboard shortcuts active

### Reports
- `/report/html` — full visual report with thumbnails
- `/report/csv` — spreadsheet download
- `/report/json` — structured data download

---

## Keyboard Shortcuts

Active on the Review Detail page:

| Key | Action |
|---|---|
| `←` / `↑` | Previous group |
| `→` / `↓` / `Space` | Next group |
| `D` or `K` | Approve Delete |
| `S` | Skip group |
| `R` | Restore / undo group |
| `F` | Fullscreen lightbox |
| `Ctrl+Z` | Undo last action |
| `Esc` | Close lightbox |

---

## Approval Workflow In Detail

```
Group status:  pending → approved_delete → moved → deleted
                  │             │            │
                  │         (files stay    (files in
                  │          on disk)    _permanent_delete/)
                  │
                skip → skipped  (ignored, stays on disk)
```

- **Approve Delete** — status changes in DB; zero filesystem activity
- **Unapprove** — reverts to pending; zero filesystem activity
- **Process Approved** — files physically move to `_permanent_delete/`; reversible via Undo
- **Delete Permanently** — files unlinked from `_permanent_delete/`; not reversible

---

## Undo

Every file movement is recorded in `move_history`. Three undo levels:

| Action | Effect |
|---|---|
| **Undo Last** | Restores the single most recent moved file |
| **Undo Group** | Restores all files in a specific group |
| **Undo All** | Restores every file currently in `_permanent_delete/` |

Undo is only possible before permanent deletion. After `Delete Permanently`, the record remains but the file is gone.

---

## Confidence Score

Every duplicate group shows a 0–100% confidence score based on:

| Signal | Passes that use it |
|---|---|
| pHash Hamming distance | burst, similar |
| Time delta between frames | burst |
| Number of heuristics matched | screenshot |
| Distance from quality threshold | blurry, dark, overexposed |
| MD5 match | exact (always 100%) |

**Green (≥75%)** — high confidence, safe to approve without detailed review  
**Amber (50–74%)** — worth a quick look before approving  
**Red (<50%)** — review carefully; these are the most subjective detections

---

## Tuning Guide

### pHash threshold (burst + similar passes)

| Threshold | Meaning |
|---|---|
| 0–4 | Near-pixel-perfect — same shot |
| 5–10 | Very similar — typical burst frames **(burst default: 10)** |
| 5–8 | Similar scene — conservative cross-session **(similar default: 8)** |
| 11–15 | Possible lighting/angle variation — risk of false positives |
| >15 | Not recommended |

### Blur threshold (Laplacian variance)

| Threshold | Typical result |
|---|---|
| 30–50 | Only severely out-of-focus shots |
| 80 | Noticeably blurry **(default)** |
| 150 | Mild motion blur |
| 300+ | May flag intentional soft-focus portraits |

### Burst window

| Window | Typical result |
|---|---|
| 1s | Only camera burst modes (rapid fire) |
| 3s | Burst + quick re-shots **(default)** |
| 5s | More aggressive — review carefully |

---

## Legacy CLI Mode

The original `dedupe.py` single-file tool in `../photo-dedup/` still works unchanged. The new `app.py --execute` flag preserves backwards compatibility with the old workflow:

```bash
# Old tool (still works)
python ../photo-dedup/dedupe.py --folder ~/Downloads/MyAlbum --execute

# New tool, legacy mode (same behaviour)
python app.py --folder ~/Downloads/MyAlbum --execute
```

The legacy mode skips the web UI, runs all passes, approves everything automatically, and moves files to `_permanent_delete/` in one shot. Use it only if you've already reviewed the results and trust the defaults.

---

## Safety Guarantees

- **Nothing moves during a scan** — analysis is read-only until you explicitly approve
- **Two-phase deletion** — approval and physical movement are separate steps
- **Full undo** — every move is logged in SQLite; restore any file until permanent deletion
- **No auto-delete** — permanent deletion requires an explicit button click in the UI
- **Conflict-safe moves** — if a filename already exists in `_permanent_delete/`, a unique suffix is added automatically
- **Offline-first** — zero network requests at any point; all CSS and JS are local files

---

## Limitations

- **HEIC files** — OpenCV cannot decode HEIC natively. Sharpness and brightness analysis are skipped for HEIC; all other passes (MD5, pHash via Pillow, EXIF, screenshots) still work. Install `pillow-heif` for full support: `pip install pillow-heif`
- **No EXIF timestamps** — photos without `DateTimeOriginal` are skipped in the burst pass but are still processed by all other passes
- **Large albums (>20k photos)** — the `similar` pass (cross-session near-duplicates) may take several minutes on first scan even with the BK-tree. Run `--passes exact,burst,blur,screenshot` first; add `similar` as a separate step
- **Google Photos does not provide a deletion API** — the final step (removing from Google Photos) is always manual
- **Single user** — the web server binds to `127.0.0.1` only and has no authentication. Do not expose it on a network interface
