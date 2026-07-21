import os
import re
import shutil
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox
import numpy as np
from PIL import Image as PILImage, ImageDraw as PILDraw

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND = True
except Exception:
    _DND = False

from config import (
    WINDOW_TITLE,
    WINDOW_WIDTH,
    WINDOW_HEIGHT,
    MODELS,
    DEFAULT_MODEL,
    AUTO_MODEL,
    OUTPUT_FOLDER,
    SUPPORTED_INPUT,
    FIT_TO_CARD_DEFAULT,
    PARALLEL_JOBS,
    PDF_PAGE_SIZES,
    PDF_DEFAULT_PAGE,
    PDF_QUALITY_MODES,
    PDF_DEFAULT_QUALITY,
    PAGES_PER_FILE,
    PAGES_PER_FILE_DEFAULT,
    SHARPEN_MODES,
    SHARPEN_DEFAULT,
    SHADOW_LIFTS,
    SHADOW_DEFAULT,
    BORDER_MODES,
    BORDER_DEFAULT,
    CALIBRATION_PROFILES,
    BACKS_MODES,
    find_back_image,
    ICON_FILE,
    load_settings,
    save_settings,
)
from upscale import upscale
import scryfall
import print_sheet
import bootstrap
import update as app_update
from version import APP_NAME, APP_VERSION, DONATE_URL


ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# ---- palette: dark arena + MTG gold accent -------------------------------
BG        = "#101218"   # window
PANEL     = "#181b24"   # bars / frames
ROW       = "#1f2330"   # queue rows
GOLD      = "#d4a017"
GOLD_HOVER = "#b8860b"
GOLD_TEXT = "#171003"   # text on gold buttons
BLUE      = "#2c5f8a"   # secondary actions (import / export)
BLUE_HOVER = "#24486a"
GRAY_BTN  = "#2a2e3a"
GRAY_HOVER = "#39404f"
MUTED     = "#8a92a6"

# WUBRG accent dots for the header
MANA_COLORS = ["#f5e7bd", "#2f76b5", "#6b6570", "#d3202a", "#00733e"]

# short per-row model names -> full config labels (None = follow global menu)
ROW_MODELS = {
    "Auto": None,
    "Anime v3": "AnimeVideo v3 x4 (scanned cards)",
    "Hi-Fi": "High Fidelity x4 (digital renders)",
    "UltraSharp": "UltraSharp x4 (max crisp, flat art)",
    "x4+ real": "Real-ESRGAN x4+ (realistic art / faces)",
}

# status -> (dot color, text color)
STATUS_COLORS = {
    "pending":    ("#6b7280", "#9ca3af"),
    "processing": ("#3b82f6", "#93c5fd"),
    "done":       ("#22c55e", "#86efac"),
    "error":      ("#ef4444", "#fca5a5"),
}


# --------------------------------------------------------------------------
# root that also supports drag & drop (tkinterdnd2 mixed into CTk)
# --------------------------------------------------------------------------
if _DND:
    class _Root(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    _Root = ctk.CTk


# --------------------------------------------------------------------------
# a single row in the queue
# --------------------------------------------------------------------------
class QueueItem(ctk.CTkFrame):
    def __init__(self, master, ref, kind, on_remove, downloads=None, label=None,
                 qty=1, released_at=None, set_code=None, on_status=None):
        super().__init__(master, corner_radius=10, fg_color=ROW)
        self.ref = ref              # file path or scryfall reference
        self.kind = kind            # "file" | "scryfall" | "card"
        self.downloads = downloads  # for "card": [(basename, png_url), ...]
        self.qty = qty              # number of physical copies requested
        self.released_at = released_at  # "YYYY-MM-DD" or None (Auto mode hint)
        self.set_code = set_code    # Scryfall set code or None (Auto mode hint)
        self.outputs = []           # produced files (incl. qty copies)
        self.status = "pending"
        self.on_remove = on_remove
        self.on_status = on_status  # notified after every status change

        self.grid_columnconfigure(1, weight=1)

        self.dot = ctk.CTkLabel(self, text="●", width=18,
                                text_color=STATUS_COLORS["pending"][0],
                                font=("Segoe UI", 16))
        self.dot.grid(row=0, column=0, padx=(12, 6), pady=8)

        if label is not None:
            pass
        elif kind == "file":
            label = Path(ref).name
        else:
            label = ref
        self.name = ctk.CTkLabel(self, text=self._trim(label), anchor="w",
                                 font=("Segoe UI", 13))
        self.name.grid(row=0, column=1, sticky="ew", pady=(8, 0))

        self.info = ctk.CTkLabel(self, text="Pending", anchor="w",
                                 text_color=STATUS_COLORS["pending"][1],
                                 font=("Segoe UI", 11))
        self.info.grid(row=1, column=1, sticky="ew", pady=(0, 8))

        self.model_menu = ctk.CTkOptionMenu(
            self, values=list(ROW_MODELS.keys()), width=104, height=26,
            font=("Segoe UI", 11), fg_color=GRAY_BTN, button_color=GRAY_HOVER,
            command=self._model_changed)
        self.model_menu.set("Auto")
        self.model_menu.grid(row=0, column=2, rowspan=2, padx=(4, 2))

        self.bar = ctk.CTkProgressBar(self, height=6, width=120)
        self.bar.set(0)
        self.bar.grid(row=0, column=3, rowspan=2, padx=8)

        self.remove_btn = ctk.CTkButton(self, text="✕", width=30, height=30,
                                        fg_color="transparent", hover_color=GRAY_HOVER,
                                        command=lambda: on_remove(self))
        self.remove_btn.grid(row=0, column=4, rowspan=2, padx=(0, 10))

    @property
    def model_override(self):
        return ROW_MODELS.get(self.model_menu.get())

    def _model_changed(self, _choice):
        # picking a new model on a finished card re-queues it, so
        # UPSCALE ALL will redo it (output file gets overwritten)
        if self.status in ("done", "error"):
            self.set_status("pending", "Re-queued (model changed)", 0)

    def _trim(self, text, n=54):
        return text if len(text) <= n else text[: n - 1] + "…"

    def set_status(self, status, info=None, progress=None):
        changed = status != self.status
        self.status = status
        dot_c, txt_c = STATUS_COLORS[status]
        self.dot.configure(text_color=dot_c)
        self.info.configure(text=info or status.capitalize(), text_color=txt_c)
        if progress is not None:
            self.bar.set(progress)
        if status == "done":
            self.bar.set(1)
        if changed and self.on_status:
            self.on_status(self)


# --------------------------------------------------------------------------
# main application
# --------------------------------------------------------------------------
class App(_Root):
    def __init__(self):
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(900, 620)
        self.configure(fg_color=BG)
        try:
            self.iconbitmap(str(ICON_FILE))
        except Exception:
            pass

        self.items: list[QueueItem] = []
        self.running = False
        self.ai_ok = load_settings().get("gpu_ok", True)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_inputs()
        self._build_queue()
        self._build_footer()

        if _DND:
            self.queue_frame.drop_target_register(DND_FILES)
            self.queue_frame.dnd_bind("<<Drop>>", self._on_drop)

        # first-run setup (engine/models download + GPU probe)
        if bootstrap.missing_components():
            self.after(400, lambda: SetupDialog(self))
        elif "gpu_ok" not in load_settings():
            threading.Thread(target=self._probe_gpu_silent, daemon=True).start()

        # a failed update leaves the download behind; don't keep 38 MB around
        app_update.cleanup_leftovers()

        # silent update check
        threading.Thread(target=self._check_update, daemon=True).start()

    def _probe_gpu_silent(self):
        ok, name = bootstrap.probe_gpu()
        self.ai_ok = ok
        if not ok:
            self._ui(messagebox.showwarning, "No compatible GPU",
                     "No Vulkan-compatible GPU was found.\n\nThe app will "
                     "still work, but cards are resized without AI "
                     "enhancement.")

    def _check_update(self):
        info = app_update.check_for_update()
        if info:
            self._ui(self._show_update, info)

    def _show_update(self, info):
        self.update_btn = ctk.CTkButton(
            self.header, text=f"Update {info['version']} ↓", width=120,
            fg_color=GOLD, hover_color=GOLD_HOVER, text_color=GOLD_TEXT,
            command=lambda: self._apply_update(info))
        self.update_btn.pack(side="right", padx=4)

    def _apply_update(self, info):
        if self.running:
            messagebox.showinfo("Busy", "Finish the current batch first.")
            return
        if not messagebox.askyesno(
                "Update available",
                f"Version {info['version']} is available. Download and "
                f"restart now?\n\n{info['notes'][:400]}"):
            return
        self.update_btn.configure(state="disabled", text="Downloading…")

        def run():
            try:
                app_update.apply_update(info["url"])
                self._ui(self._quit_for_update)
            except Exception as e:
                self._ui(messagebox.showerror, "Update failed", str(e))
                self._ui(lambda: self.update_btn.configure(
                    state="normal", text=f"Update {info['version']} ↓"))

        threading.Thread(target=run, daemon=True).start()

    def _quit_for_update(self):
        """
        Exit immediately so the swap script can replace the exe. We skip the
        normal teardown on purpose: this process is being replaced, and a
        slow/erroring shutdown would keep the file locked.
        """
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)

    # ---------------------------------------------------------------- header
    def _build_header(self):
        self.header = header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 6))
        ctk.CTkLabel(header, text=APP_NAME,
                     font=("Georgia", 30, "bold"),
                     text_color=GOLD).pack(side="left")
        for color in MANA_COLORS:
            ctk.CTkLabel(header, text="●", font=("Segoe UI", 16),
                         text_color=color).pack(side="left", padx=2, pady=(12, 0))
        ctk.CTkLabel(header, text="  Scryfall · Gatherer → 1200 DPI print-ready cards",
                     font=("Segoe UI", 13),
                     text_color=MUTED).pack(side="left", pady=(14, 0))
        ctk.CTkLabel(header, text=f"v{APP_VERSION}", font=("Segoe UI", 11),
                     text_color=MUTED).pack(side="right", pady=(14, 0))
        ctk.CTkButton(header, text="♥ Donate", width=84, height=28,
                      fg_color="transparent", hover_color=GRAY_HOVER,
                      border_width=1, border_color=GOLD, text_color=GOLD,
                      font=("Segoe UI", 12),
                      command=lambda: webbrowser.open(DONATE_URL)).pack(
            side="right", padx=(4, 10), pady=(10, 0))

    # ---------------------------------------------------------------- inputs
    def _build_inputs(self):
        bar = ctk.CTkFrame(self, corner_radius=12, fg_color=PANEL)
        bar.grid(row=1, column=0, sticky="ew", padx=24, pady=8)
        bar.grid_columnconfigure(0, weight=1)

        self.ref_entry = ctk.CTkEntry(
            bar, height=40, fg_color=BG, border_color=GRAY_BTN,
            placeholder_text="Card name, Scryfall or Gatherer link  (e.g. Lightning Bolt, scryfall.com/card/…, gatherer.wizards.com/…)")
        self.ref_entry.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=12)
        self.ref_entry.bind("<Return>", lambda e: self._add_scryfall())

        ctk.CTkButton(bar, text="Add card", width=100, height=40,
                      fg_color=GOLD, hover_color=GOLD_HOVER, text_color=GOLD_TEXT,
                      font=("Segoe UI", 13, "bold"),
                      command=self._add_scryfall).grid(row=0, column=1, padx=4, pady=12)
        ctk.CTkButton(bar, text="Add files…", width=100, height=40,
                      fg_color=GRAY_BTN, hover_color=GRAY_HOVER,
                      command=self._add_files).grid(row=0, column=2, padx=4, pady=12)
        ctk.CTkButton(bar, text="Import list…", width=110, height=40,
                      fg_color=BLUE, hover_color=BLUE_HOVER,
                      command=self._open_import).grid(row=0, column=3, padx=(4, 12), pady=12)

    # ----------------------------------------------------------------- queue
    def _build_queue(self):
        wrap = ctk.CTkFrame(self, corner_radius=12, fg_color=PANEL)
        wrap.grid(row=2, column=0, sticky="nsew", padx=24, pady=8)
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(wrap, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        ctk.CTkLabel(top, text="Queue", text_color=GOLD,
                     font=("Segoe UI", 13, "bold")).pack(side="left")

        self.status_filter = "All"
        self.filter_btn = ctk.CTkSegmentedButton(
            top, values=["All", "Pending", "Processing", "Done", "Error"],
            command=self._set_filter, height=26,
            font=("Segoe UI", 11),
            fg_color=BG, unselected_color=BG, unselected_hover_color=GRAY_HOVER,
            selected_color=GOLD, selected_hover_color=GOLD_HOVER,
            text_color="#d7dbe4")
        self.filter_btn.set("All")
        self.filter_btn.pack(side="right")

        self.queue_frame = ctk.CTkScrollableFrame(
            wrap, corner_radius=10, fg_color=PANEL)
        self.queue_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.queue_frame.grid_columnconfigure(0, weight=1)

        hint = "Drag & drop images here" if _DND else "Use “Add files…” to add images"
        self.empty_hint = ctk.CTkLabel(
            self.queue_frame, text=f"\n\n{hint}\n",
            text_color=MUTED, font=("Segoe UI", 14))
        self.empty_hint.grid(row=0, column=0, pady=40)

    # ---------------------------------------------------------------- footer
    def _build_footer(self):
        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=3, column=0, sticky="ew", padx=24, pady=(4, 0))
        opts.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(opts, text="Model:").grid(row=0, column=0, padx=(0, 6))
        self.model_menu = ctk.CTkOptionMenu(
            opts, values=[AUTO_MODEL] + list(MODELS.keys()), width=250)
        self.model_menu.set(DEFAULT_MODEL)
        self.model_menu.grid(row=0, column=1, padx=(0, 16))

        self.fit_switch = ctk.CTkSwitch(opts, text="Fit to MTG card (1200 DPI)")
        if FIT_TO_CARD_DEFAULT:
            self.fit_switch.select()
        self.fit_switch.grid(row=0, column=2, padx=8)

        self.pdf_btn = ctk.CTkButton(opts, text="Export PDF…", width=120,
                                     fg_color=BLUE, hover_color=BLUE_HOVER,
                                     command=self._export_pdf)
        self.pdf_btn.grid(row=0, column=4, padx=2)

        ctk.CTkButton(opts, text="Open output folder", width=140,
                      fg_color=GRAY_BTN, hover_color=GRAY_HOVER,
                      command=self._open_output).grid(row=0, column=5, padx=(2, 0))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=24, pady=(8, 20))
        footer.grid_columnconfigure(0, weight=1)

        self.overall = ctk.CTkProgressBar(footer, height=12, progress_color=GOLD)
        self.overall.set(0)
        self.overall.grid(row=0, column=0, sticky="ew", padx=(0, 16))

        self.clear_btn = ctk.CTkButton(footer, text="Clear", width=90, height=44,
                                       fg_color=GRAY_BTN, hover_color=GRAY_HOVER,
                                       command=self._clear)
        self.clear_btn.grid(row=0, column=1, padx=4)

        self.start_btn = ctk.CTkButton(footer, text="UPSCALE ALL", width=180, height=44,
                                       font=("Segoe UI", 15, "bold"),
                                       fg_color=GOLD, hover_color=GOLD_HOVER,
                                       text_color=GOLD_TEXT,
                                       command=self._start)
        self.start_btn.grid(row=0, column=2)

    # ============================================================ queue ops
    def _refresh_empty(self):
        if self.items:
            self.empty_hint.grid_remove()
        else:
            self.empty_hint.grid()

    def _set_filter(self, value):
        self.status_filter = value
        for it in self.items:
            self._apply_filter(it)

    def _apply_filter(self, item):
        """Show/hide a row according to the active status filter."""
        if self.status_filter == "All" or item.status == self.status_filter.lower():
            item.grid()
        else:
            item.grid_remove()

    def _add_item(self, ref, kind, downloads=None, label=None, qty=1,
                  released_at=None, set_code=None):
        item = QueueItem(self.queue_frame, ref, kind, self._remove_item,
                         downloads=downloads, label=label, qty=qty,
                         released_at=released_at, set_code=set_code,
                         on_status=self._apply_filter)
        item.grid(row=len(self.items) + 1, column=0, sticky="ew", padx=6, pady=4)
        self.items.append(item)
        self._apply_filter(item)
        self._refresh_empty()

    def _remove_item(self, item):
        if self.running:
            return
        item.destroy()
        self.items.remove(item)
        for i, it in enumerate(self.items):
            it.grid_configure(row=i + 1)
        self._refresh_empty()

    def _add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_INPUT))
        files = filedialog.askopenfilenames(
            filetypes=[("Images", exts), ("All files", "*.*")])
        for f in files:
            self._add_item(f, "file")

    def _add_scryfall(self):
        ref = self.ref_entry.get().strip()
        if not ref:
            return
        self._add_item(ref, "scryfall")
        self.ref_entry.delete(0, "end")

    def _open_import(self):
        if self.running:
            return
        ImportDialog(self, on_resolved=self._add_resolved_cards)

    def _add_resolved_cards(self, cards):
        for c in cards:
            self._add_item(c["display"], "card",
                           downloads=c["downloads"], label=c["display"],
                           qty=c["qty"], released_at=c.get("released_at"),
                           set_code=c.get("set"))

    def _on_drop(self, event):
        # event.data is a brace/space separated list of paths
        paths = self.tk.splitlist(event.data)
        for p in paths:
            if Path(p).suffix.lower() in SUPPORTED_INPUT:
                self._add_item(p, "file")

    def _clear(self):
        if self.running:
            return
        for it in list(self.items):
            it.destroy()
        self.items.clear()
        self.overall.set(0)
        self._refresh_empty()

    def _open_output(self):
        os.startfile(OUTPUT_FOLDER)

    # ============================================================ pdf export
    def _export_images(self):
        """Cards to lay out: queue results first (order + copies), else
        everything in the output folder."""
        images = [p for it in self.items if it.status == "done"
                  for p in it.outputs if Path(p).exists()]
        source = "queue"
        if not images:
            images = sorted(OUTPUT_FOLDER.glob("*.png"))
            source = "output folder"
        return images, source

    def _export_pdf(self):
        if self.running:
            return
        images, source = self._export_images()
        if not images:
            messagebox.showerror("Nothing to export",
                                 "No upscaled cards found. Run UPSCALE ALL first.")
            return
        ExportDialog(self, images, source)

    # ================================================================ run
    def _start(self):
        if self.running:
            return
        if not self.items:
            messagebox.showerror("Nothing to do", "Add some cards or images first.")
            return

        self.running = True
        self.start_btn.configure(state="disabled", text="Working…")
        self.clear_btn.configure(state="disabled")
        self.overall.set(0)

        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        model = self.model_menu.get()
        fit = bool(self.fit_switch.get())
        pending = [it for it in self.items if it.status != "done"]
        total = len(pending)
        state = {"done": 0, "errors": 0}
        lock = threading.Lock()

        def process(item):
            try:
                self._process_item(item, model, fit)
            except Exception as e:
                with lock:
                    state["errors"] += 1
                self._ui(item.set_status, "error", f"Error: {e}")
            finally:
                with lock:
                    state["done"] += 1
                    progress = state["done"] / total
                self._ui(self.overall.set, progress)

        with ThreadPoolExecutor(max_workers=PARALLEL_JOBS) as pool:
            list(pool.map(process, pending))

        self.after(0, lambda: self._finish(total, state["errors"]))

    def _process_item(self, item, model, fit):
        self._ui(item.set_status, "processing", "Preparing…", 0)
        item.outputs = []

        # build the list of local files to upscale (2 for DFCs)
        if item.kind == "card":
            # already resolved by the importer -> just download
            targets = []
            for base, url in item.downloads:
                self._ui(item.set_status, "processing", "Downloading…")
                targets.append(str(scryfall.download_to_temp(base, url)))
            use_scryfall_name = True
        elif item.kind == "scryfall":
            paths, meta = scryfall.fetch(
                item.ref,
                status_callback=lambda t, it=item: self._ui(
                    it.set_status, "processing", t))
            targets = [str(p) for p in paths]
            item.released_at = meta.get("released_at")
            item.set_code = meta.get("set")
            use_scryfall_name = True
        else:
            targets = [item.ref]
            use_scryfall_name = False

        for t in targets:
            out = Path(upscale(
                t,
                model_label=item.model_override or model,
                fit_to_card=fit,
                rename=not use_scryfall_name,
                released_at=item.released_at,
                set_code=item.set_code,
                ai=self.ai_ok,
                progress_callback=lambda v, it=item: self._ui(
                    it.set_status, "processing", None, v),
                status_callback=lambda s, it=item: self._ui(
                    it.set_status, "processing", s),
            ))
            item.outputs.append(out)
            # one output file per requested copy (upscale once, copy)
            for n in range(2, item.qty + 1):
                copy = out.with_name(f"{out.stem} ({n}){out.suffix}")
                shutil.copy2(out, copy)
                item.outputs.append(copy)

        done_msg = f"Done ({item.qty} copies)" if item.qty > 1 else "Done"
        self._ui(item.set_status, "done", done_msg, 1)

    def _finish(self, total, errors):
        self.running = False
        self.start_btn.configure(state="normal", text="UPSCALE ALL")
        self.clear_btn.configure(state="normal")
        ok = total - errors
        if errors:
            failed = [it for it in self.items if it.status == "error"]
            names = "\n".join(
                f"  • {it.name.cget('text')} — {it.info.cget('text')}"
                for it in failed[:12])
            if len(failed) > 12:
                names += f"\n  … (+{len(failed) - 12} more)"
            messagebox.showwarning(
                "Finished with errors",
                f"{ok} succeeded, {errors} failed:\n\n{names}\n\n"
                f"Use the 'Error' filter above the queue to see them, "
                f"then UPSCALE ALL retries only the failed ones.")
        else:
            messagebox.showinfo(
                "Completed", f"{ok} image(s) upscaled to 1200 DPI.\nSaved to:\n{OUTPUT_FOLDER}")

    # ------------------------------------------------------------- ui helper
    def _ui(self, fn, *args):
        """Marshal a call onto the Tk main thread (no-op if window is gone)."""
        try:
            self.after(0, lambda: fn(*args))
        except RuntimeError:
            pass


# --------------------------------------------------------------------------
# decklist import dialog
# --------------------------------------------------------------------------
class ImportDialog(ctk.CTkToplevel):
    PLACEHOLDER = ("Paste a decklist or an Archidekt deck URL, e.g.\n\n"
                   "1 Winota, Joiner of Forces (PRM) 80807 [matte]\n"
                   "3 Plains (MSC) 866 [matte]\n"
                   "1 Ajani, Nacatl Pariah // Ajani, Nacatl Avenger (MH3) 442\n\n"
                   "https://archidekt.com/decks/1234567/my-deck\n\n"
                   "(Moxfield: use Export → copy the list and paste it here)")

    def __init__(self, master, on_resolved):
        super().__init__(master)
        self.on_resolved = on_resolved
        self.title("Import decklist")
        self.geometry("620x520")
        self.transient(master)
        self.after(60, self.grab_set)   # after window is viewable
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.configure(fg_color=BG)
        ctk.CTkLabel(self, text="Import from decklist",
                     font=("Georgia", 18, "bold"), text_color=GOLD).grid(
            row=0, column=0, sticky="w", padx=20, pady=(18, 4))

        self.textbox = ctk.CTkTextbox(self, font=("Consolas", 12), wrap="none",
                                      fg_color=PANEL, border_color=GRAY_BTN,
                                      border_width=1)
        self.textbox.grid(row=1, column=0, sticky="nsew", padx=20, pady=8)
        self.textbox.insert("1.0", self.PLACEHOLDER)
        self.textbox.bind("<FocusIn>", self._clear_placeholder)
        self._has_placeholder = True

        self.status = ctk.CTkLabel(self, text="Fetches the exact printing "
                                   "(set + number) from Scryfall.",
                                   text_color="#9ca3af", font=("Segoe UI", 12))
        self.status.grid(row=2, column=0, sticky="w", padx=20, pady=2)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="e", padx=20, pady=(6, 16))
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color=GRAY_BTN,
                      hover_color=GRAY_HOVER, command=self.destroy).pack(
            side="left", padx=6)
        self.import_btn = ctk.CTkButton(btns, text="Resolve & add", width=140,
                                        fg_color=GOLD, hover_color=GOLD_HOVER,
                                        text_color=GOLD_TEXT,
                                        font=("Segoe UI", 13, "bold"),
                                        command=self._do_import)
        self.import_btn.pack(side="left")

    def _clear_placeholder(self, _=None):
        if self._has_placeholder:
            self.textbox.delete("1.0", "end")
            self._has_placeholder = False

    def _do_import(self):
        if self._has_placeholder:
            return
        text = self.textbox.get("1.0", "end").strip()
        if not text:
            return
        self.import_btn.configure(state="disabled", text="Resolving…")
        threading.Thread(target=self._resolve, args=(text,), daemon=True).start()

    def _resolve(self, text):
        try:
            status = lambda s: self.after(
                0, lambda: self.status.configure(text=s, text_color="#9ca3af"))

            # deck site URLs
            kind = scryfall.deck_url_kind(text)
            if kind == "moxfield":
                raise scryfall.ScryfallError(
                    "Moxfield blocks external tools. In Moxfield use "
                    "Export → copy the decklist text and paste it here "
                    "instead — the format is supported directly.")
            if kind == "archidekt":
                text = scryfall.fetch_archidekt(text, status_callback=status)

            cards, not_found, bad = scryfall.resolve_decklist(
                text, status_callback=status)
            self.after(0, lambda: self._done(cards, not_found, bad))
        except Exception as e:
            self.after(0, lambda: self._failed(e))

    def _done(self, cards, not_found, bad):
        if cards:
            self.on_resolved(cards)

        problems = []
        if not_found:
            problems.append("Not found on Scryfall:\n  - " + "\n  - ".join(not_found))
        if bad:
            problems.append("Could not parse these lines:\n  - " + "\n  - ".join(bad))

        if problems:
            self.import_btn.configure(state="normal", text="Resolve & add")
            self.status.configure(
                text=f"Added {len(cards)}. {len(not_found)+len(bad)} issue(s) — see popup.",
                text_color="#fca5a5")
            messagebox.showwarning(
                "Imported with issues",
                f"Added {len(cards)} card(s) to the queue.\n\n" + "\n\n".join(problems),
                parent=self)
        else:
            self.destroy()

    def _failed(self, e):
        self.import_btn.configure(state="normal", text="Resolve & add")
        self.status.configure(text=f"Error: {e}", text_color="#fca5a5")


# --------------------------------------------------------------------------
# PDF export dialog with live preview
# --------------------------------------------------------------------------
_PAGE_MM = {"Letter": (215.9, 279.4), "A4": (210.0, 297.0)}

GUIDE_CHOICES = ["White", "Black", "Gray", "None"]
BLEED_COLOR_CHOICES = ["Black", "White"]


class ExportDialog(ctk.CTkToplevel):
    """Print-sheet export: layout, quality, color pipeline, duplex backs and
    a live preview of page 1. Choices persist in settings.json."""

    def __init__(self, master, images, source):
        super().__init__(master)
        self.images = images
        self.source = source
        self.title("Export print sheet")
        self.geometry("980x760")
        self.transient(master)
        self.after(60, self.grab_set)
        self.configure(fg_color=BG)

        self._thumbs = {}          # path -> raw thumbnail
        self._thumbs_b = {}        # path -> thumbnail with the border treated
        self._border_modes = {}    # path -> "auto" | "on" | "off"
        self._slots = []           # preview hit-boxes: (x0, y0, x1, y1, path)
        self._prev_job = None
        self._preview_img = None

        s = load_settings()

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="Export print sheet",
                     font=("Georgia", 18, "bold"), text_color=GOLD).grid(
            row=0, column=0, sticky="w", padx=20, pady=(16, 2))
        self.summary = ctk.CTkLabel(self, text="", text_color=MUTED,
                                    font=("Segoe UI", 12))
        self.summary.grid(row=0, column=1, sticky="w", padx=8, pady=(20, 2))

        # ------------------------------------------------ left: controls
        left = ctk.CTkScrollableFrame(self, width=430, fg_color=PANEL,
                                      corner_radius=12)
        left.grid(row=1, column=0, sticky="nsw", padx=(16, 8), pady=8)
        left.grid_columnconfigure(1, weight=1)
        self._r = 0

        def row(label, values, initial):
            ctk.CTkLabel(left, text=label, anchor="w").grid(
                row=self._r, column=0, sticky="w", padx=(12, 8), pady=4)
            menu = ctk.CTkOptionMenu(left, values=values, width=220,
                                     fg_color=GRAY_BTN, button_color=GRAY_HOVER,
                                     command=self._refresh_preview)
            menu.set(initial if initial in values else values[0])
            menu.grid(row=self._r, column=1, sticky="w", pady=4)
            self._r += 1
            return menu

        def entry_row(label, key, default, hint=""):
            ctk.CTkLabel(left, text=label, anchor="w").grid(
                row=self._r, column=0, sticky="w", padx=(12, 8), pady=4)
            fr = ctk.CTkFrame(left, fg_color="transparent")
            fr.grid(row=self._r, column=1, sticky="w", pady=4)
            e = ctk.CTkEntry(fr, width=56, fg_color=BG, border_color=GRAY_BTN)
            e.insert(0, str(s.get(key, default)))
            e.pack(side="left")
            e.bind("<KeyRelease>", self._refresh_preview)
            if hint:
                ctk.CTkLabel(fr, text=hint, text_color=MUTED,
                             font=("Segoe UI", 11)).pack(side="left", padx=6)
            self._r += 1
            return e

        def section(text):
            ctk.CTkLabel(left, text=text, text_color=GOLD,
                         font=("Segoe UI", 12, "bold")).grid(
                row=self._r, column=0, columnspan=2, sticky="w",
                padx=12, pady=(10, 2))
            self._r += 1

        profile_labels = [p[0] for p in CALIBRATION_PROFILES.values()]
        saved_profile = CALIBRATION_PROFILES.get(s.get("profile", 1),
                                                 CALIBRATION_PROFILES[1])[0]

        section("Layout")
        self.layout = row("Card grid", list(print_sheet.LAYOUTS.keys()),
                          s.get("layout", print_sheet.DEFAULT_LAYOUT))
        self.page = row("Page size", PDF_PAGE_SIZES,
                        s.get("page", PDF_DEFAULT_PAGE))
        self.split = row("File split", list(PAGES_PER_FILE.keys()),
                         s.get("split", PAGES_PER_FILE_DEFAULT))
        self.edge_bleed = entry_row("Edge bleed (mm)", "edge_bleed", 0.0,
                                    "0 = cards touching")
        self.bleed_color = row("Bleed color", BLEED_COLOR_CHOICES,
                               s.get("bleed_color", "Black"))
        self.guides = row("Cut guides", GUIDE_CHOICES,
                          s.get("guides", "White"))
        self.shift_down = entry_row("Shift down (mm)", "shift_down", 0.0,
                                    "late paper feed")

        section("Image pipeline (PDF only, masters untouched)")
        self.quality = row("Quality", list(PDF_QUALITY_MODES.keys()),
                           s.get("quality", PDF_DEFAULT_QUALITY))
        self.profile = row("Color profile", profile_labels, saved_profile)
        self.sharpen = row("Sharpening", list(SHARPEN_MODES.keys()),
                           s.get("sharpen", SHARPEN_DEFAULT))
        self.shadow = row("Shadow lift", list(SHADOW_LIFTS.keys()),
                          s.get("shadow", SHADOW_DEFAULT))
        self.border = row("Deepen black border", BORDER_MODES,
                          s.get("border", BORDER_DEFAULT))

        section("Duplex backs")
        self.backs = row("Card backs", BACKS_MODES,
                         s.get("backs", BACKS_MODES[0]))
        self.back_dx = entry_row("Back offset X (mm)", "back_dx", 0.0)
        self.back_dy = entry_row("Back offset Y (mm)", "back_dy", 0.0)
        self.back_bleed = entry_row("Back bleed (mm)", "back_bleed", 1.5,
                                    "covers duplex drift")

        section("Test sheets")
        tests = ctk.CTkFrame(left, fg_color="transparent")
        tests.grid(row=self._r, column=0, columnspan=2, sticky="w",
                   padx=12, pady=4)
        self._r += 1
        self.cal_btn = ctk.CTkButton(
            tests, text="Calibration...", width=120,
            fg_color=GRAY_BTN, hover_color=GRAY_HOVER,
            command=self._calibration)
        self.cal_btn.pack(side="left", padx=(0, 8))
        self.shadow_btn = ctk.CTkButton(
            tests, text="Shadow test...", width=120,
            fg_color=GRAY_BTN, hover_color=GRAY_HOVER,
            command=self._shadow_test)
        self.shadow_btn.pack(side="left")

        self.status = ctk.CTkLabel(left, text="", text_color=MUTED,
                                   font=("Segoe UI", 11))
        self.status.grid(row=self._r, column=0, columnspan=2, sticky="w",
                         padx=12, pady=(6, 8))
        self._r += 1

        # ------------------------------------------------ right: preview
        right = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 16), pady=8)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 2))
        head.grid_columnconfigure(0, weight=1)
        self.preview_title = ctk.CTkLabel(head, text="Preview",
                                          text_color=MUTED, font=("Segoe UI", 12))
        self.preview_title.grid(row=0, column=0)
        self.side_btn = ctk.CTkSegmentedButton(
            head, values=["Fronts", "Backs"], height=24,
            font=("Segoe UI", 11), command=lambda _v: self._draw_preview(),
            fg_color=BG, unselected_color=BG, unselected_hover_color=GRAY_HOVER,
            selected_color=GOLD, selected_hover_color=GOLD_HOVER,
            text_color="#d7dbe4")
        self.side_btn.set("Fronts")
        self.side_btn.grid(row=0, column=1, sticky="e")
        self.preview_label = ctk.CTkLabel(right, text="Loading preview...",
                                          text_color=MUTED)
        self.preview_label.grid(row=1, column=0, pady=(0, 10))
        self.preview_label.bind("<Button-1>", self._preview_click)
        ctk.CTkLabel(right, text="Click a card to cycle its black border:  "
                     "auto → force off → force on",
                     text_color=MUTED, font=("Segoe UI", 11)).grid(
            row=2, column=0, pady=(0, 8))

        # ------------------------------------------------ bottom buttons
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=2, column=0, columnspan=2, sticky="e",
                  padx=20, pady=(0, 14))
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color=GRAY_BTN,
                      hover_color=GRAY_HOVER, command=self.destroy).pack(
            side="left", padx=6)
        self.export_btn = ctk.CTkButton(btns, text="Export", width=130,
                                        fg_color=GOLD, hover_color=GOLD_HOVER,
                                        text_color=GOLD_TEXT,
                                        font=("Segoe UI", 13, "bold"),
                                        command=self._export)
        self.export_btn.pack(side="left")

        self._draw_preview()
        threading.Thread(target=self._load_thumbs, daemon=True).start()

    # ------------------------------------------------------------- values
    def _profile_id(self):
        label = self.profile.get()
        for pid, prof in CALIBRATION_PROFILES.items():
            if prof[0] == label:
                return pid
        return 1

    def _float(self, widget, default=0.0, lo=None, hi=None):
        try:
            v = float(widget.get())
        except ValueError:
            return default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def _offsets(self):
        return (self._float(self.back_dx), self._float(self.back_dy))

    def _shift(self):
        return self._float(self.shift_down, 0.0, 0.0)

    def _bleed(self):
        return self._float(self.back_bleed, 1.5, 0.0, 3.0)

    def _edge_bleed(self):
        return self._float(self.edge_bleed, 0.0, 0.0, 2.0)

    def _persist(self):
        dx, dy = self._offsets()
        s = load_settings()
        s.update({
            "layout": self.layout.get(),
            "page": self.page.get(),
            "quality": self.quality.get(),
            "split": self.split.get(),
            "profile": self._profile_id(),
            "sharpen": self.sharpen.get(),
            "shadow": self.shadow.get(),
            "border": self.border.get(),
            "backs": self.backs.get(),
            "back_dx": dx,
            "back_dy": dy,
            "back_bleed": self._bleed(),
            "edge_bleed": self._edge_bleed(),
            "bleed_color": self.bleed_color.get(),
            "guides": self.guides.get(),
            "shift_down": self._shift(),
        })
        save_settings(s)

    def _set_status(self, text):
        self.after(0, lambda: self.status.configure(text=text))

    # ------------------------------------------------------------- preview
    _PREVIEW_BOX = (470, 560)   # max preview pixels (w, h)

    def _sheet_images(self):
        """
        What actually goes on the sheets, matching the export exactly:
        (fronts, backs) with backs=None when duplex is off. In duplex mode a
        card's own back face stops being a front of its own.
        """
        if self.backs.get() != BACKS_MODES[0]:
            fronts, backs = self._pairs()
            default = find_back_image()
            backs = [b or default for b in backs]
            return fronts, backs
        return list(self.images), None

    def _load_thumbs(self):
        fronts, backs = self._sheet_images()
        wanted = list(fronts[:12]) + [b for b in (backs or [])[:12] if b]
        for p in wanted:
            key = str(p)
            if key in self._thumbs:
                continue
            try:
                src = PILImage.open(p).convert("RGBA")
                # treat at a workable size so detection behaves like it will
                # at full resolution, then shrink for display
                mid = src.resize((420, 586))
                flat = PILImage.new("RGB", mid.size, (0, 0, 0))
                flat.paste(mid, mask=mid.split()[3])
                self._thumbs[key] = flat.resize((200, 279))
                opaque = np.asarray(mid.split()[3]) > 250
                self._thumbs_b[key] = print_sheet._deepen_black_border(
                    flat, opaque).resize((200, 279))
            except (OSError, ValueError):
                continue
        try:
            self.after(0, self._draw_preview)
        except RuntimeError:
            pass

    def _refresh_preview(self, *_):
        if self._prev_job:
            try:
                self.after_cancel(self._prev_job)
            except Exception:
                pass
        # duplex on/off changes which files are needed as thumbnails
        threading.Thread(target=self._load_thumbs, daemon=True).start()
        self._prev_job = self.after(200, self._draw_preview)

    def _draw_preview(self):
        cols, rows, landscape = print_sheet.LAYOUTS.get(
            self.layout.get(), print_sheet.LAYOUTS[print_sheet.DEFAULT_LAYOUT])
        pw, ph = _PAGE_MM.get(self.page.get(), _PAGE_MM["A4"])
        if landscape:
            pw, ph = ph, pw
        s = min(self._PREVIEW_BOX[0] / pw, self._PREVIEW_BOX[1] / ph)
        W, H = int(pw * s), int(ph * s)
        img = PILImage.new("RGB", (W, H), (255, 255, 255))
        d = PILDraw.Draw(img)

        per_page = cols * rows
        self._slots = []
        border_on = self.border.get() != BORDER_MODES[0]
        fronts, backs = self._sheet_images()
        duplex = backs is not None
        showing_backs = duplex and self.side_btn.get() == "Backs"
        page_items = backs if showing_backs else fronts
        eb = self._edge_bleed()
        g = 2 * eb
        bw = cols * 63 + (cols - 1) * g
        bh = rows * 88 + (rows - 1) * g
        left = (pw - bw) / 2
        top = (ph - bh) / 2 + self._shift()
        if ph - top - bh < 3:
            top = ph - bh - 3

        def X(v):
            return int(v * s)

        bleed_fill = {"Black": (10, 10, 10), "White": (250, 250, 250)}.get(
            self.bleed_color.get(), (10, 10, 10))

        cw, ch = X(left + 63) - X(left), X(top + 88) - X(top)

        # bleed frames + cards
        for idx in range(per_page):
            col, rw = idx % cols, idx // cols
            # backs print column-mirrored so they land behind their front
            slot = rw * cols + (cols - 1 - col) if showing_backs else idx
            x = left + col * (63 + g)
            y = top + rw * (88 + g)
            if slot < len(page_items):
                if eb > 0:
                    d.rectangle([X(x - eb), X(y - eb),
                                 X(x + 63 + eb), X(y + 88 + eb)],
                                fill=bleed_fill, outline=(210, 210, 215))
                item = page_items[slot]
                key = str(item) if item else None
                mode = self._border_modes.get(key, "auto")
                treated = mode == "on" or (mode == "auto" and border_on)
                pool = self._thumbs_b if treated else self._thumbs
                t = (pool.get(key) or self._thumbs.get(key)) if key else None
                if t:
                    img.paste(t.resize((cw, ch)), (X(x), X(y)))
                    self._slots.append((X(x), X(y), X(x + 63), X(y + 88), key))
                    if mode != "auto":
                        col = (90, 190, 110) if mode == "on" else (210, 110, 110)
                        d.rectangle([X(x) + 3, X(y) + 3, X(x) + 36, X(y) + 18],
                                    fill=col)
                        d.text((X(x) + 8, X(y) + 5),
                               "ON" if mode == "on" else "OFF", fill=(15, 15, 20))
                else:
                    d.rectangle([X(x), X(y), X(x + 63), X(y + 88)],
                                fill=(55, 60, 76))
                    if showing_backs and not item:
                        d.text((X(x) + cw // 2 - 26, X(y) + ch // 2),
                               "back.png\nmissing", fill=(190, 120, 120))

        # guide crosses
        gc = {"White": (255, 255, 255), "Black": (0, 0, 0),
              "Gray": (120, 120, 120), "None": None}.get(self.guides.get())
        xs, ys = set(), set()
        for c_ in range(cols):
            xs.add(left + c_ * (63 + g)); xs.add(left + c_ * (63 + g) + 63)
        for c_ in range(rows):
            ys.add(top + c_ * (88 + g)); ys.add(top + c_ * (88 + g) + 88)
        if gc:
            for x in xs:
                for y in ys:
                    d.line([X(x), X(max(y - 4, top)), X(x), X(min(y + 4, top + bh))],
                           fill=gc, width=1)
                    d.line([X(max(x - 4, left)), X(y), X(min(x + 4, left + bw)), X(y)],
                           fill=gc, width=1)

        # margin ticks
        for x in xs:
            d.line([X(x), X(top - 5), X(x), X(top - 1)], fill=(120, 125, 135))
            d.line([X(x), X(top + bh + 1), X(x), X(top + bh + 5)], fill=(120, 125, 135))
        for y in ys:
            d.line([X(left - 5), X(y), X(left - 1), X(y)], fill=(120, 125, 135))
            d.line([X(left + bw + 1), X(y), X(left + bw + 5), X(y)], fill=(120, 125, 135))

        d.rectangle([0, 0, W - 1, H - 1], outline=(185, 190, 200))

        self._preview_img = ctk.CTkImage(light_image=img, dark_image=img,
                                         size=(W, H))
        self.preview_label.configure(image=self._preview_img, text="")

        sheets = -(-len(fronts) // per_page)          # sheets of paper
        pages = sheets * 2 if duplex else sheets      # PDF pages
        side = "backs, mirrored" if showing_backs else "fronts"
        self.preview_title.configure(text=f"Sheet 1 of {sheets} — {side}")
        self.side_btn.configure(state="normal" if duplex else "disabled")
        if not duplex:
            self.side_btn.set("Fronts")

        extra = f" ({len(self.images)} files, backs paired)" if duplex else ""
        self.summary.configure(
            text=f"{len(fronts)} card(s) from the {self.source}{extra} -> "
                 f"{sheets} sheet(s), {pages} PDF page(s)")

    def _preview_click(self, event):
        """Cycle one card's black border: auto -> force off -> force on."""
        if not self._preview_img:
            return
        iw, ih = self._preview_img.cget("size")        # image is centred
        ox = max(0, (self.preview_label.winfo_width() - iw) // 2)
        oy = max(0, (self.preview_label.winfo_height() - ih) // 2)
        px, py = event.x - ox, event.y - oy
        nxt = {"auto": "off", "off": "on", "on": "auto"}
        for x0, y0, x1, y1, key in self._slots:
            if key and x0 <= px <= x1 and y0 <= py <= y1:
                self._border_modes[key] = nxt[self._border_modes.get(key, "auto")]
                self._draw_preview()
                return

    # ------------------------------------------------------------- actions
    def _export(self):
        target = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".pdf",
            initialdir=OUTPUT_FOLDER,
            initialfile="print-sheet.pdf",
            filetypes=[("PDF", "*.pdf")])
        if not target:
            return

        self._persist()
        self.export_btn.configure(state="disabled", text="Exporting...")

        duplex = self.backs.get() != BACKS_MODES[0]
        if duplex:
            images, backs = self._pairs()
        else:
            images, backs = self.images, None

        args = dict(
            layout=self.layout.get(),
            page_name=self.page.get(),
            quality=self.quality.get(),
            sharpen_name=self.sharpen.get(),
            profile_id=self._profile_id(),
            shadow_name=self.shadow.get(),
            deepen_border=self.border.get() != BORDER_MODES[0],
            border_modes=dict(self._border_modes),
            pages_per_file=PAGES_PER_FILE.get(self.split.get(), 0),
            backs=backs,
            back_offset=self._offsets(),
            back_bleed_mm=self._bleed(),
            edge_bleed_mm=self._edge_bleed(),
            bleed_color=self.bleed_color.get(),
            guide_color=self.guides.get(),
            shift_down_mm=self._shift(),
        )

        def build():
            try:
                files = print_sheet.build_pdf(
                    images, target,
                    status_callback=self._set_status, **args)
                self.after(0, lambda: self._done(files))
            except Exception as e:
                self.after(0, lambda: self._failed(e))

        threading.Thread(target=build, daemon=True).start()

    _FRONT_RE = re.compile(r"-front( \(\d+\))?$")
    _BACK_RE = re.compile(r"-back( \(\d+\))?$")

    def _pairs(self):
        """Pair DFC faces: returns (fronts, backs) where backs[i] is the
        card's own back face, or None -> use the shared back.png."""
        paths = [Path(p) for p in self.images]
        stems = {p.stem: p for p in paths}
        fronts, backs = [], []
        for p in paths:
            st = p.stem
            if self._BACK_RE.search(st) and \
                    self._BACK_RE.sub(r"-front\1", st) in stems:
                continue
            b = None
            if self._FRONT_RE.search(st):
                b = stems.get(self._FRONT_RE.sub(r"-back\1", st))
            fronts.append(p)
            backs.append(b)
        return fronts, backs

    def _done(self, files):
        total_mb = sum(Path(f).stat().st_size for f in files) / 1e6
        names = "\n".join(Path(f).name for f in files[:8])
        if len(files) > 8:
            names += f"\n... (+{len(files) - 8} more)"
        messagebox.showinfo(
            "PDF ready",
            f"{len(self.images)} card(s) -> {len(files)} file(s), "
            f"{total_mb:.0f} MB total:\n\n{names}", parent=self)
        self.destroy()

    def _failed(self, e):
        self.export_btn.configure(state="normal", text="Export")
        messagebox.showerror("Export failed", str(e), parent=self)

    def _calibration(self):
        target = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".pdf",
            initialdir=OUTPUT_FOLDER,
            initialfile="calibration.pdf",
            filetypes=[("PDF", "*.pdf")])
        if not target:
            return
        self.cal_btn.configure(state="disabled", text="Building...")
        card = self.images[0]
        page = self.page.get()
        shift = self._shift()

        def build():
            try:
                print_sheet.build_calibration(
                    card, target, page, shift_down_mm=shift,
                    status_callback=self._set_status)
                self.after(0, lambda: self._cal_done(target))
            except Exception as e:
                self.after(0, lambda: self._failed_cal(e))

        threading.Thread(target=build, daemon=True).start()

    def _cal_done(self, target):
        self.cal_btn.configure(state="normal", text="Calibration...")
        self._set_status("")
        messagebox.showinfo(
            "Calibration sheet ready",
            "Print it at 100% scale with printer color correction OFF.\n"
            "Compare the 9 numbered variants against a real card, then pick "
            f"that number in 'Color profile'.\n\n{target}", parent=self)

    def _failed_cal(self, e):
        self.cal_btn.configure(state="normal", text="Calibration...")
        messagebox.showerror("Calibration failed", str(e), parent=self)

    def _shadow_test(self):
        target = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".pdf",
            initialdir=OUTPUT_FOLDER,
            initialfile="shadow-test.pdf",
            filetypes=[("PDF", "*.pdf")])
        if not target:
            return
        self.shadow_btn.configure(state="disabled", text="Building...")
        card = self.images[0]
        page = self.page.get()
        profile = self._profile_id()
        shift = self._shift()

        def build():
            try:
                print_sheet.build_shadow_test(
                    card, target, page, profile, shift_down_mm=shift,
                    status_callback=self._set_status)
                self.after(0, lambda: self._shadow_done(target))
            except Exception as e:
                self.after(0, lambda: self._shadow_failed(e))

        threading.Thread(target=build, daemon=True).start()

    def _shadow_done(self, target):
        self.shadow_btn.configure(state="normal", text="Shadow test...")
        self._set_status("")
        messagebox.showinfo(
            "Shadow test ready",
            "Print AND laminate it like a real order, then look at the "
            "darkest details (artist signature, shadow texture).\n\n"
            "Pick the lowest +N where they become visible and set it in "
            f"'Shadow lift'.\n\n{target}", parent=self)

    def _shadow_failed(self, e):
        self.shadow_btn.configure(state="normal", text="Shadow test...")
        messagebox.showerror("Shadow test failed", str(e), parent=self)


# --------------------------------------------------------------------------
# first-run setup: download engine + models, probe GPU
# --------------------------------------------------------------------------
class SetupDialog(ctk.CTkToplevel):
    def __init__(self, master):
        super().__init__(master)
        self.master_app = master
        self.title("First-run setup")
        self.geometry("520x300")
        self.transient(master)
        self.after(60, self.grab_set)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # not closable mid-setup

        ctk.CTkLabel(self, text="Setting up the AI engine",
                     font=("Georgia", 18, "bold"), text_color=GOLD).pack(
            anchor="w", padx=24, pady=(20, 4))
        ctk.CTkLabel(
            self,
            text="This app downloads its AI engine and models from their\n"
                 "official sources on first run (~110 MB total):\n"
                 "  •  Real-ESRGAN engine + base models (BSD license)\n"
                 "  •  UltraSharp / High Fidelity models (Upscayl project)\n\n"
                 "This happens only once.",
            justify="left", text_color=MUTED,
            font=("Segoe UI", 12)).pack(anchor="w", padx=24)

        self.bar = ctk.CTkProgressBar(self, width=460, progress_color=GOLD)
        self.bar.set(0)
        self.bar.pack(padx=24, pady=(16, 4))
        self.status = ctk.CTkLabel(self, text="Starting…", text_color=MUTED,
                                   font=("Segoe UI", 11))
        self.status.pack(anchor="w", padx=24)

        threading.Thread(target=self._run, daemon=True).start()

    def _progress(self, text, frac):
        self.after(0, lambda: (self.status.configure(text=text),
                               self.bar.set(frac)))

    def _run(self):
        try:
            bootstrap.download_all(self._progress)
            self._progress("Detecting GPU…", 0.99)
            ok, name = bootstrap.probe_gpu()
            self.master_app.ai_ok = ok
            self.after(0, lambda: self._finish(ok, name))
        except Exception as e:
            self.after(0, lambda: self._fail(e))

    def _finish(self, ok, name):
        self.grab_release()
        self.destroy()
        if ok:
            messagebox.showinfo(
                "Ready",
                f"Setup complete.\nGPU detected: {name}\n\n"
                "AI upscaling is fully enabled.")
        else:
            messagebox.showwarning(
                "Ready (no GPU)",
                f"Setup complete, but no Vulkan-compatible GPU was found "
                f"({name}).\n\nThe app will work with plain high-quality "
                "resizing instead of AI upscaling.")

    def _fail(self, e):
        self.grab_release()
        self.destroy()
        messagebox.showerror(
            "Setup failed",
            f"Could not download required components:\n\n{e}\n\n"
            "Check your internet connection and restart the app to retry.")
