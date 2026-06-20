#!/usr/bin/env python3
"""
PlainText for Claude  —  v1.2
A system-tray app with a single job: squish multi-line / indented text
(e.g. Claude AI output) into a single line with no indentation.

Hotkey (default Ctrl+Shift+L):
  - Simulates Ctrl+C to copy the current selection
  - Strips leading/trailing whitespace from every line
  - Drops blank lines
  - Joins everything into one line and puts it back on the clipboard

URL hotkey (default Ctrl+Shift+U):
  - Same as above, but joins WITHOUT spaces — perfect for wrapped URLs

Left-click the tray icon to squish whatever is on the clipboard instantly.

Requirements:  pip install pywin32 pystray Pillow
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
import winreg
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

# ── Win32 hotkey helpers ───────────────────────────────────────────────────────

_WM_HOTKEY    = 0x0312
_HOTKEY_ID_TEXT = 1
_HOTKEY_ID_URL  = 2
_MOD_NOREPEAT = 0x4000

_MOD_MAP: dict[str, int] = {
    "ctrl": 0x0002, "control": 0x0002,
    "shift": 0x0004,
    "alt": 0x0001,
    "win": 0x0008,
}
_VK_MAP: dict[str, int] = {
    "a":0x41,"b":0x42,"c":0x43,"d":0x44,"e":0x45,"f":0x46,"g":0x47,
    "h":0x48,"i":0x49,"j":0x4A,"k":0x4B,"l":0x4C,"m":0x4D,"n":0x4E,
    "o":0x4F,"p":0x50,"q":0x51,"r":0x52,"s":0x53,"t":0x54,"u":0x55,
    "v":0x56,"w":0x57,"x":0x58,"y":0x59,"z":0x5A,
    "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
    "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
    "0":0x30,"1":0x31,"2":0x32,"3":0x33,"4":0x34,
    "5":0x35,"6":0x36,"7":0x37,"8":0x38,"9":0x39,
}


def _parse_hotkey(hk_str: str) -> tuple[int, int]:
    mods = _MOD_NOREPEAT
    vk   = 0
    for part in hk_str.lower().split("+"):
        part = part.strip()
        if part in _MOD_MAP:
            mods |= _MOD_MAP[part]
        elif part in _VK_MAP:
            vk = _VK_MAP[part]
    return mods, vk


# SendInput structures
_PUL = ctypes.POINTER(ctypes.c_ulong)

class _KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                 ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                 ("dwExtraInfo", _PUL)]

class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                 ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                 ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]

class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short),
                 ("wParamH", ctypes.c_ushort)]

class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput)]

class _Input(ctypes.Structure):
    _anonymous_ = ("ii",)
    _fields_    = [("type", ctypes.c_ulong), ("ii", _InputUnion)]

class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                 ("wParam", ctypes.c_ulong), ("lParam", ctypes.c_long),
                 ("time", ctypes.c_ulong), ("pt", ctypes.c_long * 2)]


def _send_ctrl_c() -> None:
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL      = 0x11
    VK_C            = 0x43
    events = (_Input * 4)()
    events[0].type = 1; events[0].ki.wVk = VK_CONTROL
    events[1].type = 1; events[1].ki.wVk = VK_C
    events[2].type = 1; events[2].ki.wVk = VK_C;       events[2].ki.dwFlags = KEYEVENTF_KEYUP
    events[3].type = 1; events[3].ki.wVk = VK_CONTROL; events[3].ki.dwFlags = KEYEVENTF_KEYUP
    ctypes.windll.user32.SendInput(4, events, ctypes.sizeof(_Input))


class _HotkeyThread(threading.Thread):
    """Manages multiple RegisterHotKey bindings in a dedicated message-loop thread."""
    _WM_QUIT  = 0x0012
    _WM_USER  = 0x0400
    _MSG_REBIND = _WM_USER + 1

    def __init__(self) -> None:
        super().__init__(daemon=True, name="HotkeyThread")
        self._bindings: dict[int, dict] = {}   # id -> {callback, pending, current}
        self._tid: int = 0
        self._ready = threading.Event()

    def add(self, hk_id: int, callback, hk_str: str) -> None:
        self._bindings[hk_id] = {"callback": callback, "pending": hk_str, "current": ""}

    def wait_ready(self) -> None:
        self._ready.wait()

    def rebind(self, hk_id: int, hk_str: str) -> None:
        if hk_id in self._bindings:
            self._bindings[hk_id]["pending"] = hk_str
        ctypes.windll.user32.PostThreadMessageW(self._tid, self._MSG_REBIND, hk_id, 0)

    def stop(self) -> None:
        ctypes.windll.user32.PostThreadMessageW(self._tid, self._WM_QUIT, 0, 0)

    def run(self) -> None:
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self._ready.set()
        for hk_id in self._bindings:
            self._do_bind(hk_id)

        msg = _MSG()
        user32 = ctypes.windll.user32
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0 or result == -1:
                break
            if msg.message == _WM_HOTKEY and msg.wParam in self._bindings:
                self._bindings[msg.wParam]["callback"]()
            elif msg.message == self._MSG_REBIND:
                self._do_bind(msg.wParam)

    def _do_bind(self, hk_id: int) -> None:
        b = self._bindings.get(hk_id)
        if not b:
            return
        user32 = ctypes.windll.user32
        if b["current"]:
            user32.UnregisterHotKey(None, hk_id)
            b["current"] = ""
        hk_str = b["pending"]
        if hk_str:
            mods, vk = _parse_hotkey(hk_str)
            if vk and user32.RegisterHotKey(None, hk_id, mods, vk):
                b["current"] = hk_str
            elif vk:
                print(f"[PlainText for Claude] Cannot register hotkey '{hk_str}'")

# ── Clipboard helpers ──────────────────────────────────────────────────────────

def clip_seq() -> int:
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


def get_plain_text() -> str | None:
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


def squish_text(text: str) -> str:
    """Strip indentation from every line and collapse to a single line (with spaces)."""
    parts = [line.strip() for line in text.splitlines()]
    return " ".join(p for p in parts if p)


def squish_url(text: str) -> str:
    """Strip indentation from every line and join with NO spaces — for wrapped URLs."""
    parts = [line.strip() for line in text.splitlines()]
    return "".join(p for p in parts if p)

# ── Constants & defaults ───────────────────────────────────────────────────────

APP_NAME      = "PlainText for Claude"
BASE_DIR      = os.path.dirname(os.path.abspath(sys.argv[0]))
SETTINGS_FILE = os.path.join(BASE_DIR, "plaintext_claude_settings.json")

DEFAULTS: dict = {
    "hotkey":     "ctrl+shift+l",
    "hotkey_url": "ctrl+shift+u",
}

# ── Windows startup (registry) helpers ────────────────────────────────────────

_STARTUP_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "PlainTextForClaude"


def _startup_target() -> str:
    """Command stored in the registry — run.bat beside this script."""
    bat = os.path.join(BASE_DIR, "run.bat")
    return f'"{bat}"'


def _startup_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY)
        winreg.QueryValueEx(key, _STARTUP_REG_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def _set_startup(enabled: bool) -> None:
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
        access=winreg.KEY_SET_VALUE,
    )
    if enabled:
        winreg.SetValueEx(key, _STARTUP_REG_NAME, 0, winreg.REG_SZ, _startup_target())
    else:
        try:
            winreg.DeleteValue(key, _STARTUP_REG_NAME)
        except OSError:
            pass
    winreg.CloseKey(key)

# ── Settings ───────────────────────────────────────────────────────────────────

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

# ── Tray icon ──────────────────────────────────────────────────────────────────

def make_icon(paused: bool = False) -> "Image.Image":
    size  = 64
    img   = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw  = ImageDraw.Draw(img)
    color = (120, 120, 120) if paused else (39, 174, 96)   # green, distinct from blue PlainText Monitor
    draw.rounded_rectangle([2, 2, 61, 61], radius=14, fill=color)
    # "C" letterform for Claude
    draw.arc([12, 14, 51, 50], start=45, end=315, fill="white", width=9)
    if paused:
        draw.rectangle([38, 40, 44, 56], fill=(255, 255, 255, 160))
        draw.rectangle([48, 40, 54, 56], fill=(255, 255, 255, 160))
    return img

# ── Settings dialog ────────────────────────────────────────────────────────────

class SettingsDialog(tk.Toplevel):
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

        ttk.Label(outer,
                  text="Squishes multi-line / indented text into one line.",
                  font=("Segoe UI", 9),
                  foreground="gray").pack(anchor="w", pady=(0, 12))

        # ── Startup ─────────────────────────────────────────────────────────
        sf = ttk.LabelFrame(outer, text=" Startup ", padding=(12, 8))
        sf.pack(fill=tk.X, pady=(0, 10))

        self.startup_var = tk.BooleanVar(value=_startup_enabled())
        ttk.Checkbutton(sf, text="Start with Windows",
                        variable=self.startup_var).pack(anchor="w")

        # ── Hotkeys ────────────────────────────────────────────────────────
        hf = ttk.LabelFrame(outer, text=" Hotkeys ", padding=(12, 8))
        hf.pack(fill=tk.X, pady=(0, 14))

        row1 = ttk.Frame(hf)
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="Squish text:").pack(side=tk.LEFT)
        self.hotkey_var = tk.StringVar(value=s.get("hotkey"))
        ttk.Entry(row1, textvariable=self.hotkey_var, width=22).pack(
            side=tk.LEFT, padx=(8, 0))

        row2 = ttk.Frame(hf)
        row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="Squish URL:").pack(side=tk.LEFT)
        self.hotkey_url_var = tk.StringVar(value=s.get("hotkey_url"))
        ttk.Entry(row2, textvariable=self.hotkey_url_var, width=22).pack(
            side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            hf,
            text="Text: joins lines with spaces.  URL: joins with no spaces.\n"
                 "Or click the tray icon to squish text on the clipboard.\n"
                 "Examples:  ctrl+shift+l   ctrl+alt+l   alt+shift+u",
            foreground="gray",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 0))

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
        old_hk     = self.app.settings.get("hotkey")
        old_hk_url = self.app.settings.get("hotkey_url")
        new_hk     = self.hotkey_var.get().strip()
        new_hk_url = self.hotkey_url_var.get().strip()
        self.app.settings.update({"hotkey": new_hk, "hotkey_url": new_hk_url})
        if old_hk != new_hk:
            self.app._hotkey_thread.rebind(_HOTKEY_ID_TEXT, new_hk)
        if old_hk_url != new_hk_url:
            self.app._hotkey_thread.rebind(_HOTKEY_ID_URL, new_hk_url)
        _set_startup(self.startup_var.get())
        self.destroy()

# ── Application ────────────────────────────────────────────────────────────────

class App:
    def __init__(self) -> None:
        self.settings = Settings()
        self._q: queue.Queue    = queue.Queue()
        self._suppress_until    = 0.0
        self._tray: pystray.Icon | None = None
        self._paused            = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        try:
            ttk.Style(self.root).theme_use("vista")
        except tk.TclError:
            pass

        self.root.after(120, self._drain)

        self._hotkey_thread = _HotkeyThread()
        self._hotkey_thread.add(
            _HOTKEY_ID_TEXT,
            lambda: self.root.after(0, self._hotkey_triggered),
            self.settings.get("hotkey"),
        )
        self._hotkey_thread.add(
            _HOTKEY_ID_URL,
            lambda: self.root.after(0, self._hotkey_url_triggered),
            self.settings.get("hotkey_url"),
        )
        self._hotkey_thread.start()
        self._hotkey_thread.wait_ready()

        threading.Thread(
            target=self._run_tray, daemon=True, name="TrayThread"
        ).start()

    def _copy_and_process(self, processor) -> None:
        """Simulate Ctrl+C, wait for clipboard, then run processor on the text."""
        self._suppress_until = time.time() + 1.5
        before = clip_seq()
        _send_ctrl_c()

        deadline = time.time() + 0.4
        while time.time() < deadline:
            if clip_seq() != before:
                break
            time.sleep(0.05)

        text = get_plain_text()
        if text:
            result = processor(text)
            if result:
                set_plain_text(result)

    def _hotkey_triggered(self) -> None:
        self._copy_and_process(squish_text)

    def _hotkey_url_triggered(self) -> None:
        self._copy_and_process(squish_url)

    def _squish_now(self) -> None:
        text = get_plain_text()
        if text:
            squished = squish_text(text)
            if squished:
                self._suppress_until = time.time() + 0.6
                set_plain_text(squished)

    def _squish_url_now(self) -> None:
        text = get_plain_text()
        if text:
            squished = squish_url(text)
            if squished:
                self._suppress_until = time.time() + 0.6
                set_plain_text(squished)

    def _drain(self) -> None:
        try:
            while True:
                kind, _ = self._q.get_nowait()
                if kind == "settings":
                    SettingsDialog(self.root, self)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _make_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                "Squish Clipboard to One Line",
                lambda icon, item: self._squish_now(),
                default=True,
            ),
            pystray.MenuItem(
                "Squish URL (no spaces)",
                lambda icon, item: self._squish_url_now(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Resume" if self._paused else "Pause  (hotkeys only)",
                self._toggle_pause,
            ),
            pystray.MenuItem(
                "Settings\u2026",
                lambda icon, item: self._q.put(("settings", None)),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _run_tray(self) -> None:
        self._tray = pystray.Icon(
            APP_NAME,
            make_icon(self._paused),
            APP_NAME,
            self._make_menu(),
        )
        self._tray.run()

    def _toggle_pause(self, icon=None, item=None) -> None:
        self._paused = not self._paused
        if self._paused:
            self._hotkey_thread.rebind(_HOTKEY_ID_TEXT, "")
            self._hotkey_thread.rebind(_HOTKEY_ID_URL, "")
        else:
            self._hotkey_thread.rebind(_HOTKEY_ID_TEXT, self.settings.get("hotkey"))
            self._hotkey_thread.rebind(_HOTKEY_ID_URL, self.settings.get("hotkey_url"))
        if self._tray:
            self._tray.icon = make_icon(self._paused)
            self._tray.menu = self._make_menu()

    def _quit(self, icon=None, item=None) -> None:
        try:
            self._hotkey_thread.stop()
        except Exception:
            pass
        if self._tray:
            self._tray.stop()
        self.root.after(0, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
