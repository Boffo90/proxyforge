"""
In-app auto-update via GitHub Releases.

Flow: on startup a background thread asks the GitHub API for the latest
release of GITHUB_REPO. If its tag is newer than APP_VERSION and it has an
.exe asset, the UI shows an "Update" button. Applying it downloads the new
exe next to the current one and spawns a tiny .bat that waits for this
process to exit, swaps the files and relaunches. Fails silently when
offline or when the repo does not exist yet.
"""

import os
import subprocess
import sys
from pathlib import Path

import requests

from config import ROOT
from version import APP_VERSION, GITHUB_REPO

_UA = {"User-Agent": f"ProxyForge/{APP_VERSION}",
       "Accept": "application/vnd.github+json"}


def _parse(v: str) -> tuple:
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def check_for_update() -> dict | None:
    """Returns {"version", "url", "notes"} when a newer release exists."""
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers=_UA, timeout=15)
        if r.status_code != 200:
            return None
        rel = r.json()
        tag = rel.get("tag_name", "")
        if _parse(tag) <= _parse(APP_VERSION):
            return None
        # prefer the bare app exe; never grab the installer by mistake
        assets = rel.get("assets", [])
        best = None
        for asset in assets:
            name = asset.get("name", "").lower()
            if not name.endswith(".exe"):
                continue
            if name == "proxyforge.exe":
                best = asset
                break
            if "setup" not in name and "install" not in name and best is None:
                best = asset
        if best:
            return {
                "version": tag,
                "url": best["browser_download_url"],
                "notes": (rel.get("body") or "")[:2000],
            }
    except requests.RequestException:
        pass
    return None


def apply_update(url: str, progress_callback=None):
    """
    Download the new exe and restart into it. Only meaningful when frozen;
    from source it raises so the caller can show a message.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Auto-update only works in the packaged .exe")

    current = Path(sys.executable)
    new = current.with_name(current.stem + "_new.exe")

    r = requests.get(url, headers=_UA, timeout=600, stream=True)
    r.raise_for_status()
    total = int(r.headers.get("Content-Length") or 0)
    got = 0
    with open(new, "wb") as f:
        for chunk in r.iter_content(1 << 18):
            f.write(chunk)
            got += len(chunk)
            if progress_callback and total:
                progress_callback(got / total)

    bat = ROOT / "_update.bat"
    bat.write_text(
        "@echo off\r\n"
        ":wait\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        f'tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul && goto wait\r\n'
        f'move /y "{new}" "{current}" >nul\r\n'
        f'start "" "{current}"\r\n'
        'del "%~f0"\r\n',
        encoding="ascii")

    subprocess.Popen(["cmd", "/c", str(bat)],
                     creationflags=subprocess.CREATE_NO_WINDOW,
                     cwd=str(ROOT))
    # caller should now close the app
