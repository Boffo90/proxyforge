"""
First-run bootstrap: downloads the AI engine + models, probes the GPU.

The distributed exe ships WITHOUT the Real-ESRGAN engine or any model
(license-clean: everything is fetched from its official source on first
run, with attribution):

  - Engine + base models (BSD-3): xinntao/Real-ESRGAN official release zip
  - ultrasharp-4x, high-fidelity-4x (CC-BY-NC-SA community models):
    fetched from the open-source Upscayl repository (AGPL app, models
    redistributed there by the Upscayl project)

GPU probe: the ncnn engine is Vulkan-only. We run a tiny upscale once and
parse the result; without a compatible GPU the app falls back to a
no-AI mode (plain high-quality resize) instead of crashing.
"""

import io
import subprocess
import zipfile
from pathlib import Path

import requests
from PIL import Image

from config import ROOT, MODELS_FOLDER, REALESRGAN_EXE, TEMP_FOLDER, \
    load_settings, save_settings

CREATE_NO_WINDOW = 0x08000000

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MTGEnhancer"}

ENGINE_ZIP_URL = ("https://github.com/xinntao/Real-ESRGAN/releases/download/"
                  "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip")

UPSCAYL_RAW = "https://raw.githubusercontent.com/upscayl/upscayl/main/resources/models/"

# files that must exist next to the exe / in models/
ENGINE_FILES = ["realesrgan-ncnn-vulkan.exe", "vcomp140.dll"]
MODEL_FILES = [
    "realesrgan-x4plus.bin", "realesrgan-x4plus.param",
    "realesr-animevideov3-x4.bin", "realesr-animevideov3-x4.param",
    "ultrasharp-4x.bin", "ultrasharp-4x.param",
    "high-fidelity-4x.bin", "high-fidelity-4x.param",
]

# names of upscayl-hosted models (subset of MODEL_FILES)
_UPSCAYL_MODELS = {"ultrasharp-4x", "high-fidelity-4x"}


def missing_components() -> list[str]:
    missing = [f for f in ENGINE_FILES if not (ROOT / f).exists()]
    missing += [f for f in MODEL_FILES if not (MODELS_FOLDER / f).exists()]
    return missing


def download_all(progress_callback=None):
    """Download every missing component. progress_callback(text, fraction)."""
    MODELS_FOLDER.mkdir(exist_ok=True)
    missing = set(missing_components())
    if not missing:
        return

    def report(text, frac):
        if progress_callback:
            progress_callback(text, frac)

    steps = 0
    total_steps = 0

    need_zip = any(f in missing for f in ENGINE_FILES) or any(
        f in missing and f.rsplit(".", 1)[0] not in _UPSCAYL_MODELS
        for f in MODEL_FILES)
    need_upscayl = [m for m in _UPSCAYL_MODELS
                    if f"{m}.bin" in missing or f"{m}.param" in missing]
    total_steps = (1 if need_zip else 0) + len(need_upscayl)
    if total_steps == 0:
        return

    # ---- engine + base models from the official Real-ESRGAN release zip
    if need_zip:
        report("Downloading AI engine (Real-ESRGAN, ~43 MB)…", 0.02)
        r = requests.get(ENGINE_ZIP_URL, headers=_UA, timeout=300, stream=True)
        r.raise_for_status()
        buf = io.BytesIO()
        got = 0
        total = int(r.headers.get("Content-Length") or 0)
        for chunk in r.iter_content(1 << 18):
            buf.write(chunk)
            got += len(chunk)
            if total:
                report(f"Downloading AI engine… {got // 1048576} / {total // 1048576} MB",
                       0.02 + 0.68 * (got / total) / max(total_steps, 1))
        buf.seek(0)
        with zipfile.ZipFile(buf) as z:
            for info in z.infolist():
                name = Path(info.filename).name
                if not name:
                    continue
                if name in ENGINE_FILES:
                    (ROOT / name).write_bytes(z.read(info))
                elif name in MODEL_FILES:
                    (MODELS_FOLDER / name).write_bytes(z.read(info))
        steps += 1

    # ---- community models from the Upscayl open-source repository
    for m in need_upscayl:
        for ext in (".param", ".bin"):
            fname = m + ext
            report(f"Downloading model {fname}…",
                   0.72 + 0.26 * steps / max(total_steps, 1))
            r = requests.get(UPSCAYL_RAW + fname, headers=_UA, timeout=300)
            r.raise_for_status()
            (MODELS_FOLDER / fname).write_bytes(r.content)
        steps += 1

    report("Downloads complete", 0.98)

    still = missing_components()
    if still:
        raise RuntimeError("Some components could not be downloaded:\n"
                           + "\n".join(still))


def probe_gpu() -> tuple[bool, str]:
    """
    Run a tiny real upscale to find out whether a Vulkan GPU works.
    Returns (ok, gpu_name_or_reason) and stores the result in settings.
    """
    ok, detail = False, "engine not present"
    if REALESRGAN_EXE.exists():
        src = TEMP_FOLDER / "_probe.png"
        dst = TEMP_FOLDER / "_probe_out.png"
        Image.new("RGB", (32, 32), (128, 60, 60)).save(src)
        try:
            p = subprocess.run(
                [str(REALESRGAN_EXE), "-i", str(src), "-o", str(dst),
                 "-n", "realesr-animevideov3", "-s", "4", "-v"],
                capture_output=True, text=True, timeout=120,
                creationflags=CREATE_NO_WINDOW)
            out = (p.stderr or "") + (p.stdout or "")
            name = ""
            for line in out.splitlines():
                if line.startswith("[0 ") and "]" in line:
                    name = line[3:line.index("]")].strip()
                    break
            if p.returncode == 0 and dst.exists():
                ok, detail = True, (name or "Vulkan GPU")
            else:
                ok, detail = False, (name or "no compatible Vulkan GPU")
        except Exception as e:
            ok, detail = False, str(e)
        finally:
            for f in (src, dst):
                try:
                    f.unlink()
                except OSError:
                    pass

    s = load_settings()
    s["gpu_ok"] = ok
    s["gpu_name"] = detail
    save_settings(s)
    return ok, detail
