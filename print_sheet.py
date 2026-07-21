"""
Print-sheet PDF builder.

Lays out upscaled card PNGs on A4 / Letter pages, 3x3 cards of exactly
63 x 88 mm each (no gutters, so a straight guillotine cut between cards),
with short white cross guides at every card intersection plus dark tick
marks in the margins.

Print-time adjustments (masters on disk stay untouched):
  - quality:  lossless PNG/Flate, or JPEG q97/q92
  - profile:  color calibration (brightness / gamma / saturation) chosen
              from a printed calibration sheet
  - sharpen:  output sharpening to compensate inkjet dot gain
  - pages_per_file: split large lossless exports into printable chunks
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from reportlab import rl_config

# store image streams as raw binary Flate instead of ASCII85 (25% smaller,
# and lossless mode is heavy enough already)
rl_config.useA85 = 0

from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from config import (
    TEMP_FOLDER,
    PDF_QUALITY_MODES,
    PDF_DEFAULT_QUALITY,
    SHARPEN_MODES,
    SHADOW_LIFTS,
    SHADOW_TEST_LEVELS,
    CALIBRATION_PROFILES,
    find_back_image,
)

CARD_W = 63 * mm
CARD_H = 88 * mm

# name -> (cols, rows, landscape)
LAYOUTS = {
    "3×3 portrait": (3, 3, False),
    "4×2 landscape": (4, 2, True),
}
DEFAULT_LAYOUT = "3×3 portrait"

# calibration / shadow test sheets always use the classic grid
COLS = 3
ROWS = 3
PER_PAGE = COLS * ROWS

MARK_LEN = 4 * mm     # margin tick length
MARK_GAP = 1 * mm     # gap between block edge and tick start

PAGES = {"A4": A4, "Letter": letter}

# The card block never gets closer than this to the paper's bottom edge
# (typical inkjet unprintable margin).
MIN_BOTTOM = 3 * mm


def _block_origin(pw, ph, block_w, block_h, shift_down_mm=0.0):
    """Centered block, optionally shifted down (for printers whose heavy
    cardstock feeds late and clips the top of the page)."""
    ox = (pw - block_w) / 2
    oy = (ph - block_h) / 2 - shift_down_mm * mm
    return ox, max(MIN_BOTTOM, oy)


# --------------------------------------------------------------------------
# image preparation
# --------------------------------------------------------------------------

# Shadow lift only affects tones below this level, fading linearly to zero.
# Midtones and highlights are untouched, so the card keeps its overall look.
SHADOW_KNEE = 75

# --- black-border deepening -----------------------------------------------
# Scans carry their black border at ~20/255 instead of true black, and the
# shadow lift pushes it further up (~37 with profile 9 + Medium), so it
# prints as dark grey. This snaps that border to real black.
#
# Three independent guards keep it artifact-free:
#   1. per card: only run when the perimeter is UNIFORMLY dark. Borderless,
#      full-art and white-bordered cards have art (high variance) or light
#      pixels there, so they are skipped entirely — no vignette effect.
#   2. per pixel, spatially: the treated band is measured from the image
#      (how deep the uniform dark border actually goes), then faded out, so
#      the border is covered edge to edge with no gradient inside it.
#   3. per pixel, tonally: only pixels that are already dark are pushed, so
#      anything bright that reaches into the band is left alone.
# Detection uses percentiles, not mean/std: scans carry stray bright specks
# that wreck a standard deviation (one card measured std 22 off four white
# pixels) while the median and p90 stay rock steady.
BORDER_RING_FRAC = 0.03      # perimeter depth sampled for detection
BORDER_MAX_LEVEL = 70        # median brighter than this -> not a black border
BORDER_MAX_SPREAD = 12       # p90-p50 above this -> art in the ring, skip card
BORDER_MAX_WIDTH = 0.09      # never treat deeper than this (card width frac)
BORDER_FADE_FRAC = 0.012     # fade-out distance past the detected border
BORDER_TONE_MAX = 90         # pixels brighter than this are never touched


def _apply_profile(im: Image.Image, profile, shadow=0) -> Image.Image:
    """
    profile = (label, brightness, gamma, saturation); shadow = lift at pure
    black (0-255), fading linearly to zero at SHADOW_KNEE. Only the deepest
    tones rise — the rest of the tonal range is untouched.
    """
    _, brightness, gamma, saturation = profile if profile else ("", 1.0, 1.0, 1.0)
    if gamma != 1.0 or shadow > 0:
        lut = []
        for i in range(256):
            base = 255 * ((i / 255) ** gamma)
            lift = shadow * max(0.0, 1.0 - i / SHADOW_KNEE)
            lut.append(min(255, int(base + lift + 0.5)))
        im = im.point(lut * 3)
    if brightness != 1.0:
        im = ImageEnhance.Brightness(im).enhance(brightness)
    if saturation != 1.0:
        im = ImageEnhance.Color(im).enhance(saturation)
    return im


def _deepen_black_border(im: Image.Image, opaque=None) -> Image.Image:
    """
    Snap a washed-out black border to true black. Returns the image
    unchanged unless the card's perimeter is uniformly dark.

    `opaque` is a bool mask of non-transparent pixels (the PNG's rounded
    corners must be excluded or every card would look black-bordered).
    """
    a = np.asarray(im, dtype=np.float32)
    h, w = a.shape[:2]
    gray = a @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    # ---- guard 1: is this actually a uniform black border?
    t = max(2, int(w * BORDER_RING_FRAC))
    ring = np.zeros((h, w), dtype=bool)
    ring[:t, :] = ring[-t:, :] = True
    ring[:, :t] = ring[:, -t:] = True
    if opaque is not None:
        ring &= opaque
    vals = gray[ring]
    if vals.size == 0:
        return im
    p50, p90 = (float(x) for x in np.percentile(vals, (50, 90)))
    if p50 > BORDER_MAX_LEVEL or (p90 - p50) > BORDER_MAX_SPREAD:
        return im

    # ---- distance from the nearest edge, in pixels
    yy = np.arange(h, dtype=np.float32)[:, None]
    xx = np.arange(w, dtype=np.float32)[None, :]
    dist = np.minimum(np.minimum(xx, w - 1 - xx), np.minimum(yy, h - 1 - yy))

    # ---- how deep does the uniform dark border really go?
    # walk outwards-in and stop where the ring stops being border-dark
    limit = int(w * BORDER_MAX_WIDTH)
    cutoff = p90 + 22
    border_px = limit
    step = max(1, limit // 30)
    for d in range(t, limit, step):
        shell = (dist >= d) & (dist < d + 2)
        if opaque is not None:
            shell &= opaque
        v = gray[shell]
        if v.size and float(np.percentile(v, 60)) > cutoff:
            border_px = d
            break

    # ---- guard 2: spatial weight (full inside the border, then fades)
    fade = max(2.0, w * BORDER_FADE_FRAC)
    spatial = np.clip((border_px + fade - dist) / fade, 0.0, 1.0)

    # ---- guard 3: tonal weight (only already-dark pixels)
    black_point = min(p90 + 6, BORDER_TONE_MAX - 5)
    tonal = np.clip((BORDER_TONE_MAX - gray) / (BORDER_TONE_MAX - black_point),
                    0.0, 1.0)

    weight = (spatial * tonal)[..., None]
    scaled = np.clip((a - black_point) * (255.0 / (255.0 - black_point)), 0, 255)
    out = a * (1.0 - weight) + scaled * weight
    return Image.fromarray(out.astype(np.uint8), "RGB")


def _flatten(png_path: Path, jpeg_quality, profile=None, sharpen=None,
             shadow=0, deepen_border=False, suffix="_sheet") -> Path:
    """
    Flatten transparent rounded corners onto black, apply the print-time
    adjustments, and write the temp file the PDF will embed.

    jpeg_quality None -> PNG (Flate, pixel-identical apart from adjustments)
    """
    im = Image.open(png_path).convert("RGBA")
    alpha = im.split()[3]
    bg = Image.new("RGB", im.size, (0, 0, 0))
    bg.paste(im, mask=alpha)

    if (profile and profile[1:] != (1.0, 1.0, 1.0)) or shadow > 0:
        bg = _apply_profile(bg, profile, shadow)
    if sharpen:
        radius, percent, threshold = sharpen
        bg = bg.filter(ImageFilter.UnsharpMask(
            radius=radius, percent=percent, threshold=threshold))
    if deepen_border:
        # last, so it also undoes the shadow lift inside the border
        opaque = np.asarray(alpha) > 250
        bg = _deepen_black_border(bg, opaque)

    if jpeg_quality is None:
        out = TEMP_FOLDER / (png_path.stem + suffix + ".png")
        bg.save(out, "PNG", dpi=(1200, 1200))
    else:
        out = TEMP_FOLDER / (png_path.stem + suffix + ".jpg")
        bg.save(out, "JPEG", quality=jpeg_quality, dpi=(1200, 1200))
    return out


# --------------------------------------------------------------------------
# page drawing
# --------------------------------------------------------------------------

# guide-cross colors selectable in the export dialog
GUIDE_COLORS = {
    "White": (1, 1, 1),
    "Black": (0, 0, 0),
    "Gray": (0.45, 0.45, 0.45),
    "None": None,
}

# bleed-frame colors (edge printed around each card, cut runs through it)
BLEED_COLORS = {
    "Black": (0, 0, 0),
    "White": (1, 1, 1),
}


def _boundaries(origin, size, gutter, count=3):
    """Card-edge coordinates along one axis (duplicates removed at gutter 0)."""
    edges = []
    for i in range(count):
        a = origin + i * (size + gutter)
        edges.append(a)
        edges.append(a + size)
    return sorted(set(edges))


def _draw_marks(c, ox, oy, block_w, block_h, gutter=0.0, guide_rgb=(1, 1, 1),
                cols=COLS, rows=ROWS):
    xs = _boundaries(ox, CARD_W, gutter, cols)
    ys = _boundaries(oy, CARD_H, gutter, rows)
    tick = 4 * mm

    # short cross ticks at every card corner (over borders / bleed frames)
    if guide_rgb is not None:
        c.setLineWidth(0.4)
        c.setStrokeColorRGB(*guide_rgb)
        for x in xs:
            for y in ys:
                c.line(x, max(y - tick, oy), x, min(y + tick, oy + block_h))
                c.line(max(x - tick, ox), y, min(x + tick, ox + block_w), y)

    # dark tick marks in the margins, aligned to every boundary
    c.setLineWidth(0.4)
    c.setStrokeColorRGB(0.4, 0.4, 0.4)
    for x in xs:
        c.line(x, oy + block_h + MARK_GAP, x, oy + block_h + MARK_GAP + MARK_LEN)
        c.line(x, oy - MARK_GAP, x, oy - MARK_GAP - MARK_LEN)
    for y in ys:
        c.line(ox - MARK_GAP, y, ox - MARK_GAP - MARK_LEN, y)
        c.line(ox + block_w + MARK_GAP, y, ox + block_w + MARK_GAP + MARK_LEN, y)


def _card_pos(idx, ox, oy, block_h, gutter=0.0, cols=COLS):
    col = idx % cols
    row = idx // cols
    x = ox + col * (CARD_W + gutter)
    y = oy + block_h - (row + 1) * CARD_H - row * gutter
    return x, y


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def build_pdf(images, out_path, page_name="A4", quality=PDF_DEFAULT_QUALITY,
              sharpen_name="Off", profile_id=1, shadow_name="Off",
              pages_per_file=0, backs=None, back_offset=(0.0, 0.0),
              back_bleed_mm=1.5, shift_down_mm=0.0,
              edge_bleed_mm=0.0, bleed_color="Black", guide_color="White",
              layout=DEFAULT_LAYOUT, deepen_border=False,
              status_callback=None) -> list[Path]:
    """
    Compose `images` (paths, in order) into one or more print-sheet PDFs.

    pages_per_file: 0 = single file; N = split every N sheets into
                    name-01.pdf, name-02.pdf, ...
    backs:          None = fronts only. Otherwise a list parallel to
                    `images`: a back-face path per slot, or None to use the
                    user-supplied back.png. Each front page is followed by a
                    column-mirrored back page for duplex printing
                    (flip on long edge).
    back_offset:    (dx_mm, dy_mm) shift applied to the back pages only, to
                    compensate the printer's duplex misalignment.
    back_bleed_mm:  backs are drawn oversized by this much on every edge, so
                    duplex drift up to ~that amount never exposes a white
                    sliver when cutting along the FRONT's marks.
    edge_bleed_mm:  fronts get a colored bleed frame this wide around each
                    card; cards are separated by a 2x gutter so the cut runs
                    through the frame — small cut drift shows frame color,
                    never white paper or the neighboring card.
    bleed_color:    key of BLEED_COLORS for that frame.
    guide_color:    key of GUIDE_COLORS for the corner cross guides.
    Returns the list of files written.
    """
    images = [Path(p) for p in images]
    if not images:
        raise ValueError("No images to lay out")

    if backs is not None:
        default_back = find_back_image()
        if any(b is None for b in backs) and default_back is None:
            raise ValueError(
                "back.png not found.\nPut a card-back image named back.png "
                "(or back.jpg) next to ProxyForge.exe — it is used for "
                "every card that has no double-faced back of its own.")
        backs = [Path(b) if b else default_back for b in backs]

    jpeg_quality = PDF_QUALITY_MODES.get(
        quality, PDF_QUALITY_MODES[PDF_DEFAULT_QUALITY])
    sharpen = SHARPEN_MODES.get(sharpen_name)
    profile = CALIBRATION_PROFILES.get(profile_id)
    shadow = SHADOW_LIFTS.get(shadow_name, 0)

    cols, rows, landscape = LAYOUTS.get(layout, LAYOUTS[DEFAULT_LAYOUT])
    per_page = cols * rows
    page = PAGES.get(page_name, A4)
    if landscape:
        page = (page[1], page[0])
    pw, ph = page
    gutter = 2 * edge_bleed_mm * mm
    block_w = cols * CARD_W + (cols - 1) * gutter
    block_h = rows * CARD_H + (rows - 1) * gutter
    if block_w > pw - 2 * MIN_BOTTOM or block_h > ph - 2 * MIN_BOTTOM:
        raise ValueError("Edge bleed too large for this page size")
    ox, oy = _block_origin(pw, ph, block_w, block_h, shift_down_mm)
    guide_rgb = GUIDE_COLORS.get(guide_color, (1, 1, 1))
    bleed_rgb = BLEED_COLORS.get(bleed_color, (0, 0, 0))
    ebleed = edge_bleed_mm * mm
    dx = back_offset[0] * mm
    dy = back_offset[1] * mm

    # split the card list into sheets, then sheets into files
    idxs = list(range(len(images)))
    batches = [idxs[i:i + per_page] for i in range(0, len(idxs), per_page)]
    if pages_per_file and pages_per_file > 0:
        groups = [batches[i:i + pages_per_file]
                  for i in range(0, len(batches), pages_per_file)]
    else:
        groups = [batches]

    out_path = Path(out_path)
    written = []
    flat_cache = {}

    def flat(img):
        key = str(img)
        if key not in flat_cache:
            flat_cache[key] = _flatten(img, jpeg_quality, profile, sharpen,
                                       shadow, deepen_border)
        return flat_cache[key]

    placed = 0
    for gi, group in enumerate(groups):
        if len(groups) == 1:
            target = out_path
        else:
            target = out_path.with_name(f"{out_path.stem}-{gi + 1:02d}{out_path.suffix}")

        c = canvas.Canvas(str(target), pagesize=page)
        c.setTitle("ProxyForge print sheet")

        for batch in group:
            # ---- front page
            if ebleed > 0:
                c.setFillColorRGB(*bleed_rgb)
                for slot in range(len(batch)):
                    x, y = _card_pos(slot, ox, oy, block_h, gutter, cols)
                    c.rect(x - ebleed, y - ebleed,
                           CARD_W + 2 * ebleed, CARD_H + 2 * ebleed,
                           stroke=0, fill=1)
            for slot, i in enumerate(batch):
                placed += 1
                if status_callback:
                    status_callback(f"Placing card {placed}/{len(images)}…")
                x, y = _card_pos(slot, ox, oy, block_h, gutter, cols)
                c.drawImage(ImageReader(str(flat(images[i]))), x, y, CARD_W, CARD_H)
            _draw_marks(c, ox, oy, block_w, block_h, gutter, guide_rgb, cols, rows)
            c.showPage()

            # ---- mirrored back page (duplex, flip on long edge)
            if backs is not None:
                bleed = back_bleed_mm * mm
                for slot, i in enumerate(batch):
                    col = slot % cols
                    row = slot // cols
                    mirrored = row * cols + (cols - 1 - col)
                    x, y = _card_pos(mirrored, ox, oy, block_h, gutter, cols)
                    # oversized by the bleed on every edge: small duplex
                    # drift stays covered when cutting along the front
                    c.drawImage(ImageReader(str(flat(backs[i]))),
                                x + dx - bleed, y + dy - bleed,
                                CARD_W + 2 * bleed, CARD_H + 2 * bleed)
                _draw_marks(c, ox, oy, block_w, block_h, gutter, guide_rgb, cols, rows)
                c.showPage()

        c.save()
        written.append(target)

    for t in flat_cache.values():
        try:
            t.unlink()
        except OSError:
            pass

    return written


def build_calibration(image_path, out_path, page_name="A4",
                      shift_down_mm=0.0, status_callback=None) -> Path:
    """
    One page with the same card rendered through all 9 calibration
    profiles, numbered, to print and compare against a real card.
    """
    image_path = Path(image_path)
    page = PAGES.get(page_name, A4)
    pw, ph = page
    block_w, block_h = COLS * CARD_W, ROWS * CARD_H
    ox, oy = _block_origin(pw, ph, block_w, block_h, shift_down_mm)

    c = canvas.Canvas(str(out_path), pagesize=page)
    c.setTitle("ProxyForge calibration sheet")

    temp_files = []
    for idx, (pid, profile) in enumerate(sorted(CALIBRATION_PROFILES.items())):
        if status_callback:
            status_callback(f"Rendering variant {pid}/9…")
        x, y = _card_pos(idx, ox, oy, block_h)
        flat = _flatten(image_path, 97, profile, None, suffix=f"_cal{pid}")
        temp_files.append(flat)
        c.drawImage(ImageReader(str(flat)), x, y, CARD_W, CARD_H)

        # number badge on the card's top-left corner
        c.setFillColorRGB(1, 1, 1)
        c.rect(x + 2 * mm, y + CARD_H - 8 * mm, 8 * mm, 6 * mm, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(x + 6 * mm, y + CARD_H - 6.4 * mm, str(pid))

    _draw_marks(c, ox, oy, block_w, block_h)

    c.setFillColorRGB(0.3, 0.3, 0.3)
    c.setFont("Helvetica", 8)
    c.drawCentredString(pw / 2, oy - MARK_LEN - 4 * mm,
                        "ProxyForge calibration — print at 100% scale, no printer color correction. "
                        "Pick the number closest to a real card.")
    c.showPage()
    c.save()

    for t in temp_files:
        try:
            t.unlink()
        except OSError:
            pass

    return Path(out_path)


def build_shadow_test(image_path, out_path, page_name="A4", profile_id=1,
                      shift_down_mm=0.0, status_callback=None) -> Path:
    """
    One page with the same card at 9 shadow-lift levels (the chosen color
    profile already applied), labeled with the +N value. Pick the lowest
    number where dark details (artist signatures, shadow texture) become
    visible without the blacks looking washed.
    """
    image_path = Path(image_path)
    profile = CALIBRATION_PROFILES.get(profile_id)
    page = PAGES.get(page_name, A4)
    pw, ph = page
    block_w, block_h = COLS * CARD_W, ROWS * CARD_H
    ox, oy = _block_origin(pw, ph, block_w, block_h, shift_down_mm)

    c = canvas.Canvas(str(out_path), pagesize=page)
    c.setTitle("ProxyForge shadow test")

    temp_files = []
    for idx, level in enumerate(SHADOW_TEST_LEVELS[:PER_PAGE]):
        if status_callback:
            status_callback(f"Rendering +{level}…")
        x, y = _card_pos(idx, ox, oy, block_h)
        flat = _flatten(image_path, 97, profile, None, level,
                        suffix=f"_sh{level}")
        temp_files.append(flat)
        c.drawImage(ImageReader(str(flat)), x, y, CARD_W, CARD_H)

        c.setFillColorRGB(1, 1, 1)
        c.rect(x + 2 * mm, y + CARD_H - 8 * mm, 11 * mm, 6 * mm, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(x + 7.5 * mm, y + CARD_H - 6.4 * mm, f"+{level}")

    _draw_marks(c, ox, oy, block_w, block_h)
    c.setFillColorRGB(0.3, 0.3, 0.3)
    c.setFont("Helvetica", 8)
    c.drawCentredString(pw / 2, oy - MARK_LEN - 4 * mm,
                        "Shadow lift test — pick the lowest +N where dark details "
                        "(signature) are visible after laminating.")
    c.showPage()
    c.save()

    for t in temp_files:
        try:
            t.unlink()
        except OSError:
            pass

    return Path(out_path)
