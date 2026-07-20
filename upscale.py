"""
Upscale pipeline.

    input image (any format)
        -> normalize to PNG if needed
        -> Real-ESRGAN x4 (ncnn / vulkan)
        -> resize to exact MTG card size (optional)
        -> embed 1200 DPI metadata
        -> output PNG
"""

import re
import subprocess
from pathlib import Path

from PIL import Image

from config import (
    REALESRGAN_EXE,
    OUTPUT_FOLDER,
    TEMP_FOLDER,
    MODELS,
    DEFAULT_MODEL,
    AUTO_MODEL,
    RENDER_ERA_START,
    AUTO_DIGITAL_MAX_BYTES,
    REALISTIC_SETS,
    AUTO_DIGITAL_MODEL,
    AUTO_REALISTIC_MODEL,
    AUTO_SCAN_MODEL,
    SUPPORTED_INPUT,
    TARGET_DPI,
    CARD_WIDTH_PX,
    CARD_HEIGHT_PX,
    FIT_TO_CARD_DEFAULT,
)

# Prevents the Windows console window from flashing.
CREATE_NO_WINDOW = 0x08000000

_percent_re = re.compile(r"(\d+[.,]\d+)%")


def generate_output_name(file: Path) -> str:
    """
    ltr-123-Lightning Bolt   ->   Lightning Bolt-ltr-123.png

    (Scryfall downloads already come as Name-set-number, which has 3 parts
    too but in the desired order, so they pass through unchanged-ish.)
    """
    parts = file.stem.split("-", 2)
    if len(parts) != 3:
        return file.stem + ".png"
    setcode, number, fullname = parts
    return f"{fullname}-{setcode}-{number}.png"


def _normalize_input(file: Path, status_callback=None) -> Path:
    """
    Real-ESRGAN reads png/jpg/webp. Anything else (or images with odd modes)
    is converted to a temporary PNG first.
    """
    if file.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return file

    if status_callback:
        status_callback(f"Converting {file.suffix} to PNG...")

    im = Image.open(file)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    temp = TEMP_FOLDER / (file.stem + "_in.png")
    im.save(temp)
    return temp


def _postprocess(path: Path, fit_to_card: bool):
    """Resize to exact card size (optional) and stamp DPI metadata in place."""
    im = Image.open(path)

    if fit_to_card and im.size != (CARD_WIDTH_PX, CARD_HEIGHT_PX):
        im = im.resize((CARD_WIDTH_PX, CARD_HEIGHT_PX), Image.LANCZOS)

    im.save(path, "PNG", dpi=(TARGET_DPI, TARGET_DPI))


def upscale(
    file_path,
    model_label: str = DEFAULT_MODEL,
    fit_to_card: bool = FIT_TO_CARD_DEFAULT,
    rename: bool = True,
    released_at: str | None = None,
    set_code: str | None = None,
    ai: bool = True,
    progress_callback=None,
    status_callback=None,
):
    """
    Upscale a single image and return the output Path.

    rename      -> apply the legacy "set-number-name" -> "name-set-number"
                   renaming (True for user files). Scryfall downloads are
                   already correctly named, so pass rename=False for those.
    released_at -> "YYYY-MM-DD" from Scryfall, used by Auto mode to pick
                   the model by image era (scan vs official render).
    set_code    -> Scryfall set code; sets in REALISTIC_SETS divert Auto
                   to the face-friendly model.
    progress_callback(value)  -> value in 0..1
    status_callback(text)     -> short text for the GUI
    """
    file = Path(file_path)

    if not file.exists():
        raise FileNotFoundError(file)

    if not ai or not REALESRGAN_EXE.exists():
        # No compatible GPU (or engine missing): plain high-quality resize.
        # Still yields a correctly sized 1200 DPI file, just without the AI
        # detail reconstruction.
        source = _normalize_input(file, status_callback)
        out_name = generate_output_name(file) if rename else file.stem + ".png"
        output = OUTPUT_FOLDER / out_name
        if status_callback:
            status_callback("Resizing (no GPU — AI disabled)…")
        im = Image.open(source)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        im = im.resize((CARD_WIDTH_PX, CARD_HEIGHT_PX), Image.LANCZOS)
        im.save(output, "PNG", dpi=(TARGET_DPI, TARGET_DPI))
        if progress_callback:
            progress_callback(1.0)
        if status_callback:
            status_callback("Done (resize only)")
        return output

    if model_label == AUTO_MODEL:
        if set_code and set_code.lower() in REALISTIC_SETS:
            # photorealistic art (Marvel etc.): UltraSharp breaks faces/hands
            kind, why = "realistic", f"set {set_code.upper()}"
            model_label = AUTO_REALISTIC_MODEL
        elif released_at:
            # date is authoritative: Scryfall switched from scans to official
            # WotC renders with LTR (June 2023)
            digital = released_at >= RENDER_ERA_START
            kind = "render" if digital else "scan"
            why = f"released {released_at}"
            model_label = AUTO_DIGITAL_MODEL if digital else AUTO_SCAN_MODEL
        else:
            # no date (local file): renders compress small, scans weigh more
            size = file.stat().st_size
            digital = size < AUTO_DIGITAL_MAX_BYTES
            kind = "render" if digital else "scan"
            why = f"{size/1e6:.2f} MB"
            model_label = AUTO_DIGITAL_MODEL if digital else AUTO_SCAN_MODEL
        if status_callback:
            status_callback(f"Auto: {kind} ({why}) -> {MODELS[model_label][0]}")

    model_name, scale = MODELS.get(model_label, MODELS[AUTO_SCAN_MODEL])

    source = _normalize_input(file, status_callback)

    out_name = generate_output_name(file) if rename else file.stem + ".png"
    output = OUTPUT_FOLDER / out_name

    cmd = [
        str(REALESRGAN_EXE),
        "-i", str(source),
        "-o", str(output),
        "-n", model_name,
        "-s", str(scale),
    ]

    if status_callback:
        status_callback("Upscaling...")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        creationflags=CREATE_NO_WINDOW,
    )

    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        line = line.strip()
        if not line:
            continue

        match = _percent_re.search(line)
        if match:
            percent = float(match.group(1).replace(",", ".")) / 100
            # reserve the last 10% for post-processing
            if progress_callback:
                progress_callback(percent * 0.9)
            if status_callback:
                status_callback(f"Upscaling... {percent*100:.0f}%")

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(f"Real-ESRGAN exited with code {process.returncode}")

    if status_callback:
        status_callback("Finalizing (DPI / size)...")

    _postprocess(output, fit_to_card)

    if progress_callback:
        progress_callback(1.0)
    if status_callback:
        status_callback("Done")

    return output
