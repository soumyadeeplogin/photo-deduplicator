/* Photo Deduplicator — client-side logic */

// ── theme ─────────────────────────────────────────────────────────────────────
const THEME_KEY = "pd-theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem(THEME_KEY, theme);
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.textContent = theme === "dark" ? "☀ Light" : "☾ Dark";
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "light";
  applyTheme(current === "dark" ? "light" : "dark");
}

(function () {
  const saved = localStorage.getItem(THEME_KEY) ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  applyTheme(saved);
})();

// ── toast ─────────────────────────────────────────────────────────────────────
function toast(message, type = "info", duration = 3000) {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    document.body.appendChild(container);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ── API helpers ───────────────────────────────────────────────────────────────
async function api(method, url, body = null) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || res.statusText);
  }
  return res.json();
}

// ── review actions ────────────────────────────────────────────────────────────
async function approveGroup(groupId) {
  try {
    const data = await api("POST", `/review/${groupId}/approve`);
    toast(`Approved ${data.approved} photo(s) for deletion`, "success");
    updateGroupStatus(groupId, "approved_delete");
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

async function unapproveGroup(groupId) {
  try {
    const data = await api("POST", `/review/${groupId}/unapprove`);
    toast(`Restored ${data.restored} photo(s) to pending`, "info");
    updateGroupStatus(groupId, "pending");
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

async function skipGroup(groupId) {
  try {
    await api("POST", `/review/${groupId}/skip`);
    toast("Group skipped", "info");
    updateGroupStatus(groupId, "skipped");
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

async function changeKeeper(groupId, photoId) {
  try {
    await api("POST", `/review/${groupId}/keeper/${photoId}`);
    toast("Keeper updated — reload to see changes", "success");
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

function updateGroupStatus(groupId, status) {
  // Update status chip in-place without full reload
  const chip = document.querySelector(`[data-group-id="${groupId}"] .status-chip`);
  if (chip) {
    chip.className = `status-chip status-${status}`;
    chip.textContent = status.replace("_", " ");
  }
  const card = document.querySelector(`[data-group-id="${groupId}"]`);
  if (card) card.dataset.status = status;
}

// ── process approved ──────────────────────────────────────────────────────────
async function processApproved() {
  if (!confirm("Move all approved photos to _permanent_delete/? This cannot be undone without using Undo.")) return;
  try {
    const data = await api("POST", "/process");
    toast(`Moved ${data.moved} file(s) to _permanent_delete/`, "success");
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

// ── undo ──────────────────────────────────────────────────────────────────────
async function undoLast() {
  try {
    const data = await api("POST", "/undo/last");
    toast(data.message, data.ok ? "success" : "info");
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

async function undoGroup(groupId) {
  try {
    const data = await api("POST", `/undo/group/${groupId}`);
    toast(`Restored ${data.restored} file(s)`, "success");
    if (data.restored > 0) updateGroupStatus(groupId, "pending");
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

async function undoAll() {
  if (!confirm("Restore ALL moved files? This will undo every move.")) return;
  try {
    const data = await api("POST", "/undo/all");
    toast(`Restored ${data.restored} file(s)`, "success");
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

// ── lightbox ──────────────────────────────────────────────────────────────────
let _lightboxSrc = null;

function openLightbox(src) {
  _lightboxSrc = src;
  const lb = document.getElementById("lightbox");
  const img = document.getElementById("lightbox-img");
  if (!lb || !img) return;
  img.src = src;
  lb.classList.add("open");
}

function closeLightbox() {
  const lb = document.getElementById("lightbox");
  if (lb) lb.classList.remove("open");
}

document.addEventListener("click", (e) => {
  const lb = document.getElementById("lightbox");
  if (lb && e.target === lb) closeLightbox();
});

// ── scan progress (SSE) ───────────────────────────────────────────────────────
function startScan() {
  const overlay = document.getElementById("scan-overlay");
  const bar = document.getElementById("scan-progress-bar");
  const msg = document.getElementById("scan-message");

  if (overlay) overlay.classList.add("open");

  fetch("/api/scan", { method: "POST" }).then(() => {
    const es = new EventSource("/api/scan/status");
    es.onmessage = (e) => {
      const data = JSON.parse(e.data);
      const pct = data.total > 0 ? Math.round(data.current / data.total * 100) : 0;
      if (bar) bar.style.width = pct + "%";
      if (msg) msg.textContent = data.message || "";
      if (data.done) {
        es.close();
        if (overlay) overlay.classList.remove("open");
        toast("Scan complete — reloading…", "success");
        setTimeout(() => location.reload(), 800);
      }
    };
    es.onerror = () => {
      es.close();
      if (overlay) overlay.classList.remove("open");
      toast("Scan error — check console", "error");
    };
  });
}

// ── keyboard shortcuts ────────────────────────────────────────────────────────
document.addEventListener("keydown", (e) => {
  // Don't fire when typing in inputs
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

  const groupId = document.body.dataset.groupId;
  const prevId  = document.body.dataset.prevId;
  const nextId  = document.body.dataset.nextId;

  switch (e.key) {
    case "ArrowLeft":
    case "ArrowUp":
      if (prevId) location.href = `/review/${prevId}`;
      break;
    case "ArrowRight":
    case "ArrowDown":
    case " ":
      e.preventDefault();
      if (nextId) location.href = `/review/${nextId}`;
      break;
    case "k":
    case "K":
      if (groupId) approveGroup(parseInt(groupId));
      break;
    case "d":
    case "D":
      if (groupId) approveGroup(parseInt(groupId));
      break;
    case "s":
    case "S":
      if (groupId) skipGroup(parseInt(groupId));
      break;
    case "r":
    case "R":
      if (groupId) undoGroup(parseInt(groupId));
      break;
    case "z":
      if (e.ctrlKey || e.metaKey) undoLast();
      break;
    case "f":
    case "F":
      const firstImg = document.querySelector(".detail-photo img");
      if (firstImg) openLightbox(firstImg.src);
      break;
    case "Escape":
      closeLightbox();
      break;
  }
});

// ── confidence display helper (called from templates via inline script) ────────
function confClass(score) {
  if (score >= 0.75) return "conf-high";
  if (score >= 0.5)  return "conf-medium";
  return "conf-low";
}
