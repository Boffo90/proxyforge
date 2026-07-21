import json
import sys
from pathlib import Path

# ==========================================
# PATHS
# ==========================================
# When running as a PyInstaller .exe, external resources (the real-esrgan
# binary, models, output) live next to the .exe, not inside the bundle.

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).parent

# bundled resources (inside the exe when frozen, project folder in dev)
_BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))

ICON_FILE = _BUNDLE / "icon.ico"

REALESRGAN_EXE = ROOT / "realesrgan-ncnn-vulkan.exe"

MODELS_FOLDER = ROOT / "models"

OUTPUT_FOLDER = ROOT / "output"

# Temp folder for downloads / format normalization
TEMP_FOLDER = ROOT / "_temp"

OUTPUT_FOLDER.mkdir(exist_ok=True)
TEMP_FOLDER.mkdir(exist_ok=True)

# ==========================================
# MODELS
# ==========================================
# label -> (model name passed to real-esrgan, native scale)
#
# realesrgan-x4plus  -> best for MTG card art: keeps grain / brush texture,
#                       sharpest result (recommended default).
# realesrgan-x4plus-anime / animevideov3 -> smoother, flattens grain.
#                       Only enable if a specific card looks better with it.

MODELS = {
    "AnimeVideo v3 x4 (scanned cards)": ("realesr-animevideov3", 4),
    "High Fidelity x4 (digital renders)": ("high-fidelity-4x", 4),
    "UltraSharp x4 (max crisp, flat art)": ("ultrasharp-4x", 4),
    "Real-ESRGAN x4+ (realistic art / faces)": ("realesrgan-x4plus", 4),
}

# ------------------------------------------------------------------
# "Auto" mode
# ------------------------------------------------------------------
# Scryfall image eras (verified by sampling set-by-set):
#   - Up to MOM (2023-04): flatbed scans of paper cards, PNGs 1.4-2.1 MB.
#   - From LTR (2023-06) onward: official WotC digital renders, 0.3-1.2 MB.
# Decision order:
#   1. If the card's released_at is known (Scryfall paths), the date decides.
#   2. Otherwise (local files), the file size decides.
# Models per era (chosen by face/art comparisons on real cards):
#   - scans      -> AnimeVideo v3 (user preference, handles paper grain well)
#   - renders    -> UltraSharp (crispest result on illustrated/flat art,
#                   which is most digital cards)
#   - realistic  -> Real-ESRGAN x4+ for sets with photorealistic art
#                   (UltraSharp posterizes faces/hands there). Art style
#                   cannot be reliably detected from pixels, so these are
#                   listed by set code below — add new codes as they appear.
AUTO_MODEL = "Auto (detect digital vs scan)"

RENDER_ERA_START = "2023-06-01"      # released_at >= this -> digital render

AUTO_DIGITAL_MAX_BYTES = 1_350_000   # size fallback when no date is known

# Set codes (lowercase) whose art is photorealistic (Marvel etc.)
REALISTIC_SETS = {"msc", "spm", "mar"}

AUTO_DIGITAL_MODEL = "UltraSharp x4 (max crisp, flat art)"
AUTO_REALISTIC_MODEL = "Real-ESRGAN x4+ (realistic art / faces)"
AUTO_SCAN_MODEL = "AnimeVideo v3 x4 (scanned cards)"

DEFAULT_MODEL = AUTO_MODEL

# ==========================================
# OUTPUT / DPI
# ==========================================
# A Magic card is 63 x 88 mm (2.48 x 3.465 in).
# At 1200 DPI that is exactly 2976 x 4160 px, which also happens to be
# the x4 upscale of a Scryfall "png" image (744 x 1040). Everything lines up.

TARGET_DPI = 1200

CARD_WIDTH_PX = 2976
CARD_HEIGHT_PX = 4160

# When True, the final image is resized to the exact card pixel size above.
# When False, the native upscaled resolution is kept (DPI metadata still set).
FIT_TO_CARD_DEFAULT = True

# Accepted input extensions (anything else is converted to PNG first)
SUPPORTED_INPUT = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp",
    ".tif", ".tiff", ".gif", ".jfif", ".avif",
}

# ==========================================
# SCRYFALL
# ==========================================

SCRYFALL_API = "https://api.scryfall.com"

# Scryfall asks for a descriptive User-Agent and a small delay between calls.
SCRYFALL_HEADERS = {
    "User-Agent": "ProxyForge/2.0 (proxy print studio)",
    "Accept": "application/json",
}

SCRYFALL_DELAY = 0.1  # seconds between requests

# ==========================================
# PROCESSING
# ==========================================

# Cards processed simultaneously. The GPU serializes most of the work, but
# overlapping download / upscale / post-process gives a solid speedup.
# Verified stable on an RTX 3070 Ti; outputs are identical to sequential.
PARALLEL_JOBS = 3

# ==========================================
# PRINT SHEET (PDF)
# ==========================================

# 3x3 cards of exactly 63x88 mm per page, no gutters (fewer cuts),
# with crop tick marks in the margins.
PDF_PAGE_SIZES = ["A4", "Letter"]

PDF_DEFAULT_PAGE = "Letter"

# Image encoding inside the PDF.
#   Lossless -> PNG/Flate, pixel-for-pixel identical to the upscaled card.
#               ~29 MB per card (~264 MB per full 3x3 page).
#   JPEG q97  -> near-lossless, ~6 MB per card (~53 MB per page).
#   JPEG q92  -> drafts / proofs, ~3 MB per card.
PDF_QUALITY_MODES = {
    "Lossless (max)": None,
    "JPEG q97 (near-lossless)": 97,
    "JPEG q92 (draft)": 92,
}

PDF_DEFAULT_QUALITY = "Lossless (max)"

# How many sheet pages go into each PDF file. Lossless pages are ~217 MB,
# so splitting keeps files printable; 0 = everything in one file.
PAGES_PER_FILE = {
    "1 page / PDF": 1,
    "2 pages / PDF": 2,
    "3 pages / PDF": 3,
    "Single file": 0,
}

PAGES_PER_FILE_DEFAULT = "1 page / PDF"

# Output sharpening for the PDF only (masters stay untouched): compensates
# the ink dot gain of inkjet printing. (radius px, percent, threshold)
# Tuned for 2976x4160 @ 1200 DPI; verify on real prints.
SHARPEN_MODES = {
    "Off": None,
    "Soft": (2, 60, 3),
    "Medium": (3, 110, 3),
}

SHARPEN_DEFAULT = "Off"

# Color calibration profiles. Inkjets typically print darker and less
# saturated than the screen, so every variant pushes lighter / more vivid.
# id -> (label, brightness, gamma, saturation); applied to the PDF only.
CALIBRATION_PROFILES = {
    1: ("1 · original", 1.00, 1.00, 1.00),
    2: ("2 · brightness +6%", 1.06, 1.00, 1.00),
    3: ("3 · brightness +12%", 1.12, 1.00, 1.00),
    4: ("4 · lighter mids (gamma 0.92)", 1.00, 0.92, 1.00),
    5: ("5 · much lighter mids (gamma 0.86)", 1.00, 0.86, 1.00),
    6: ("6 · saturation +10%", 1.00, 1.00, 1.10),
    7: ("7 · bright +6% · sat +8%", 1.06, 1.00, 1.08),
    8: ("8 · gamma 0.90 · sat +10%", 1.00, 0.90, 1.10),
    9: ("9 · bright +10% · gamma 0.95 · sat +12%", 1.10, 0.95, 1.12),
}

# ==========================================
# SHADOW LIFT
# ==========================================
# Inkjet + matte lamination crush everything below ~60/255 into near-black
# (measured: an artist signature at level 27 over a level ~90 background is
# clearly visible on screen but disappears in print). Shadow lift raises the
# output black point so the darkest details stay separated; whites untouched.

SHADOW_LIFTS = {
    "Off": 0,
    "Soft (+8)": 8,
    "Medium (+14)": 14,
    "Strong (+20)": 20,
}

SHADOW_DEFAULT = "Off"

# Levels printed on the shadow test sheet
SHADOW_TEST_LEVELS = [0, 4, 8, 12, 16, 20, 24, 28, 32]

# ==========================================
# BLACK BORDER
# ==========================================
# Scans store their black border around 20/255, and the shadow lift pushes
# it higher still, so it prints as dark grey. This snaps it to true black.
# It auto-detects a genuine black border and leaves borderless, full-art and
# white-bordered cards completely untouched (no vignette).

BORDER_MODES = ["Off", "On (auto-detect)"]

BORDER_DEFAULT = "On (auto-detect)"

# ==========================================
# DUPLEX BACKS
# ==========================================
# Card back used for non-DFC cards on the duplex backs page. User-supplied
# (the official MTG back is Wizards material): drop a "back.png" (or .jpg)
# next to the exe. DFC cards use their own downloaded back face.

BACK_IMAGE_CANDIDATES = [ROOT / "back.png", ROOT / "back.jpg"]


def find_back_image():
    for p in BACK_IMAGE_CANDIDATES:
        if p.exists():
            return p
    return None


BACKS_MODES = ["No backs", "Duplex (DFC + back.png)"]

# ==========================================
# PERSISTED SETTINGS
# ==========================================

SETTINGS_FILE = ROOT / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass

# ==========================================
# WINDOW
# ==========================================

from version import APP_NAME as WINDOW_TITLE  # noqa: E402

WINDOW_WIDTH = 1040

WINDOW_HEIGHT = 720
