#!/usr/bin/env python3
"""Lay out cover images into printable A4 sheets of RFID cards.

The workflow this feeds: print a sheet, stick a round RFID tag on the back of
each image, laminate the whole page, then cut it into square cards. Because the
cards are packed edge-to-edge (no gaps), every internal cut is shared between two
cards — a single straight cut separates a whole row or column. Thin tick marks in
the page margins mark each cut line so a ruler can be laid across the sheet.

Each card is a square (default 6x6 cm). Source covers are YouTube (Music)
thumbnails: a square title image centred in a 16:9 frame with solid colour bars
left/right. Those bars are trimmed automatically and the image is centre-cropped
to a square, reproducing the manual crop that was done before (the title text
sits inside the square, so it is preserved).

Like ./sync, this keeps a manifest so you don't have to say *what* to print:

    ./cards                    # lay out every cover not yet printed -> a new PDF
    ./cards --dry-run          # list what's new, write & mark nothing
    ./cards "Bibi und Tina"    # only covers under this subpath (still new-only)
    ./cards --all              # include already-printed covers too
    ./cards --reset            # forget all print history (so everything is "new")
    ./cards --reset "Bibi und Tina"   # forget just this subpath

By default a real run marks the covers it laid out as printed, so the next
plain ./cards only picks up newly-added covers. Use --no-mark to generate a
sheet without touching the manifest, or --dry-run to preview.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

import espuino
from print_state import PrintState

# --- page / card geometry ---------------------------------------------------
DPI = 300
A4_CM = (21.0, 29.7)
DEFAULT_CARD_CM = 6.0
DEFAULT_COLS = 3
DEFAULT_DISTORTION = 0.02

# Optional rectangular card format:
# 8x6 cm print format, resulting in an 8x5 cm finished card.
RECTANGULAR_PRINT_WIDTH_CM = 8.2
RECTANGULAR_PRINT_HEIGHT_CM = 6.2
RECTANGULAR_CONTENT_WIDTH_CM = 8.2
RECTANGULAR_CONTENT_HEIGHT_CM = 5.2
RECTANGULAR_TOP_BLEED_CM = 0.5
RECTANGULAR_BOTTOM_BLEED_CM = 0.5
RECTANGULAR_COLS = 2
# Minimum white space kept between the card block and the paper edge, so home
# printers (which cannot print to the very edge) never clip a card.
PAGE_MARGIN_CM = 0.7
# Cut-guide tick marks: length and shade (light grey, thin).
MARK_LEN_CM = 0.45
MARK_COLOR = (170, 170, 170)
MARK_WIDTH = 2
# Solid-bar trim tolerance: how far a pixel may differ from the corner colour
# and still count as "part of the uniform border".
TRIM_TOL = 24

STATE_FILE = espuino.PRINT_STATE_FILE


def cm(value: float) -> int:
    """Centimetres -> pixels at the print DPI."""
    return round(value / 2.54 * DPI)


# --- image preparation ------------------------------------------------------
def _trim_uniform_border(im: Image.Image, tol: int = TRIM_TOL) -> Image.Image:
    """Remove a uniform solid border (e.g. the colour bars YouTube pads a square
    cover with) by cropping to the region that differs from the corner colour.
    Returns the image unchanged if it has no such border."""
    rgb = im.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    diff = ImageChops.difference(rgb, bg).convert("L")
    mask = diff.point(lambda p: 255 if p > tol else 0)
    bbox = mask.getbbox()
    if bbox and bbox != (0, 0, rgb.width, rgb.height):
        return im.crop(bbox)
    return im


def _centre_square(im: Image.Image) -> Image.Image:
    """Centre-crop to the largest centred square."""
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return im.crop((left, top, left + side, top + side))


def prepare_card(path: Path, size_px: int, trim: bool = True) -> Image.Image:
    """Load a cover and return a square RGB image of exactly ``size_px`` for the
    card slot: trim solid bars (optional), centre-crop to square, resize."""
    im = Image.open(path)
    im = im.convert("RGB")
    if trim:
        im = _trim_uniform_border(im)
    im = _centre_square(im)
    if im.size != (size_px, size_px):
        im = im.resize((size_px, size_px), Image.LANCZOS)
    return im

def _stretch_edge(
    im: Image.Image,
    side: str,
    amount: int,
    distortion: float = DEFAULT_DISTORTION,
) -> Image.Image:
    """Extend an image by stretching a strip from one of its edges."""
    if amount <= 0:
        return Image.new("RGB", (1, 1))

    w, h = im.size
    fraction = distortion

    if side in ("left", "right"):
        strip_w = max(1, min(w, round(w * fraction)))
        if side == "left":
            strip = im.crop((0, 0, strip_w, h))
        else:
            strip = im.crop((w - strip_w, 0, w, h))
        return strip.resize((amount, h), Image.Resampling.LANCZOS)

    if side in ("top", "bottom"):
        strip_h = max(1, min(h, round(h * fraction)))
        if side == "top":
            strip = im.crop((0, 0, w, strip_h))
        else:
            strip = im.crop((0, h - strip_h, w, h))
        return strip.resize((w, amount), Image.Resampling.LANCZOS)

    raise ValueError(f"Unknown edge: {side}")


def _extend_to_size(
    im: Image.Image,
    target_w: int,
    target_h: int,
    distortion: float = DEFAULT_DISTORTION,
) -> Image.Image:
    """Fit proportionally and fill unused space by stretching image edges.

    The original image is never cropped or non-proportionally distorted.

    Square images are fitted by height. Thus an 8x5 target becomes a
    5x5 original image with 1.5 cm stretched to each side.
    """
    w, h = im.size
    if w <= 0 or h <= 0:
        raise ValueError("Image has invalid dimensions")

    if w == h:
        # Important: square images should be 5x5 cm, not 8x8 cm.
        scale = target_h / h
    else:
        scale = min(target_w / w, target_h / h)

    fitted_w = max(1, round(w * scale))
    fitted_h = max(1, round(h * scale))
    fitted = im.resize((fitted_w, fitted_h), Image.Resampling.LANCZOS)

    extra_w = target_w - fitted_w
    extra_h = target_h - fitted_h

    left = extra_w // 2
    right = extra_w - left
    top = extra_h // 2
    bottom = extra_h - top

    out = Image.new("RGB", (target_w, target_h))
    out.paste(fitted, (left, top))

    if left:
        out.paste(_stretch_edge(fitted, "left", left, distortion), (0, top))

    if right:
        out.paste(
            _stretch_edge(fitted, "right", right, distortion),
            (left + fitted_w, top),
        )

    if top:
        out.paste(_stretch_edge(fitted, "top", top, distortion), (left, 0))

    if bottom:
        out.paste(
            _stretch_edge(fitted, "bottom", bottom, distortion),
            (left, top + fitted_h),
        )

    # Fill corner areas if both horizontal and vertical extension were needed.
    if left and top:
        corner = out.crop((0, top, left, top + 1))
        out.paste(
            corner.resize((left, top), Image.Resampling.LANCZOS),
            (0, 0),
        )

    if right and top:
        corner = out.crop(
            (target_w - right, top, target_w, top + 1)
        )
        out.paste(
            corner.resize((right, top), Image.Resampling.LANCZOS),
            (target_w - right, 0),
        )

    if left and bottom:
        corner = out.crop(
            (0, target_h - bottom, left, target_h - bottom + 1)
        )
        out.paste(
            corner.resize((left, bottom), Image.Resampling.LANCZOS),
            (0, target_h - bottom),
        )

    if right and bottom:
        corner = out.crop(
            (
                target_w - right,
                target_h - bottom,
                target_w,
                target_h - bottom + 1,
            )
        )
        out.paste(
            corner.resize((right, bottom), Image.Resampling.LANCZOS),
            (target_w - right, target_h - bottom),
        )

    return out


def _add_bleed(
    im: Image.Image,
    top_bleed_px: int,
    bottom_bleed_px: int,
    distortion: float = DEFAULT_DISTORTION,
) -> Image.Image:
    """Add top and bottom bleed by stretching the respective image edges."""
    if top_bleed_px <= 0 and bottom_bleed_px <= 0:
        return im

    top_bleed = (
        _stretch_edge(im, "top", top_bleed_px, distortion)
        if top_bleed_px > 0
        else None
    )
    bottom_bleed = (
        _stretch_edge(im, "bottom", bottom_bleed_px, distortion)
        if bottom_bleed_px > 0
        else None
    )

    out = Image.new(
        "RGB",
        (im.width, im.height + top_bleed_px + bottom_bleed_px),
    )

    if top_bleed is not None:
        out.paste(top_bleed, (0, 0))

    out.paste(im, (0, top_bleed_px))

    if bottom_bleed is not None:
        out.paste(
            bottom_bleed,
            (0, top_bleed_px + im.height),
        )

    return out

def prepare_rectangular_card(
    path: Path,
    content_width_px: int,
    content_height_px: int,
    top_bleed_px: int,
    bottom_bleed_px: int,
    trim: bool = True,
    distortion: float = DEFAULT_DISTORTION,
) -> Image.Image:
    """Prepare an 8x5 finished card on 8x6 cm print stock."""
    im = Image.open(path).convert("RGB")

    if trim:
        im = _trim_uniform_border(im)

    content = _extend_to_size(
        im,
        content_width_px,
        content_height_px,
        distortion,
    )

    return _add_bleed(
        content,
        top_bleed_px,
        bottom_bleed_px,
        distortion,
    )

# --- page layout ------------------------------------------------------------
def _draw_cut_marks(draw: ImageDraw.ImageDraw, page_w: int, page_h: int,
                    ox: int, oy: int, card_px: int, cols: int, rows: int) -> None:
    """Draw tick marks in the margins at every card boundary (cols+1 verticals,
    rows+1 horizontals), at the top/bottom and left/right paper edges, so a ruler
    can be aligned edge-to-edge for each straight cut."""
    mark = cm(MARK_LEN_CM)
    for i in range(cols + 1):
        x = ox + i * card_px
        draw.line([(x, 0), (x, mark)], fill=MARK_COLOR, width=MARK_WIDTH)
        draw.line([(x, page_h - mark), (x, page_h)], fill=MARK_COLOR, width=MARK_WIDTH)
    for j in range(rows + 1):
        y = oy + j * card_px
        draw.line([(0, y), (mark, y)], fill=MARK_COLOR, width=MARK_WIDTH)
        draw.line([(page_w - mark, y), (page_w, y)], fill=MARK_COLOR, width=MARK_WIDTH)


def page_grid(card_cm: float, cols: int) -> tuple[int, int]:
    """(rows, cards-per-page) that fit on one A4 sheet for this card size/cols."""
    page_w, page_h = cm(A4_CM[0]), cm(A4_CM[1])
    card_px = cm(card_cm)
    rows = max(1, (page_h - 2 * cm(PAGE_MARGIN_CM)) // card_px)
    if cols * card_px > page_w - 2 * cm(PAGE_MARGIN_CM):
        raise SystemExit(f"ERROR: {cols} columns of {card_cm} cm do not fit across A4.")
    return rows, cols * rows


def render_pages(covers: list[Path], card_cm: float, cols: int,
                 marks: bool, trim: bool, log=print) -> list[Image.Image]:
    """Render the covers into one or more A4 page images (top-most row first,
    left-to-right), each page's card block centred on the paper."""
    page_w, page_h = cm(A4_CM[0]), cm(A4_CM[1])
    card_px = cm(card_cm)
    rows, per_page = page_grid(card_cm, cols)

    pages: list[Image.Image] = []
    for start in range(0, len(covers), per_page):
        chunk = covers[start:start + per_page]
        rows_used = (len(chunk) + cols - 1) // cols
        block_w, block_h = cols * card_px, rows_used * card_px
        ox = (page_w - block_w) // 2
        oy = (page_h - block_h) // 2

        page = Image.new("RGB", (page_w, page_h), "white")
        for idx, cover in enumerate(chunk):
            r, c = divmod(idx, cols)
            card = prepare_card(cover, card_px, trim=trim)
            page.paste(card, (ox + c * card_px, oy + r * card_px))
        if marks:
            _draw_cut_marks(ImageDraw.Draw(page), page_w, page_h,
                            ox, oy, card_px, cols, rows_used)
        pages.append(page)
        log(f"  page {len(pages)}: {len(chunk)} card(s)")
    return pages

def _draw_rectangular_cut_marks(
    draw: ImageDraw.ImageDraw,
    page_w: int,
    page_h: int,
    ox: int,
    oy: int,
    card_w: int,
    card_h: int,
    cols: int,
    rows: int,
) -> None:
    """Draw cut marks for the rectangular 8x6 cm print cards."""
    mark = cm(MARK_LEN_CM)

    for i in range(cols + 1):
        x = ox + i * card_w
        draw.line(
            [(x, 0), (x, mark)],
            fill=MARK_COLOR,
            width=MARK_WIDTH,
        )
        draw.line(
            [(x, page_h - mark), (x, page_h)],
            fill=MARK_COLOR,
            width=MARK_WIDTH,
        )

    for j in range(rows + 1):
        y = oy + j * card_h
        draw.line(
            [(0, y), (mark, y)],
            fill=MARK_COLOR,
            width=MARK_WIDTH,
        )
        draw.line(
            [(page_w - mark, y), (page_w, y)],
            fill=MARK_COLOR,
            width=MARK_WIDTH,
        )


def render_rectangular_pages(
    covers: list[Path],
    marks: bool,
    trim: bool,
    distortion: float = DEFAULT_DISTORTION,
    log=print,
) -> list[Image.Image]:
    """Render 8x5 cm finished cards onto 8x6 cm print stock."""
    page_w = cm(A4_CM[0])
    page_h = cm(A4_CM[1])

    card_w = cm(RECTANGULAR_PRINT_WIDTH_CM)
    card_h = cm(RECTANGULAR_PRINT_HEIGHT_CM)

    content_w = cm(RECTANGULAR_CONTENT_WIDTH_CM)
    content_h = cm(RECTANGULAR_CONTENT_HEIGHT_CM)
    top_bleed = cm(RECTANGULAR_TOP_BLEED_CM)
    bottom_bleed = cm(RECTANGULAR_BOTTOM_BLEED_CM)

    cols = RECTANGULAR_COLS

    # 4 rows x 2 columns = 8 cards on A4.
    rows = max(
        1,
        (page_h - 2 * cm(PAGE_MARGIN_CM)) // card_h,
    )
    per_page = rows * cols

    pages: list[Image.Image] = []

    for start in range(0, len(covers), per_page):
        chunk = covers[start:start + per_page]
        rows_used = (len(chunk) + cols - 1) // cols

        block_w = cols * card_w
        block_h = rows_used * card_h

        ox = (page_w - block_w) // 2
        oy = (page_h - block_h) // 2

        page = Image.new("RGB", (page_w, page_h), "white")

        for idx, cover in enumerate(chunk):
            r, c = divmod(idx, cols)

            card = prepare_rectangular_card(
                cover,
                content_w,
                content_h,
                top_bleed,
                bottom_bleed,
                trim=trim,
                distortion=distortion,
            )

            expected = (card_w, card_h)
            if card.size != expected:
                raise RuntimeError(
                    f"Unexpected rectangular card size {card.size}; "
                    f"expected {expected}"
                )

            page.paste(
                card,
                (ox + c * card_w, oy + r * card_h),
            )

        if marks:
            _draw_rectangular_cut_marks(
                ImageDraw.Draw(page),
                page_w,
                page_h,
                ox,
                oy,
                card_w,
                card_h,
                cols,
                rows_used,
            )

        pages.append(page)
        log(f"  page {len(pages)}: {len(chunk)} card(s)")

    return pages

def save_pdf(pages: list[Image.Image], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", resolution=DPI, save_all=True,
                  append_images=pages[1:])


# --- cover selection --------------------------------------------------------
def collect_covers(root: Path) -> list[Path]:
    """All cover .jpg files under ``root``, sorted by path (so series stay
    grouped and episodes stay in order). Note: espuino.IGNORE_PATTERNS lists
    ``*.jpg`` (to keep images out of the device sync), so it must NOT be applied
    to filenames here — only junk directories are skipped."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                             and not espuino.is_ignored(d))
        for fn in sorted(filenames):
            if fn.lower().endswith(".jpg") and not fn.startswith("._"):
                found.append(Path(dirpath) / fn)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lay out card covers into printable A4 PDF sheets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("path", nargs="?",
                        help="Limit to a subpath under card-covers/ (default: all)")
    parser.add_argument("--all", action="store_true",
                        help="Include already-printed covers, not only new ones")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be printed; write and mark nothing")
    parser.add_argument("--no-mark", action="store_true",
                        help="Generate the PDF but do not mark covers as printed")
    parser.add_argument("--reset", action="store_true",
                        help="Forget print history (optionally limited to PATH) and exit")
    parser.add_argument("--undo", action="store_true",
                        help="Revert the most recent run: its covers count as new "
                             "again and its auto-generated sheet is removed. Repeat "
                             "to step further back. Then exit.")
    parser.add_argument("--cols", type=int, default=DEFAULT_COLS,
                        help="Cards per row")
    parser.add_argument("--card-cm", type=float, default=DEFAULT_CARD_CM,
                        help="Card edge length in centimetres")
    parser.add_argument(
        "--rectangular",
        action="store_true",
        help="Print 8x5 cm finished cards on 8x6 cm stock",
    )
    parser.add_argument(
        "--distortion",
        type=float,
        default=DEFAULT_DISTORTION,
        help=f"Verzerrungsfaktor für Rectangular-Karten (default: {DEFAULT_DISTORTION})",
    )
    parser.add_argument("--no-marks", action="store_true",
                        help="Do not draw cut-guide tick marks")
    parser.add_argument("--no-trim", action="store_true",
                        help="Do not trim solid side bars before squaring")
    parser.add_argument("--out", help="Output PDF path (default: print-sheets/cards-<timestamp>.pdf)")
    args = parser.parse_args(argv)

    covers_dir = espuino.DEFAULT_COVERS_DIR
    root = covers_dir
    if args.path:
        p = Path(args.path)
        root = p if p.is_absolute() else covers_dir / args.path
    state = PrintState.load(STATE_FILE)
    if warning := espuino.state_warning("Der Druckverlauf", state.load_status):
        print(f"WARNUNG: {warning}")

    if args.reset:
        rel = "" if not args.path else str(Path(root).resolve().relative_to(covers_dir.resolve()))
        n = state.forget_under(rel)
        state.save()
        scope = f" under {rel!r}" if rel else ""
        print(f"Forgot print history for {n} cover(s){scope}. They now count as new.")
        return 0

    if args.undo:
        run = state.undo_last()
        if run is None:
            print("No print run to undo.")
            return 0
        state.save()
        n = len(run.get("changes", {}))
        print(f"Undid the print run from {run.get('at')}: "
              f"{n} cover(s) count as new again.")
        outp = run.get("out")
        if outp and Path(outp).exists():
            pdf = Path(outp)
            # Only auto-remove the tool's own timestamped sheet, never a custom --out.
            if pdf.parent.resolve() == espuino.PRINT_SHEETS_DIR.resolve():
                pdf.unlink()
                print(f"Removed the superseded sheet {pdf.name}.")
            else:
                print(f"(Left {pdf} in place — delete it yourself if not needed.)")
        return 0

    if not root.exists():
        print(f"ERROR: not found: {root}", file=sys.stderr)
        return 1

    all_covers = collect_covers(root)
    if not all_covers:
        print(f"No cover images (*.jpg) found under {root}.")
        return 0

    def rel_of(p: Path) -> str:
        return str(p.resolve().relative_to(covers_dir.resolve()))

    def is_new(p: Path) -> bool:
        st = p.stat()
        return not state.is_printed(rel_of(p), st.st_size, st.st_mtime)

    covers = all_covers if args.all else [p for p in all_covers if is_new(p)]
    if not covers:
        print(f"Nothing new to print ({len(all_covers)} cover(s) already printed). "
              f"Use --all to reprint, or --reset to forget history.")
        return 0

    kind = "cover(s)" if args.all else "new cover(s)"

    if args.rectangular:
        cols = RECTANGULAR_COLS
        print(
            f"{len(covers)} {kind} to lay out "
            f"({cols} per row, "
            f"{RECTANGULAR_PRINT_WIDTH_CM:g}x"
            f"{RECTANGULAR_PRINT_HEIGHT_CM:g} cm print / "
            f"{RECTANGULAR_CONTENT_WIDTH_CM:g}x"
            f"{RECTANGULAR_CONTENT_HEIGHT_CM:g} cm finished):"
        )
    else:
        cols = args.cols
        print(
            f"{len(covers)} {kind} to lay out "
            f"({cols} per row, "
            f"{args.card_cm:g}x{args.card_cm:g} cm):"
        )

    for p in covers:
        print(f"  {rel_of(p)}")

    if args.dry_run:
        if args.rectangular:
            rows = max(
                1,
                (
                    cm(A4_CM[1]) - 2 * cm(PAGE_MARGIN_CM)
                ) // cm(RECTANGULAR_PRINT_HEIGHT_CM),
            )
            per_page = rows * RECTANGULAR_COLS
        else:
            _, per_page = page_grid(args.card_cm, args.cols)

        short = (-len(covers)) % per_page
        if short:
            print(f"\nNOTE: the last page would have {per_page - short}/{per_page} "
                  f"cards. Add {short} more cover(s) for a full page.")
        print("\n(dry run — nothing written)")
        return 0

    if args.rectangular:
        pages = render_rectangular_pages(
            covers,
            marks=not args.no_marks,
            trim=not args.no_trim,
        )
    else:
        pages = render_pages(
            covers,
            args.card_cm,
            args.cols,
            marks=not args.no_marks,
            trim=not args.no_trim,
        )

    out = Path(args.out) if args.out else (
        espuino.PRINT_SHEETS_DIR / f"cards-{time.strftime('%Y%m%d-%H%M%S')}.pdf")
    save_pdf(pages, out)
    print(f"\nWrote {out}  ({len(pages)} page(s), {len(covers)} card(s)).")

    # Full pages only laminate well, so flag a partial last page: say how many
    # more covers would fill it (download that many more, then re-run ./cards).
    if args.rectangular:
        rows = max(
            1,
            (
                cm(A4_CM[1]) - 2 * cm(PAGE_MARGIN_CM)
            ) // cm(RECTANGULAR_PRINT_HEIGHT_CM),
        )
        per_page = rows * RECTANGULAR_COLS
    else:
        _, per_page = page_grid(args.card_cm, args.cols)

    short = (-len(covers)) % per_page
    if short:
        print(f"NOTE: the last page has {per_page - short}/{per_page} cards. "
              f"Add {short} more cover(s) for a full page — or './cards --undo' "
              f"to drop this run, download more, and reprint a full sheet.")

    if not args.no_mark:
        items = [(rel_of(p), p.stat().st_size, p.stat().st_mtime) for p in covers]
        state.mark_run(items, str(out), time.strftime("%Y-%m-%d %H:%M:%S"))
        state.save()
        print(f"Marked {len(covers)} cover(s) as printed "
              f"(undo with ./cards --undo, or ./cards --reset to redo all).")
    else:
        print("(--no-mark: manifest unchanged; these covers still count as new.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
