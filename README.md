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
| `review_server.py` | FastAPI routes, folder picker, SSE scan progress |
| `templates/` | Jinja2 HTML templates |
| `static/` | CSS (dark/light mode) + vanilla JS (no CDN) |

---

## Requirements

- Python 3.9 or later
- Works on macOS, Windows, and Linux
- No database installation needed — SQLite is built into Python

---

## Setup (First Time)

### Step 1 — Get the code

```bash
git clone https://github.com/soumyadeeplogin/photo-deduplicator.git
cd photo-deduplicator
```

Or download the ZIP from GitHub and extract it.

---

### Step 2 — Create a virtual environment

A virtual environment keeps the app's dependencies isolated from the rest of your Python installation.

#### macOS / Linux

```bash
python3 -m venv .venv
```

#### Windows (Command Prompt)

```cmd
python -m venv .venv
```

#### Windows (PowerShell)

```powershell
python -m venv .venv
```

---

### Step 3 — Activate the virtual environment

You must activate the venv every time you open a new terminal before running the app.

#### macOS / Linux

```bash
source .venv/bin/activate
```

Your prompt changes to show `(.venv)` — that means it's active.

#### Windows (Command Prompt)

```cmd
.venv\Scripts\activate.bat
```

#### Windows (PowerShell)

```powershell
.venv\Scripts\Activate.ps1
```

> **PowerShell note:** If you see an error about execution policy, run this once:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> Then try activating again.

---

### Step 4 — Install dependencies

With the venv active:

```bash
pip install -r requirements.txt
```

If pip tries a corporate proxy and fails, force it to use PyPI directly:

```bash
pip install -r requirements.txt --index-url https://pypi.org/simple/
```

---

### Step 5 — Run the app

```bash
python app.py
```

This opens `http://localhost:8080` in your browser automatically, showing the **folder picker page**.

---

### Step 6 — Pick your photo folder in the browser

On the setup page you can:
- **Paste the path** directly into the text box
- **Click Browse** to navigate your filesystem folder by folder
- **Click a recent folder** if you've used the app before

Once you confirm, the scan starts automatically and a progress bar shows the status. When it finishes you land on the dashboard.

**Path examples:**

| OS | Example path |
|---|---|
| Windows | `C:\Users\YourName\Pictures\Goa2023` |
| macOS | `/Users/yourname/Downloads/Goa2023` |
| Linux | `/home/yourname/Downloads/Goa2023` |

---

### How to deactivate the virtual environment

When you're done and want to go back to normal:

```bash
deactivate
```

This works the same on macOS, Windows, and Linux.

---

## Quickstart (Returning Users)

```bash
cd photo-deduplicator

# Activate venv (do this every time you open a new terminal)
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate.bat         # Windows CMD
.venv\Scripts\Activate.ps1         # Windows PowerShell

# Run
python app.py
```

Then go to `http://localhost:8080` and pick your folder.

---

## Download Your Album from Google Photos

1. Open [Google Photos](https://photos.google.com)
2. Select an album (or all photos using the Select button)
3. Click the three-dot menu → **Download**
4. Extract the downloaded zip to a folder on your computer
5. Point the app at that folder

---

## Workflow (step by step)

| Step | What you do | What happens |
|---|---|---|
| 1 | Run `python app.py` | Browser opens at localhost:8080 |
| 2 | Browse to your photo folder, click Start | Photos scanned, groups detected |
| 3 | Go to **Review Groups** | See every group with thumbnails + reasons |
| 4 | Click **Approve Delete** on groups you agree with | DB status changes, files untouched |
| 5 | Click **Process Approved** in sidebar | Files move to `_permanent_delete/` |
| 6 | Review `_permanent_delete/` manually | Open folder in Explorer/Finder |
| 7 | Click **Delete Permanently** in UI | Files removed from disk |
| 8 | In Google Photos, select same photos | Move to Trash, empty Trash |

---

## Project Layout (at runtime)

```
your-photos-parent-folder/
├── MyAlbum/                   ← your photos (untouched until Step 5)
│   ├── IMG_3201.jpg
│   └── ...
├── review.db                  ← SQLite database (auto-created, no install needed)
├── _permanent_delete/         ← approved files staged here (still recoverable)
├── cache/
│   └── thumbnails/            ← cached JPEG thumbnails
├── dedup_report.csv           ← generated by --report mode
└── dedup_report.json
```

---

## All CLI Options

`--folder` is now optional — omit it to use the in-browser folder picker.

```
python app.py [--folder PATH] [mode] [options]

folder:
  --folder PATH     Pre-select a folder (optional — pick in browser if omitted)

modes (mutually exclusive):
  --review          Scan if needed, launch web UI (default)
  --scan            Scan only, no web server (requires --folder)
  --execute         Legacy CLI: scan + move files (requires --folder)
  --report          Generate CSV/JSON reports, no web server (requires --folder)

scan options:
  --passes LIST     Comma-separated passes: exact,burst,blur,screenshot,similar
                    Default: all five

burst detection:
  --burst-window SEC    Max seconds between frames for a burst (default 3.0)
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

### Folder Picker (`/setup`)
- Type or paste any folder path
- Browse filesystem folder by folder
- Recent folders remembered across sessions (stored in browser localStorage)
- Real-time scan progress bar

### Dashboard (`/`)
- Total photos, album size, potential space savings
- Groups by category with counts
- Workflow guide and keyboard shortcut reference

### Review List (`/review`)
- Filter by category, status, search filename/folder
- Sort by date, confidence, category
- Paginated (20 per page)
- Per-group approve / skip / detail actions inline

### Review Detail (`/review/{id}`)
- Large photo previews with click-to-zoom lightbox
- Full EXIF panel: resolution, megapixels, sharpness, brightness, camera, GPS, capture date
- Confidence score (green/amber/red)
- Change Keeper button
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

| Action | Effect |
|---|---|
| **Undo Last** | Restores the single most recent moved file |
| **Undo Group** | Restores all files in a specific group |
| **Undo All** | Restores every file currently in `_permanent_delete/` |

---

## Confidence Score

| Score | Colour | Meaning |
|---|---|---|
| ≥ 75% | Green | High confidence — safe to approve |
| 50–74% | Amber | Worth a quick look |
| < 50% | Red | Review carefully |

---

## Tuning Guide

### pHash threshold

| Threshold | Meaning |
|---|---|
| 0–4 | Near-pixel-perfect — same shot |
| 5–10 | Very similar — typical burst frames **(burst default: 10)** |
| 5–8 | Conservative cross-session **(similar default: 8)** |
| > 15 | Risk of false positives — not recommended |

### Blur threshold (Laplacian variance)

| Threshold | Typical result |
|---|---|
| 30–50 | Only severely out-of-focus shots |
| 80 | Noticeably blurry **(default)** |
| 150 | Mild motion blur |
| 300+ | May flag soft-focus portraits |

---

## Safety Guarantees

- Nothing moves during scan — analysis is read-only until you explicitly approve
- Two-phase deletion — approval and physical movement are separate steps
- Full undo — every move is logged in SQLite until permanent deletion
- No auto-delete — permanent deletion requires an explicit button click
- Conflict-safe moves — duplicate filenames get a unique suffix automatically
- Offline-first — zero network requests; all CSS and JS are local files

---

## Legacy CLI Mode

```bash
# Skip the folder picker, scan directly (power users / scripting)
python app.py --folder ~/Downloads/MyAlbum --review

# Old single-file tool (still works unchanged)
python ../photo-dedup/dedupe.py --folder ~/Downloads/MyAlbum --execute
```

---

## Limitations

- **HEIC files** — OpenCV cannot decode HEIC natively. Sharpness/brightness are skipped; MD5, pHash, EXIF, and screenshot detection still work. Install `pillow-heif` for full support: `pip install pillow-heif --index-url https://pypi.org/simple/`
- **No EXIF timestamps** — photos without `DateTimeOriginal` are skipped in the burst pass but processed by all other passes
- **Large albums (>20k photos)** — the `similar` pass may take several minutes. Run `--passes exact,burst,blur,screenshot` first, add `similar` separately
- **Google Photos deletion API** — Google does not offer one; the final removal from Google Photos is always manual
- **Single user** — the server binds to `127.0.0.1` only. Do not expose it on a network interface
