# PlainText — Clipboard Utilities for Windows

Two lightweight system-tray apps that clean up clipboard text on Windows.

---

## PlainText Monitor (`plaintext_monitor.py`)

Watches the clipboard for rich text (HTML / RTF) and offers to strip the formatting, leaving only plain text. Also supports OCR extraction from clipboard images.

### Features

- **Clipboard monitoring** — detects when rich text (from web pages, Word, etc.) is copied
- **Prompt to convert** — shows a Yes/No dialog; configurable default answer
- **Auto-convert mode** — silently strip formatting without asking
- **OCR extraction** — extract text from clipboard images via Tesseract (optional)
- **Pause without quitting** — toggle monitoring off/on from the tray menu
- **Hotkey** — press `Ctrl+Shift+P` (configurable) to copy the current selection as plain text in one step
- **Excluded apps** — skip the prompt when copying from specific applications
- **Tray icon** — blue "T" icon; grey when paused

### Run

```bat
run.bat
```

### Settings file

`plaintext_settings.json`

| Setting | Description |
|---|---|
| Pause monitoring | Suspend detection without quitting |
| Auto-convert | Strip formatting silently, no prompt |
| Prompt default | Which button (Yes/No) is pre-selected |
| Hotkey | Key combo to copy selection as plain text (`Ctrl+Shift+P`) |
| Excluded apps | exe names to skip (e.g. `excel.exe`) |
| Monitor images | Prompt to OCR when an image is copied |

---

## PlainText for Claude (`plaintext_claude.py`)

A focused utility for cleaning up multi-line, indented text copied from Claude (or any AI assistant). Strips all indentation, drops blank lines, and collapses everything to a single line.

### Features

- **Hotkey** — press `Ctrl+Shift+L` (configurable) to copy the current selection and squish it to one line instantly
- **Tray menu** — "Squish Clipboard to One Line" to process whatever is already on the clipboard
- **Pause / Resume** — disable the hotkey temporarily without quitting
- **Tray icon** — green "C" icon; grey when paused

### Run

```bat
run_claude.bat
```

### Settings file

`plaintext_claude_settings.json`

| Setting | Description |
|---|---|
| Hotkey | Key combo to copy + squish to one line (`Ctrl+Shift+L`) |

---

## Requirements

- Windows 10/11
- Python 3.10+
- `pip install pywin32 pystray Pillow`
- Tesseract OCR *(optional, for PlainText Monitor image OCR only)*

## Install

```bat
install.bat
```

Or manually:

```
pip install -r requirements.txt
```

## Auto-start with Windows

Both apps can run independently at startup:

1. Press `Win+R`, type `shell:startup`, press Enter
2. Create a shortcut to `run.bat` and/or `run_claude.bat` in that folder

## How the squish works

Each line of the clipboard text is stripped of leading/trailing whitespace. Blank lines are discarded. The remaining lines are joined with a single space — turning indented multi-line AI output into a paste-ready single line.

## How the rich-text strip works

- Polls `GetClipboardSequenceNumber()` every 250 ms
- When the clipboard changes and contains HTML Format or RTF alongside Unicode text, it triggers
- Converting clears the clipboard and re-sets it with only `CF_UNICODETEXT`
- The hotkey simulates `Ctrl+C` on the current selection, then immediately strips rich formats

---

## More from VARØ Industries

Free web apps, tools, and open-source projects → [varo.industries/apps](https://varo.industries/apps)
