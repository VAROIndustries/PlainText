# PlainText Monitor

A Windows system-tray application that monitors the clipboard for rich text (HTML / RTF) and offers to convert it to plain text automatically.

## Features

- **Clipboard monitoring** — detects when rich text (from web pages, Word, etc.) is copied
- **Prompt to convert** — shows a Yes/No dialog; configurable default answer
- **Auto-convert mode** — silently strip formatting without asking
- **Pause without quitting** — toggle monitoring off/on from the tray menu
- **Hotkey** — press `Ctrl+Shift+P` (configurable) to copy the current selection as plain text in one step
- **Tray menu** — Convert Now, Pause/Resume, Settings, Quit

## Requirements

- Windows 10/11
- Python 3.10+

## Install & Run

```bat
install.bat   :: installs Python dependencies
run.bat       :: starts the app (no console window)
```

Or manually:

```bash
pip install -r requirements.txt
pythonw plaintext_monitor.py
```

## Settings

Right-click the tray icon → **Settings…**

| Setting | Description |
|---|---|
| Pause monitoring | Suspend detection without quitting |
| Auto-convert | Strip formatting silently, no prompt |
| Prompt default | Which button (Yes/No) is pre-selected |
| Hotkey | Key combo to copy selection as plain text |

Settings are saved to `plaintext_settings.json` next to the script.

## How it works

- Polls `GetClipboardSequenceNumber()` every 250 ms
- When the clipboard changes and contains HTML Format or Rich Text Format alongside Unicode text, it triggers
- Converting clears the clipboard and re-sets it with only `CF_UNICODETEXT` (the already-decoded plain text Windows provides)
- The hotkey simulates `Ctrl+C` on the current selection, then immediately strips rich formats

## Auto-start with Windows

1. Press `Win+R`, type `shell:startup`, press Enter
2. Create a shortcut to `run.bat` in that folder
