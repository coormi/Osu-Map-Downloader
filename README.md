# osu! Map Downloader

A lightweight 1-click beatmap downloader for osu! multiplayer lobbies, chat, and links.

Clicking an unowned map in multiplayer downloads and imports it directly into the game in seconds. No browser windows open and you never have to leave the game.

Supports Windows 10/11 and Linux (Wine, Lutris, Bottles, osu! lazer).

---

## How It Works

Windows and Linux route in-game link clicks through the system's default HTTP/HTTPS protocol handler. During setup, this tool registers as the URL handler so it can intercept beatmap links when clicked inside osu!.

### Normal Browsing Is Unaffected
- **Non-osu links** (YouTube, Discord, Google, Reddit, etc.) are immediately forwarded to your real browser.
- **Player profiles** (`osu.ppy.sh/users/...`) and forum links open in your real browser.
- **Shift + Click** on any beatmap forces it to open in your browser instead of auto-downloading.
- The handler auto-detects your active browser (Firefox, Chrome, Brave, Edge, etc.).

### Audio-Only (No Video) Downloads
- Background video files (.mp4/.avi) are stripped from downloads to keep file sizes small (3–6 MB instead of 30–100 MB).
- Maps download and import in **3 to 8 seconds**, keeping you ready before match countdowns finish.
- The downloaded `.osz` contains full-quality audio (.mp3 at 192–320 kbps), all difficulty files, background image, hitsounds, and storyboard.
- If you want the full video version for a specific map, hold **Shift** while clicking it in-game.

---

## Installation

### Windows (10/11)
1. Download `OsuMapHandler.exe` from [Releases](https://github.com/coormi/Osu-Map-Downloader/releases).
2. Run `OsuMapHandler.exe` once to register.
3. Windows Default Apps settings will open:
   - Set **osu! Map Handler** as default for HTTP/HTTPS (or default Web Browser).
4. Done. Click any unowned beatmap in an osu! lobby.

### Linux (Arch Linux / Ubuntu / Fedora / SteamOS)
Requires Python 3.10+ and `requests`.

```bash
git clone https://github.com/coormi/Osu-Map-Downloader.git
cd Osu-Map-Downloader
pip install requests
python3 osu_map_handler.py --setup
```

---

## CLI Options

```bash
# List detected browsers and current configuration
OsuMapHandler.exe --list-browsers

# Set a specific fallback browser
OsuMapHandler.exe --set-browser brave
OsuMapHandler.exe --set-browser firefox
OsuMapHandler.exe --set-browser chrome

# Re-run setup
OsuMapHandler.exe --setup

# Uninstall and remove registry / desktop entries
OsuMapHandler.exe --uninstall
```

---

## Build from Source

```bash
pip install requests pyinstaller
pyinstaller OsuMapHandler.spec --noconfirm
```
Output binary is created at `dist/OsuMapHandler.exe`.

---

## License
MIT License
