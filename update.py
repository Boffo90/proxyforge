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


def cleanup_leftovers():
    """
    Remove the download left behind by an update that did not complete.
    (_update.bat is not touched: it deletes itself, and after a successful
    update it is still running as this process's parent.)
    """
    try:
        if getattr(sys, "frozen", False):
            leftover = Path(sys.executable).with_name(
                Path(sys.executable).stem + "_new.exe")
            if leftover.exists():
                leftover.unlink()
    except OSError:
        pass


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

    _write_swap_script(current, new)
    # caller should now close the app


def _write_swap_script(current: Path, new: Path) -> Path:
    """
    Write and launch the script that swaps the exe once this app has exited.

    A PyInstaller onefile app is TWO processes (bootloader + child), so we
    wait on the image name rather than a PID: waiting on os.getpid() only
    sees the child, and moving the exe while the bootloader still holds it
    fails silently — which then relaunches the OLD exe alongside the dying
    one and corrupts its _MEI temp dir.
    """
    # Absolute System32 paths + ping-based sleeps: "timeout" needs a console
    # (we launch without one) and both it and tasklist can be shadowed by
    # Unix ports on a developer's PATH.
    bat = ROOT / "_update.bat"
    bat.write_text(
        "@echo off\r\n"
        "setlocal enableextensions\r\n"
        'set "SYS=%SystemRoot%\\System32"\r\n'
        'set "SLEEP=%SYS%\\ping.exe -n 2 127.0.0.1"\r\n'
        f'set "EXE={current.name}"\r\n'
        f'set "CUR={current}"\r\n'
        f'set "NEW={new}"\r\n'
        "\r\n"
        ":: wait for every instance (bootloader + child) to exit\r\n"
        "set /a waited=0\r\n"
        ":wait\r\n"
        "%SLEEP% >nul\r\n"
        '%SYS%\\tasklist.exe /FI "IMAGENAME eq %EXE%" 2>nul | '
        '%SYS%\\find.exe /I "%EXE%" >nul\r\n'
        "if errorlevel 1 goto gone\r\n"
        "set /a waited+=1\r\n"
        "if %waited% lss 60 goto wait\r\n"
        ":: still running after ~60s - abort and keep the current exe\r\n"
        'del /f /q "%NEW%" >nul 2>&1\r\n'
        "goto end\r\n"
        "\r\n"
        ":gone\r\n"
        ":: let the bootloader release the file and clean its temp dir\r\n"
        "%SLEEP% >nul\r\n"
        "%SLEEP% >nul\r\n"
        "set /a tries=0\r\n"
        ":trymove\r\n"
        'move /y "%NEW%" "%CUR%" >nul 2>&1\r\n'
        "if not errorlevel 1 goto done\r\n"
        "set /a tries+=1\r\n"
        "if %tries% geq 15 goto giveup\r\n"
        "%SLEEP% >nul\r\n"
        "goto trymove\r\n"
        "\r\n"
        ":giveup\r\n"
        ":: could not replace it - leave the old exe working, drop the download\r\n"
        'del /f /q "%NEW%" >nul 2>&1\r\n'
        "goto end\r\n"
        "\r\n"
        ":done\r\n"
        ":: let the filesystem settle before running the new exe\r\n"
        "%SYS%\\ping.exe -n 4 127.0.0.1 >nul\r\n"
        ":: Run it directly instead of via START. A onefile app launched\r\n"
        ":: detached (start / Start-Process / explorer) fails to unpack its\r\n"
        ":: Python DLL; run as a child of this script it unpacks fine. This\r\n"
        ":: cmd then simply waits here for the app's lifetime.\r\n"
        '"%CUR%"\r\n'
        "\r\n"
        ":end\r\n"
        'del "%~f0"\r\n',
        # newline="" or Python turns every \r\n into \r\r\n, which cmd
        # mis-parses
        encoding="ascii", newline="")

    subprocess.Popen(["cmd", "/c", str(bat)],
                     creationflags=subprocess.CREATE_NO_WINDOW,
                     cwd=str(ROOT))
    return bat
