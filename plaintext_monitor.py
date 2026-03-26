#!/usr/bin/env python3
"""
PlainText Monitor  —  v1.0
A system-tray app that monitors the clipboard for rich text (HTML / RTF) and
offers to strip the formatting, leaving only plain text on the clipboard.

Requirements:  pip install pywin32 pystray Pillow keyboard
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

# ── Graceful import check ──────────────────────────────────────────────────────
_missing: list[str] = []
try:
    import win32clipboard
    import win32con
except ImportError:
    _missing.append("pywin32")
try:
    import pystray
except ImportError:
    _missing.append("pystray")
try:
    from PIL import Image, ImageDraw
except ImportError:
    _missing.append("Pillow")
try:
    import keyboard as kb
except ImportError:
    _missing.append("keyboard")

if _missing:
    _root = tk.Tk()
    _root.withdraw()
    import tkinter.messagebox as _mb
    _mb.showerror(
        "Missing packages",
        "Please install the following packages and try again:\n\n"
        + "\n".join(f"  pip install {p}" for p in _missing)
        + "\n\nOr run:  pip install -r requirements.txt",
    )
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
# Constants & defaults
# ══════════════════════════════════════════════════════════════════════════════

APP_NAME      = "PlainText Monitor"
BASE_DIR      = os.path.dirname(os.path.abspath(sys.argv[0]))
SETTINGS_FILE = os.path.join(BASE_DIR, "plaintext_settings.json")
POLL_INTERVAL = 0.25   # seconds between clipboard polls

DEFAULTS: dict = {
    "default_yes":  True,           # True → Yes is the highlighted/default button
    "auto_convert": False,          # skip the prompt, convert silently
    "paused":       False,          # monitoring paused
    "hotkey":       "ctrl+shift+p", # hotkey to copy selection as plain text
}

# ══════════════════════════════════════════════════════════════════════════════
# Settings
# ══════════════════════════════════════════════════════════════════════════════

class Settings:
    def __init__(self) -> None:
        self._data: dict = DEFAULTS.copy()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE) as fh:
                    self._data.update(json.load(fh))
            except Exception:
                pass

    def _save(self) -> None:
        try:
            with open(SETTINGS_FILE, "w") as fh:
                json.dump(self._data, fh, indent=2)
        except Exception:
            pass

    def get(self, key: str):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def update(self, d: dict) -> None:
        with self._lock:
            self._data.update(d)
            self._save()

# ══════════════════════════════════════════════════════════════════════════════
# Clipboard helpers
# ══════════════════════════════════════════════════════════════════════════════

_CF_HTML: int | None = None
_CF_RTF:  int | None = None


def _init_formats() -> None:
    global _CF_HTML, _CF_RTF
    if _CF_HTML is None:
        _CF_HTML = win32clipboard.RegisterClipboardFormat("HTML Format")
        _CF_RTF  = win32clipboard.RegisterClipboardFormat("Rich Text Format")


def clip_seq() -> int:
    """Returns clipboard sequence number — increments on every write."""
    return ctypes.windll.user32.GetClipboardSequenceNumber()


def _open(retries: int = 8) -> bool:
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:
            time.sleep(0.04)
    return False


def _close() -> None:
    try:
        win32clipboard.CloseClipboard()
    except Exception:
        pass


def clipboard_is_rich() -> bool:
    """Return True if clipboard has rich-text formats (HTML / RTF) alongside plain text."""
    _init_formats()
    if not _open():
        return False
    try:
        has_txt  = win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT)
        has_rich = (
            win32clipboard.IsClipboardFormatAvailable(_CF_HTML)
            or win32clipboard.IsClipboardFormatAvailable(_CF_RTF)
        )
        return bool(has_txt and has_rich)
    except Exception:
        return False
    finally:
        _close()


def get_plain_text() -> str | None:
    """Return CF_UNICODETEXT from clipboard, or None on failure."""
    if not _open():
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
    except Exception:
        pass
    finally:
        _close()
    return None


def set_plain_text(text: str) -> bool:
    """Replace clipboard contents with plain Unicode text, stripping all rich formats."""
    if not _open():
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        return True
    except Exception:
        return False
    finally:
        _close()

# ══════════════════════════════════════════════════════════════════════════════
# Tray icon
# ══════════════════════════════════════════════════════════════════════════════

def make_icon(paused: bool = False) -> "Image.Image":
    size  = 64
    img   = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw  = ImageDraw.Draw(img)
    color = (120, 120, 120) if paused else (41, 128, 185)
    draw.rounded_rectangle([2, 2, 61, 61], radius=14, fill=color)
    # "T" letterform symbolising plain Text
    draw.rectangle([13, 15, 50, 24], fill="white")   # crossbar
    draw.rectangle([26, 24, 37, 52], fill="white")   # stem
    if paused:
        # Pause indicator — two small vertical bars in bottom-right corner
        draw.rectangle([38, 40, 44, 56], fill=(255, 255, 255, 160))
        draw.rectangle([48, 40, 54, 56], fill=(255, 255, 255, 160))
    return img

# ══════════════════════════════════════════════════════════════════════════════
# Dialogs
# ══════════════════════════════════════════════════════════════════════════════

class ConvertDialog(tk.Toplevel):
    """Ask whether to convert rich clipboard content to plain text."""

    def __init__(self, parent: tk.Tk, preview: str, default_yes: bool) -> None:
        super().__init__(parent)
        self.result: bool | None = None
        self.title(APP_NAME)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._no)

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer,
                  text="Rich text detected on clipboard.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(outer,
                  text="Would you like to convert it to plain text?",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=(4, 12))

        pf = ttk.LabelFrame(outer, text=" Content preview ", padding=8)
        pf.pack(fill=tk.X, pady=(0, 14))
        snip = (preview[:220] + " …") if len(preview) > 220 else preview
        ttk.Label(pf, text=snip, wraplength=400,
                  justify=tk.LEFT, foreground="#555").pack(anchor="w")

        bf = ttk.Frame(outer)
        bf.pack()
        yes_btn = ttk.Button(bf, text="Yes", width=11, command=self._yes)
        no_btn  = ttk.Button(bf, text="No",  width=11, command=self._no)
        yes_btn.grid(row=0, column=0, padx=6)
        no_btn.grid( row=0, column=1, padx=6)

        if default_yes:
            yes_btn.focus_set()
            self.bind("<Return>", lambda _: self._yes())
        else:
            no_btn.focus_set()
            self.bind("<Return>", lambda _: self._no())

        self.bind("<Escape>", lambda _: self._no())
        self._center()
        self.grab_set()
        self.lift()
        self.wait_window()

    def _center(self) -> None:
        self.update_idletasks()
        w, h   = self.winfo_reqwidth(), self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _yes(self) -> None:
        self.result = True
        self.destroy()

    def _no(self) -> None:
        self.result = False
        self.destroy()


class SettingsDialog(tk.Toplevel):
    """Settings window."""

    def __init__(self, parent: tk.Tk, app: "App") -> None:
        super().__init__(parent)
        self.app = app
        self.title(f"{APP_NAME}  —  Settings")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        s = app.settings
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)

        # ── Monitoring ──────────────────────────────────────────────────────
        mf = ttk.LabelFrame(outer, text=" Monitoring ", padding=(12, 8))
        mf.pack(fill=tk.X, pady=(0, 10))

        self.paused_var = tk.BooleanVar(value=s.get("paused"))
        ttk.Checkbutton(mf, text="Pause monitoring  (keeps running in tray)",
                        variable=self.paused_var).pack(anchor="w")

        self.auto_var = tk.BooleanVar(value=s.get("auto_convert"))
        ttk.Checkbutton(mf,
                        text="Auto-convert rich text without prompting",
                        variable=self.auto_var).pack(anchor="w", pady=(6, 0))

        # ── Prompt default ──────────────────────────────────────────────────
        df = ttk.LabelFrame(outer, text=" Prompt Default ", padding=(12, 8))
        df.pack(fill=tk.X, pady=(0, 10))

        self.default_var = tk.StringVar(
            value="yes" if s.get("default_yes") else "no")
        ttk.Radiobutton(df, text="Yes  (convert to plain text)",
                        variable=self.default_var, value="yes").pack(anchor="w")
        ttk.Radiobutton(df, text="No   (keep rich formatting)",
                        variable=self.default_var, value="no").pack(
            anchor="w", pady=(4, 0))

        # ── Hotkey ─────────────────────────────────────────────────────────
        hf = ttk.LabelFrame(
            outer,
            text=" Hotkey — Copy Selection as Plain Text ",
            padding=(12, 8),
        )
        hf.pack(fill=tk.X, pady=(0, 14))

        row = ttk.Frame(hf)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Hotkey:").pack(side=tk.LEFT)
        self.hotkey_var = tk.StringVar(value=s.get("hotkey"))
        ttk.Entry(row, textvariable=self.hotkey_var, width=26).pack(
            side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            hf,
            text="When pressed: copies selection then strips rich formatting.\n"
                 "Examples:  ctrl+shift+p   ctrl+alt+c   alt+shift+v",
            foreground="gray",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 0))

        # ── Buttons ─────────────────────────────────────────────────────────
        bf = ttk.Frame(outer)
        bf.pack(pady=(4, 0))
        ttk.Button(bf, text="Save",   width=10, command=self._save).pack(
            side=tk.LEFT, padx=5)
        ttk.Button(bf, text="Cancel", width=10, command=self.destroy).pack(
            side=tk.LEFT, padx=5)

        self._center()
        self.grab_set()
        self.lift()
        self.wait_window()

    def _center(self) -> None:
        self.update_idletasks()
        w, h   = self.winfo_reqwidth(), self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _save(self) -> None:
        old_hk = self.app.settings.get("hotkey")
        new_hk = self.hotkey_var.get().strip()
        self.app.settings.update({
            "paused":       self.paused_var.get(),
            "auto_convert": self.auto_var.get(),
            "default_yes":  self.default_var.get() == "yes",
            "hotkey":       new_hk,
        })
        if old_hk != new_hk:
            self.app.rebind_hotkey(old_hk, new_hk)
        self.app.refresh_tray()
        self.destroy()

# ══════════════════════════════════════════════════════════════════════════════
# Application
# ══════════════════════════════════════════════════════════════════════════════

class App:
    def __init__(self) -> None:
        self.settings = Settings()
        self._q: queue.Queue        = queue.Queue()
        self._suppress_until: float = 0.0   # suppress monitor until this time()
        self._dialog_open: bool     = False
        self._tray: pystray.Icon | None = None

        # ── Tkinter hidden root ─────────────────────────────────────────────
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        try:
            ttk.Style(self.root).theme_use("vista")
        except tk.TclError:
            pass

        self.root.after(120, self._drain)

        # ── Hotkey ──────────────────────────────────────────────────────────
        self._bind_hotkey(self.settings.get("hotkey"))

        # ── Background threads ──────────────────────────────────────────────
        threading.Thread(
            target=self._monitor, daemon=True, name="ClipboardMonitor"
        ).start()
        threading.Thread(
            target=self._run_tray, daemon=True, name="TrayThread"
        ).start()

    # ── Hotkey ──────────────────────────────────────────────────────────────

    def _bind_hotkey(self, hk: str) -> None:
        if not hk:
            return
        try:
            kb.add_hotkey(hk, self._hotkey_triggered, suppress=True)
        except Exception as exc:
            print(f"[PlainText] Cannot bind hotkey '{hk}': {exc}")

    def _unbind_hotkey(self, hk: str) -> None:
        if not hk:
            return
        try:
            kb.remove_hotkey(hk)
        except Exception:
            pass

    def rebind_hotkey(self, old: str, new: str) -> None:
        self._unbind_hotkey(old)
        self._bind_hotkey(new)

    def _hotkey_triggered(self) -> None:
        """
        Hotkey handler:
          1. Simulate Ctrl+C to copy the current selection.
          2. Wait briefly for clipboard to update.
          3. Strip rich formats if present.
        """
        # Suppress monitor for the next ~1.5 s to ignore changes we cause
        self._suppress_until = time.time() + 1.5

        before = clip_seq()
        # Send Ctrl+C to copy whatever is selected
        kb.send("ctrl+c")

        # Wait up to 400 ms for the clipboard to be written
        deadline = time.time() + 0.4
        while time.time() < deadline:
            if clip_seq() != before:
                break
            time.sleep(0.05)

        # Strip rich formatting if present
        if clipboard_is_rich():
            text = get_plain_text()
            if text:
                set_plain_text(text)

    # ── Clipboard monitor (background thread) ───────────────────────────────

    def _monitor(self) -> None:
        last = clip_seq()
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                seq = clip_seq()
                if seq == last:
                    continue
                last = seq

                # Skip events caused by our own clipboard writes
                if time.time() < self._suppress_until:
                    continue

                if self.settings.get("paused"):
                    continue

                if not clipboard_is_rich():
                    continue

                text = get_plain_text() or ""
                if not text.strip():
                    continue

                if self.settings.get("auto_convert"):
                    self._suppress_until = time.time() + 0.6
                    set_plain_text(text)
                else:
                    self._q.put(("prompt", text))

            except Exception as exc:
                print(f"[PlainText] Monitor error: {exc}")

    # ── Queue drain — runs on main / tkinter thread ──────────────────────────

    def _drain(self) -> None:
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == "prompt" and not self._dialog_open:
                    self._show_prompt(data)
                elif kind == "settings":
                    self._open_settings_window()
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _show_prompt(self, text: str) -> None:
        self._dialog_open = True
        try:
            dlg = ConvertDialog(
                self.root, text, self.settings.get("default_yes")
            )
            if dlg.result:
                self._suppress_until = time.time() + 0.6
                set_plain_text(text)
        finally:
            self._dialog_open = False

    def _open_settings_window(self) -> None:
        SettingsDialog(self.root, self)

    # ── Convert now (used by tray menu "Convert Clipboard Now") ─────────────

    def _convert_now(self) -> None:
        if clipboard_is_rich():
            text = get_plain_text()
            if text:
                self._suppress_until = time.time() + 0.6
                set_plain_text(text)

    # ── System tray ─────────────────────────────────────────────────────────

    def _make_menu(self) -> pystray.Menu:
        paused = self.settings.get("paused")
        return pystray.Menu(
            pystray.MenuItem(
                "Convert Clipboard Now",
                lambda icon, item: self._convert_now(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Resume Monitoring" if paused else "Pause Monitoring",
                self._toggle_pause,
            ),
            pystray.MenuItem(
                "Settings…",
                lambda icon, item: self._q.put(("settings", None)),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _run_tray(self) -> None:
        self._tray = pystray.Icon(
            APP_NAME,
            make_icon(self.settings.get("paused")),
            APP_NAME,
            self._make_menu(),
        )
        self._tray.run()

    def refresh_tray(self) -> None:
        if not self._tray:
            return
        paused = self.settings.get("paused")
        self._tray.icon  = make_icon(paused)
        self._tray.menu  = self._make_menu()
        self._tray.title = (
            f"{APP_NAME}  ({'Paused' if paused else 'Active'})"
        )

    def _toggle_pause(self, icon=None, item=None) -> None:
        self.settings.set("paused", not self.settings.get("paused"))
        self.refresh_tray()

    def _quit(self, icon=None, item=None) -> None:
        try:
            kb.unhook_all_hotkeys()
        except Exception:
            pass
        if self._tray:
            self._tray.stop()
        self.root.after(0, self.root.destroy)

    # ── Entry point ─────────────────────────────────────────────────────────

    def run(self) -> None:
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    App().run()
