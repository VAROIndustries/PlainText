#!/usr/bin/env python3
"""
PlainText Monitor  —  v1.1
A system-tray app that monitors the clipboard for rich text (HTML / RTF) and
offers to strip the formatting, leaving only plain text on the clipboard.
Also detects clipboard images and can extract text via OCR (pytesseract).

Requirements:  pip install pywin32 pystray Pillow keyboard pytesseract
               Tesseract OCR binary: https://github.com/UB-Mannheim/tesseract/wiki
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
    from PIL import Image, ImageDraw, ImageGrab
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

# ── Win32 hotkey helpers (no keyboard hook — uses RegisterHotKey instead) ──────

_WM_HOTKEY    = 0x0312
_HOTKEY_ID    = 1
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
    """Return (modifiers, vk_code) for a string like 'ctrl+shift+p'. (0,0) on failure."""
    mods = _MOD_NOREPEAT
    vk   = 0
    for part in hk_str.lower().split("+"):
        part = part.strip()
        if part in _MOD_MAP:
            mods |= _MOD_MAP[part]
        elif part in _VK_MAP:
            vk = _VK_MAP[part]
    return mods, vk

# SendInput structures — used to inject Ctrl+C without a keyboard hook
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
    """Inject a Ctrl+C keystroke via SendInput — no keyboard hook required."""
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
    """
    Owns RegisterHotKey / UnregisterHotKey in a dedicated thread with its own
    message loop. Uses PostThreadMessageW to accept rebind and stop requests
    from other threads, keeping all Win32 hotkey calls on one thread.
    """
    _WM_QUIT  = 0x0012
    _WM_USER  = 0x0400
    _MSG_BIND = _WM_USER + 1   # signal: re-read self._pending and rebind

    def __init__(self, callback, hk_str: str) -> None:
        super().__init__(daemon=True, name="HotkeyThread")
        self._callback  = callback
        self._pending   = hk_str
        self._current   = ""
        self._tid: int  = 0
        self._ready     = threading.Event()

    # ── public API (call from any thread) ───────────────────────────────

    def wait_ready(self) -> None:
        self._ready.wait()

    def rebind(self, hk_str: str) -> None:
        self._pending = hk_str
        ctypes.windll.user32.PostThreadMessageW(self._tid, self._MSG_BIND, 0, 0)

    def stop(self) -> None:
        ctypes.windll.user32.PostThreadMessageW(self._tid, self._WM_QUIT, 0, 0)

    # ── internal (runs inside this thread) ──────────────────────────────

    def run(self) -> None:
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self._ready.set()
        self._do_bind(self._pending)

        msg = _MSG()
        user32 = ctypes.windll.user32
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0 or result == -1:
                break
            if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                self._callback()
            elif msg.message == self._MSG_BIND:
                self._do_bind(self._pending)

    def _do_bind(self, hk_str: str) -> None:
        user32 = ctypes.windll.user32
        if self._current:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._current = ""
        if hk_str:
            mods, vk = _parse_hotkey(hk_str)
            if vk and user32.RegisterHotKey(None, _HOTKEY_ID, mods, vk):
                self._current = hk_str
            elif vk:
                print(f"[PlainText] Cannot register hotkey '{hk_str}'")

# pytesseract is optional — OCR features are disabled when absent
try:
    import pytesseract as _pytesseract
    # Point to the default Tesseract install location on Windows if not on PATH
    _TESS_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_TESS_DEFAULT):
        _pytesseract.pytesseract.tesseract_cmd = _TESS_DEFAULT
    _HAS_OCR = True
except ImportError:
    _HAS_OCR = False

# ══════════════════════════════════════════════════════════════════════════════
# Constants & defaults
# ══════════════════════════════════════════════════════════════════════════════

APP_NAME      = "PlainText Monitor"
BASE_DIR      = os.path.dirname(os.path.abspath(sys.argv[0]))
SETTINGS_FILE = os.path.join(BASE_DIR, "plaintext_settings.json")
POLL_INTERVAL = 0.25   # seconds between clipboard polls

DEFAULTS: dict = {
    "default_yes":    True,           # True → Yes is the highlighted/default button
    "auto_convert":   False,          # skip the prompt, convert silently
    "paused":         False,          # monitoring paused
    "hotkey":         "ctrl+shift+p", # hotkey to copy selection as plain text
    "excluded_apps":  [],             # exe names to skip (e.g. ["excel.exe"])
    "monitor_images": True,           # show OCR prompt when an image is copied
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


def clipboard_has_image() -> bool:
    """Return True if clipboard holds a bitmap / DIB image."""
    if not _open():
        return False
    try:
        return (
            win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB)
            or win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIBV5)
            or win32clipboard.IsClipboardFormatAvailable(win32con.CF_BITMAP)
        )
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


def get_clipboard_image() -> "Image.Image | None":
    """Return a PIL Image grabbed from the clipboard, or None."""
    try:
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            return img
    except Exception as exc:
        print(f"[PlainText] get_clipboard_image error: {exc}")
    return None


def ocr_image(img: "Image.Image") -> str | None:
    """Run Tesseract OCR on a PIL Image.  Returns stripped text or None."""
    if not _HAS_OCR:
        return None
    try:
        result = _pytesseract.image_to_string(img).strip()
        return result or None
    except Exception as exc:
        print(f"[PlainText] OCR error: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Foreground-app detection
# ══════════════════════════════════════════════════════════════════════════════

def get_foreground_app() -> str:
    """Return the lowercase exe filename of the foreground window's process.

    Uses only ctypes (no extra deps).  Returns '' on any failure.
    """
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid  = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not handle:
            return ""
        buf  = ctypes.create_unicode_buffer(512)
        size = ctypes.c_ulong(512)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        )
        ctypes.windll.kernel32.CloseHandle(handle)
        return os.path.basename(buf.value).lower()
    except Exception:
        return ""

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
    """Ask whether to convert rich clipboard content to plain text.

    result values:
      True   — convert to plain text
      "ocr"  — extract text from the clipboard image instead
      False  — do nothing
    """

    def __init__(self, parent: tk.Tk, preview: str, default_yes: bool,
                 has_image: bool = False) -> None:
        super().__init__(parent)
        self.result: bool | str | None = None
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
        yes_btn = ttk.Button(bf, text="Yes",  width=11, command=self._yes)
        no_btn  = ttk.Button(bf, text="No",   width=11, command=self._no)
        yes_btn.grid(row=0, column=0, padx=6)
        no_btn.grid( row=0, column=1, padx=6)

        if has_image:
            ocr_btn = ttk.Button(bf, text="Extract from image",
                                 width=18, command=self._ocr)
            ocr_btn.grid(row=0, column=2, padx=6)

        if default_yes:
            yes_btn.focus_set()
            self.bind("<Return>", lambda _: self._yes())
        else:
            no_btn.focus_set()
            self.bind("<Return>", lambda _: self._no())

        self.bind("<Escape>", lambda _: self._no())
        self.bind("y", lambda _: self._yes())
        self.bind("n", lambda _: self._no())
        self._center()
        self.grab_set()
        self.lift()
        self.focus_force()
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

    def _ocr(self) -> None:
        self.result = "ocr"
        self.destroy()


class ImageOCRDialog(tk.Toplevel):
    """Ask whether to extract text from the clipboard image via OCR."""

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent)
        self.result: bool = False
        self.title(APP_NAME)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._no)

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer,
                  text="Image detected on clipboard.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(outer,
                  text="Would you like to extract text from it?",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=(4, 12))

        if not _HAS_OCR:
            ttk.Label(
                outer,
                text="pytesseract is not installed.\n"
                     "Run:  pip install pytesseract\n"
                     "and install Tesseract OCR to enable this feature.",
                foreground="red",
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(0, 10))

        bf = ttk.Frame(outer)
        bf.pack()
        yes_btn = ttk.Button(bf, text="Extract text", width=13, command=self._yes,
                             state=tk.NORMAL if _HAS_OCR else tk.DISABLED)
        no_btn  = ttk.Button(bf, text="Cancel",       width=11, command=self._no)
        yes_btn.grid(row=0, column=0, padx=6)
        no_btn.grid( row=0, column=1, padx=6)

        if _HAS_OCR:
            yes_btn.focus_set()
            self.bind("<Return>", lambda _: self._yes())
        else:
            no_btn.focus_set()
            self.bind("<Return>", lambda _: self._no())

        self.bind("<Escape>", lambda _: self._no())
        self.bind("y", lambda _: self._yes())
        self.bind("n", lambda _: self._no())
        self._center()
        self.grab_set()
        self.lift()
        self.focus_force()
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

        self.images_var = tk.BooleanVar(value=s.get("monitor_images"))
        ttk.Checkbutton(mf,
                        text="Prompt to extract text when an image is copied  (OCR)",
                        variable=self.images_var).pack(anchor="w", pady=(6, 0))

        if not _HAS_OCR:
            ttk.Label(
                mf,
                text="  pytesseract not installed — OCR unavailable",
                foreground="gray",
            ).pack(anchor="w")

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

        # ── Excluded Apps ────────────────────────────────────────────────────
        ef = ttk.LabelFrame(outer, text=" Excluded Apps ", padding=(12, 8))
        ef.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(
            ef,
            text="Skip the conversion prompt when copying from these apps:",
            foreground="gray",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        list_frame = ttk.Frame(ef)
        list_frame.pack(fill=tk.X)
        self.excluded_list = tk.Listbox(
            list_frame, height=4, selectmode=tk.SINGLE,
            activestyle="dotbox", relief="solid", bd=1,
        )
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                           command=self.excluded_list.yview)
        self.excluded_list.configure(yscrollcommand=sb.set)
        self.excluded_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)

        for app in (s.get("excluded_apps") or []):
            self.excluded_list.insert(tk.END, app)

        add_row = ttk.Frame(ef)
        add_row.pack(fill=tk.X, pady=(6, 0))
        self.new_app_var = tk.StringVar()
        ttk.Entry(add_row, textvariable=self.new_app_var, width=24).pack(
            side=tk.LEFT)
        ttk.Button(add_row, text="Add",    width=7,
                   command=self._add_excluded).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(add_row, text="Remove", width=7,
                   command=self._remove_excluded).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(
            ef, text="Examples:  excel.exe   notepad++.exe   code.exe",
            foreground="gray",
        ).pack(anchor="w", pady=(4, 0))

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

    def _add_excluded(self) -> None:
        val = self.new_app_var.get().strip().lower()
        if not val:
            return
        if val not in self.excluded_list.get(0, tk.END):
            self.excluded_list.insert(tk.END, val)
        self.new_app_var.set("")

    def _remove_excluded(self) -> None:
        sel = self.excluded_list.curselection()
        if sel:
            self.excluded_list.delete(sel[0])

    def _save(self) -> None:
        old_hk = self.app.settings.get("hotkey")
        new_hk = self.hotkey_var.get().strip()
        self.app.settings.update({
            "paused":         self.paused_var.get(),
            "auto_convert":   self.auto_var.get(),
            "monitor_images": self.images_var.get(),
            "default_yes":    self.default_var.get() == "yes",
            "hotkey":         new_hk,
            "excluded_apps":  list(self.excluded_list.get(0, tk.END)),
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

        # ── Hotkey (RegisterHotKey — no system-wide hook) ────────────────────
        self._hotkey_thread = _HotkeyThread(
            lambda: self.root.after(0, self._hotkey_triggered),
            self.settings.get("hotkey"),
        )
        self._hotkey_thread.start()
        self._hotkey_thread.wait_ready()

        # ── Background threads ──────────────────────────────────────────────
        threading.Thread(
            target=self._monitor, daemon=True, name="ClipboardMonitor"
        ).start()
        threading.Thread(
            target=self._run_tray, daemon=True, name="TrayThread"
        ).start()

    # ── Hotkey ──────────────────────────────────────────────────────────────

    def rebind_hotkey(self, old: str, new: str) -> None:
        self._hotkey_thread.rebind(new)

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
        _send_ctrl_c()

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

                # Skip if the app that triggered the copy is excluded
                excluded = self.settings.get("excluded_apps") or []
                if excluded:
                    app_exe = get_foreground_app()
                    if app_exe in [e.lower() for e in excluded]:
                        continue

                is_rich  = clipboard_is_rich()
                is_image = clipboard_has_image()

                if not is_rich and not is_image:
                    continue

                if is_rich:
                    text = get_plain_text() or ""
                    if not text.strip():
                        # No usable text — fall through to image path if available
                        if is_image and self.settings.get("monitor_images"):
                            self._q.put(("image_prompt", None))
                        continue

                    if self.settings.get("auto_convert"):
                        self._suppress_until = time.time() + 0.6
                        set_plain_text(text)
                    else:
                        self._q.put(("prompt", {
                            "text":      text,
                            "has_image": is_image,
                        }))

                elif is_image and self.settings.get("monitor_images"):
                    self._q.put(("image_prompt", None))

            except Exception as exc:
                print(f"[PlainText] Monitor error: {exc}")

    # ── Queue drain — runs on main / tkinter thread ──────────────────────────

    def _drain(self) -> None:
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == "prompt" and not self._dialog_open:
                    self._show_prompt(data)
                elif kind == "image_prompt" and not self._dialog_open:
                    self._show_image_prompt()
                elif kind == "settings":
                    self._open_settings_window()
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _show_prompt(self, data: dict) -> None:
        text      = data["text"]
        has_image = data.get("has_image", False)
        self._dialog_open = True
        try:
            dlg = ConvertDialog(
                self.root, text, self.settings.get("default_yes"), has_image
            )
            if dlg.result is True:
                self._suppress_until = time.time() + 0.6
                set_plain_text(text)
            elif dlg.result == "ocr":
                self._do_ocr()
            else:
                # User said No — suppress re-prompts for this clipboard content
                self._suppress_until = time.time() + 2.0
                self._flush_prompts()
        finally:
            self._dialog_open = False

    def _show_image_prompt(self) -> None:
        self._dialog_open = True
        try:
            dlg = ImageOCRDialog(self.root)
            if dlg.result:
                self._do_ocr()
            else:
                self._suppress_until = time.time() + 2.0
                self._flush_prompts()
        finally:
            self._dialog_open = False

    def _do_ocr(self) -> None:
        """Grab the clipboard image, run OCR, and put the result as plain text."""
        img = get_clipboard_image()
        if img is None:
            import tkinter.messagebox as mb
            mb.showwarning(APP_NAME, "No image found on the clipboard.")
            return

        text = ocr_image(img)
        if text:
            self._suppress_until = time.time() + 0.6
            set_plain_text(text)
        else:
            import tkinter.messagebox as mb
            if not _HAS_OCR:
                mb.showerror(
                    APP_NAME,
                    "pytesseract is not installed.\n\n"
                    "Run:  pip install pytesseract\n"
                    "Then install the Tesseract OCR binary from:\n"
                    "https://github.com/UB-Mannheim/tesseract/wiki",
                )
            else:
                mb.showwarning(APP_NAME, "No text could be extracted from the image.")

    def _flush_prompts(self) -> None:
        """Discard all pending prompt / image_prompt items from the queue."""
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind not in ("prompt", "image_prompt"):
                    self._q.put((kind, data))
                    break
        except queue.Empty:
            pass

    def _open_settings_window(self) -> None:
        SettingsDialog(self.root, self)

    # ── Convert now (used by tray menu "Convert Clipboard Now") ─────────────

    def _convert_now(self) -> None:
        if clipboard_is_rich():
            text = get_plain_text()
            if text:
                self._suppress_until = time.time() + 0.6
                set_plain_text(text)

    def _ocr_now(self) -> None:
        """Tray menu: extract text from clipboard image right now."""
        self._q.put(("image_prompt", None))

    # ── System tray ─────────────────────────────────────────────────────────

    def _make_menu(self) -> pystray.Menu:
        paused = self.settings.get("paused")
        return pystray.Menu(
            pystray.MenuItem(
                "Convert Clipboard Now",
                lambda icon, item: self._convert_now(),
            ),
            pystray.MenuItem(
                "Extract Text from Image",
                lambda icon, item: self._ocr_now(),
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
            self._hotkey_thread.stop()
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
