#!/usr/bin/env python3
"""Interactive card-picking UI for ./cards (a small local web app).

``./cards`` in its default form decides *for* you what to print (everything
new). This is the opposite: a browser page shows every cover as a thumbnail,
newest first, with the already-printed ones marked, and you click the exact
covers you want on one sheet. A counter tracks how many of a full page you have
picked; selecting the last one auto-renders the PDF and marks those covers as
printed (there is also a button to render a partial page early).

Why a browser and not a terminal UI: the whole point is to *see* the title
images (the device has no display, so a card is identified purely by its
picture), and only a real image grid does that. The server is bound to
localhost and only manipulates files under card-covers/ and print-sheets/.

It reuses the rendering and manifest logic from cards.py verbatim, so a sheet
made here is identical to one from ``./cards`` and integrates with the same undo
history (``./cards --undo``).
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import espuino
from cards import (collect_covers, page_grid, prepare_card, render_pages,
                   save_pdf)
from print_state import PrintState, _key

THUMB_PX = 320  # thumbnail size served to the browser (square, card-cropped)


def _creation_ts(st) -> float:
    """Best available creation timestamp. macOS exposes st_birthtime (the true
    file-creation time, which is what "sorted by Erstellungsdatum" means here);
    fall back to mtime where birthtime is absent."""
    return getattr(st, "st_birthtime", None) or st.st_mtime


class _State:
    """Shared, mutable server state guarded by a lock. Holds the print manifest
    in memory so prints made during the session immediately show up as marked."""

    def __init__(self, root: Path, covers_dir: Path, state: PrintState,
                 card_cm: float, cols: int, marks: bool, trim: bool):
        self.root = root
        self.covers_dir = covers_dir
        self.state = state
        self.card_cm = card_cm
        self.cols = cols
        self.marks = marks
        self.trim = trim
        _, self.per_page = page_grid(card_cm, cols)
        self._thumbs: dict[str, bytes] = {}  # (rel|mtime) -> jpeg bytes cache
        self.lock = threading.Lock()

    def rel_of(self, p: Path) -> str:
        return str(p.resolve().relative_to(self.covers_dir.resolve()))

    def covers(self) -> list[dict]:
        """Every cover under root as a dict, newest (by creation date) first."""
        items = []
        for p in collect_covers(self.root):
            st = p.stat()
            rel = self.rel_of(p)
            items.append({
                "rel": rel,
                # Stable NFC-normalised key matching the print manifest, so the
                # browser can correlate a cover with a run's undo history
                # regardless of macOS NFD/NFC filename form (see print_state._key).
                "key": _key(rel),
                "printed": self.state.is_printed(rel, st.st_size, st.st_mtime),
                "ts": _creation_ts(st),
            })
        items.sort(key=lambda d: d["ts"], reverse=True)
        return items

    def last_run(self) -> dict | None:
        """The most recent print run (what ``./cards --undo`` would revert), as
        the set of manifest keys it marked plus its label. None if no history."""
        if not self.state.runs:
            return None
        run = self.state.runs[-1]
        return {"keys": list(run.get("changes", {}).keys()),
                "at": run.get("at"), "out": run.get("out")}

    def undo(self) -> dict | None:
        """Revert the most recent print run (identical to ``./cards --undo``):
        restore the manifest and delete the tool's own auto-generated sheet (but
        never a custom --out file). Returns a summary, or None if nothing to undo."""
        run = self.state.undo_last()
        if run is None:
            return None
        self.state.save()
        removed = None
        outp = run.get("out")
        if outp and Path(outp).exists():
            pdf = Path(outp)
            if pdf.parent.resolve() == (espuino.REPO_ROOT / "print-sheets").resolve():
                pdf.unlink()
                removed = pdf.name
        return {"at": run.get("at"),
                "count": len(run.get("changes", {})),
                "removed": removed}

    def thumb(self, rel: str) -> bytes:
        """Square, card-cropped JPEG thumbnail for a cover (cached by mtime)."""
        path = self.covers_dir / rel
        st = path.stat()
        key = f"{rel}|{st.st_mtime_ns}"
        cached = self._thumbs.get(key)
        if cached is not None:
            return cached
        card = prepare_card(path, THUMB_PX, trim=self.trim)
        buf = io.BytesIO()
        card.save(buf, "JPEG", quality=85)
        data = buf.getvalue()
        self._thumbs[key] = data
        return data

    def make_sheet(self, rels: list[str]) -> dict:
        """Render the chosen covers to a fresh PDF and mark them printed."""
        covers = [self.covers_dir / r for r in rels]
        missing = [str(p) for p in covers if not p.exists()]
        if missing:
            raise FileNotFoundError(missing[0])
        pages = render_pages(covers, self.card_cm, self.cols,
                             marks=self.marks, trim=self.trim, log=lambda *_: None)
        out = (espuino.REPO_ROOT / "print-sheets"
               / f"cards-{time.strftime('%Y%m%d-%H%M%S')}.pdf")
        save_pdf(pages, out)
        items = []
        for r in rels:
            st = (self.covers_dir / r).stat()
            items.append((r, st.st_size, st.st_mtime))
        self.state.mark_run(items, str(out), time.strftime("%Y-%m-%d %H:%M:%S"))
        self.state.save()
        if sys.platform == "darwin":
            try:
                subprocess.run(["open", str(out)], check=False)
            except Exception:
                pass
        return {"out": str(out), "pages": len(pages), "marked": len(rels)}


def _handler(shared: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet; we print our own lines
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/config":
                self._json({"perPage": shared.per_page,
                            "cardCm": shared.card_cm, "cols": shared.cols})
            elif u.path == "/api/covers":
                with shared.lock:
                    self._json({"covers": shared.covers(),
                                "lastRun": shared.last_run()})
            elif u.path == "/thumb":
                rel = parse_qs(u.query).get("rel", [""])[0]
                try:
                    with shared.lock:
                        data = shared.thumb(rel)
                    self._send(200, data, "image/jpeg")
                except (FileNotFoundError, OSError):
                    self._send(404, b"not found", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/undo":
                with shared.lock:
                    result = shared.undo()
                if result is None:
                    self._json({"error": "no print run to undo"}, code=400)
                    return
                msg = (f"Undid the print run from {result['at']}: "
                       f"{result['count']} cover(s) count as new again.")
                if result["removed"]:
                    msg += f" Removed {result['removed']}."
                print(msg)
                self._json(result)
                return
            if u.path != "/api/print":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                rels = [str(r) for r in payload.get("rels", [])]
            except (ValueError, TypeError):
                self._json({"error": "bad request"}, code=400)
                return
            if not rels:
                self._json({"error": "no covers selected"}, code=400)
                return
            try:
                with shared.lock:
                    result = shared.make_sheet(rels)
                print(f"Wrote {result['out']}  "
                      f"({result['pages']} page(s), {result['marked']} card(s)); "
                      f"marked printed.")
                self._json(result)
            except FileNotFoundError as e:
                self._json({"error": f"missing cover: {e}"}, code=400)

    return Handler


def serve(root: Path, covers_dir: Path, state: PrintState, card_cm: float,
          cols: int, marks: bool, trim: bool, port: int) -> int:
    shared = _State(root, covers_dir, state, card_cm, cols, marks, trim)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler(shared))
    url = f"http://127.0.0.1:{httpd.server_port}/"
    print(f"Interactive card picker at {url}")
    print(f"  {len(collect_covers(root))} cover(s), {shared.per_page} per full page.")
    print("  Pick covers; the sheet auto-prints when a page is full. Ctrl+C to stop.")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


# --- the single-page app ----------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Karten drucken</title>
<style>
  :root { color-scheme: light dark; --gap: 10px; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.4 -apple-system, system-ui, sans-serif;
         background: #f4f4f5; color: #18181b; }
  @media (prefers-color-scheme: dark) {
    body { background: #18181b; color: #f4f4f5; }
    header { background: #27272a !important; border-color: #3f3f46 !important; }
    .card { background: #27272a; }
  }
  header { position: sticky; top: 0; z-index: 10; display: flex; align-items: center;
           gap: 16px; padding: 12px 20px; background: #fff;
           border-bottom: 1px solid #e4e4e7; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  .counter { font-variant-numeric: tabular-nums; font-weight: 600; font-size: 18px; }
  .counter.full { color: #16a34a; }
  .spacer { flex: 1; }
  button { font: inherit; padding: 8px 14px; border-radius: 8px; border: 1px solid #d4d4d8;
           background: #fff; color: inherit; cursor: pointer; }
  button:hover:not(:disabled) { background: #f4f4f5; }
  button.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
  button.primary:hover:not(:disabled) { background: #1d4ed8; }
  button.danger { border-color: #dc2626; color: #dc2626; }
  button.danger:hover:not(:disabled) { background: #dc2626; color: #fff; }
  button:disabled { opacity: .45; cursor: default; }
  button[hidden] { display: none; }
  #toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: #16a34a; color: #fff; padding: 10px 18px; border-radius: 10px;
           box-shadow: 0 6px 20px rgba(0,0,0,.25); opacity: 0; transition: opacity .3s;
           pointer-events: none; max-width: 90vw; }
  #toast.show { opacity: 1; }
  .grid { display: grid; gap: var(--gap); padding: 20px;
          grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .card { position: relative; aspect-ratio: 1; border-radius: 10px; overflow: hidden;
          cursor: pointer; border: 3px solid transparent; background: #ddd;
          transition: transform .08s; }
  .card:hover { transform: translateY(-2px); }
  .card img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .card.printed img { filter: grayscale(.7) brightness(.8); }
  .card.selected { border-color: #2563eb; }
  /* While hovering the undo button, spotlight exactly the covers that would be
     reverted: fade the rest, ring the affected ones in red. */
  .grid.undo-preview .card { transition: opacity .1s; }
  .grid.undo-preview .card:not(.in-last-run) { opacity: .25; }
  .grid.undo-preview .card.in-last-run { border-color: #dc2626;
                                         box-shadow: 0 0 0 3px rgba(220,38,38,.5); }
  .grid.undo-preview .card.in-last-run img { filter: none; }
  .badge { position: absolute; top: 6px; padding: 2px 7px; border-radius: 999px;
           font-size: 12px; font-weight: 700; color: #fff; }
  .badge.printed { right: 6px; background: rgba(0,0,0,.65); }
  .order { left: 6px; background: #2563eb; min-width: 22px; text-align: center; }
  .caption { position: absolute; bottom: 0; left: 0; right: 0; padding: 6px 8px;
             font-size: 11px; line-height: 1.25; color: #fff;
             background: linear-gradient(transparent, rgba(0,0,0,.75));
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .empty { padding: 60px; text-align: center; opacity: .6; }
</style>
</head>
<body>
<header>
  <h1>Karten drucken</h1>
  <span class="counter" id="counter">0 / ?</span>
  <span class="spacer"></span>
  <button id="undo" class="danger" disabled hidden>Letzten Druck rückgängig</button>
  <button id="auto" disabled hidden>Älteste drucken</button>
  <button id="clear" disabled>Auswahl leeren</button>
  <button id="make" class="primary" disabled>Seite erzeugen</button>
</header>
<div class="grid" id="grid"><div class="empty">Lade Titelbilder…</div></div>
<div id="toast"></div>

<script>
let perPage = 12;
let covers = [];
let lastRunKeys = new Set();        // manifest keys the last run marked (undo target)
const selected = [];               // rels in click order
const grid = document.getElementById('grid');
const counter = document.getElementById('counter');
const makeBtn = document.getElementById('make');
const clearBtn = document.getElementById('clear');
const autoBtn = document.getElementById('auto');
const undoBtn = document.getElementById('undo');
const toast = document.getElementById('toast');
let busy = false;

function showToast(msg) {
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 3500);
}

function shortName(rel) {
  const base = rel.split('/').pop().replace(/\.jpg$/i, '');
  return base;
}

function render() {
  grid.innerHTML = '';
  if (!covers.length) {
    grid.innerHTML = '<div class="empty">Keine Titelbilder gefunden.</div>';
    return;
  }
  for (const c of covers) {
    const pos = selected.indexOf(c.rel);
    const card = document.createElement('div');
    card.className = 'card' + (c.printed ? ' printed' : '') + (pos >= 0 ? ' selected' : '')
                   + (lastRunKeys.has(c.key) ? ' in-last-run' : '');
    card.title = c.rel;
    card.onclick = () => toggle(c.rel);
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.src = '/thumb?rel=' + encodeURIComponent(c.rel);
    card.appendChild(img);
    if (c.printed) {
      const b = document.createElement('span');
      b.className = 'badge printed';
      b.textContent = '✓ gedruckt';
      card.appendChild(b);
    }
    if (pos >= 0) {
      const o = document.createElement('span');
      o.className = 'badge order';
      o.textContent = pos + 1;
      card.appendChild(o);
    }
    const cap = document.createElement('div');
    cap.className = 'caption';
    cap.textContent = shortName(c.rel);
    card.appendChild(cap);
    grid.appendChild(card);
  }
}

function updateCounter() {
  counter.textContent = selected.length + ' / ' + perPage;
  counter.classList.toggle('full', selected.length >= perPage);
  clearBtn.disabled = selected.length === 0;
  makeBtn.disabled = selected.length === 0 || busy;
  makeBtn.textContent = 'Seite erzeugen (' + selected.length + ')';

  // "Älteste drucken": offer a one-click full page of the oldest unprinted
  // covers, only once a whole page's worth is waiting (same result as ./cards).
  const unprinted = covers.filter(c => !c.printed).length;
  autoBtn.hidden = unprinted < perPage;
  autoBtn.disabled = busy || selected.length > 0 || unprinted < perPage;
  autoBtn.textContent = 'Älteste ' + perPage + ' drucken';

  // Undo mirrors ./cards --undo; only shown when there is a run to revert.
  undoBtn.hidden = lastRunKeys.size === 0;
  undoBtn.disabled = busy || lastRunKeys.size === 0;
}

function toggle(rel) {
  if (busy) return;
  const i = selected.indexOf(rel);
  if (i >= 0) selected.splice(i, 1);
  else {
    if (selected.length >= perPage) { showToast('Seite ist voll (' + perPage + ').'); return; }
    selected.push(rel);
  }
  render();
  updateCounter();
  if (selected.length === perPage) makeSheet();  // auto-print on full page
}

function clearSelection() {
  selected.length = 0;
  render();
  updateCounter();
}

function autoPrintOldest() {
  if (busy) return;
  // covers is sorted newest-first, so the oldest unprinted are at the tail.
  const unprinted = covers.filter(c => !c.printed);
  if (unprinted.length < perPage) return;
  const oldest = unprinted.slice(-perPage);
  // Lay them out sorted by path, like ./cards does (series stay together).
  const rels = oldest.map(c => c.rel).sort();
  selected.length = 0;
  selected.push(...rels);
  render();
  updateCounter();
  makeSheet();
}

async function undoLast() {
  if (busy || lastRunKeys.size === 0) return;
  busy = true;
  updateCounter();
  grid.classList.remove('undo-preview');
  try {
    const res = await fetch('/api/undo', {method: 'POST'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Fehler');
    let msg = '↩ Druck rückgängig: ' + data.count + ' Karte(n) wieder neu.';
    if (data.removed) msg += ' ' + data.removed + ' gelöscht.';
    showToast(msg);
    await load();               // refresh printed state + undo target
  } catch (e) {
    showToast('Fehler: ' + e.message);
  } finally {
    busy = false;
    updateCounter();
  }
}

async function makeSheet() {
  if (busy || !selected.length) return;
  busy = true;
  updateCounter();
  try {
    const res = await fetch('/api/print', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rels: selected}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Fehler');
    const name = data.out.split('/').pop();
    showToast('✓ ' + data.pages + ' Seite(n) erzeugt: ' + name + ' — als gedruckt markiert.');
    selected.length = 0;
    await load();               // refresh printed state
  } catch (e) {
    showToast('Fehler: ' + e.message);
  } finally {
    busy = false;
    updateCounter();
  }
}

async function load() {
  const [cfg, cov] = await Promise.all([
    fetch('/api/config').then(r => r.json()),
    fetch('/api/covers').then(r => r.json()),
  ]);
  perPage = cfg.perPage;
  covers = cov.covers;
  lastRunKeys = new Set(cov.lastRun ? cov.lastRun.keys : []);
  // Drop any selected rels that vanished.
  for (let i = selected.length - 1; i >= 0; i--)
    if (!covers.some(c => c.rel === selected[i])) selected.splice(i, 1);
  render();
  updateCounter();
}

makeBtn.onclick = makeSheet;
clearBtn.onclick = clearSelection;
autoBtn.onclick = autoPrintOldest;
undoBtn.onclick = undoLast;
// Hovering the undo button previews which printed cards it would revert.
undoBtn.onmouseenter = () => { if (!undoBtn.disabled) grid.classList.add('undo-preview'); };
undoBtn.onmouseleave = () => grid.classList.remove('undo-preview');
undoBtn.onfocus = undoBtn.onmouseenter;
undoBtn.onblur = undoBtn.onmouseleave;
load();
</script>
</body>
</html>
"""
