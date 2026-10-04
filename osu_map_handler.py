"""
osu_map_handler.py — Single-file setup + URL handler for osu! beatmaps.

Supports:
- Windows (10/11)
- Linux (Arch Linux, Ubuntu, Fedora, SteamOS, etc.) with Wine, Lutris, Bottles, or osu! lazer.

Features:
- Parallel Mirror Racing: Pings top mirrors concurrently (Catboy, Beatconnect, Sayobot)
  and streams from the fastest responding server. Eliminates mirror stall timeouts!
- 1-Click Beatmap Download: Clicking a beatmap in multiplayer lobby, chat,
  or solo listing automatically downloads and imports the map into osu!
  without leaving the game.
- Shift + Click: Hold Shift while clicking any beatmap link to force it to open
  in your web browser instead of auto-downloading.
- Profile & External Links: Profile links (osu.ppy.sh/users/...) and external links
  automatically open in your preferred web browser.
- Universal Browser Support: Works seamlessly with any web browser (Firefox, Chrome,
  Brave, Edge, Opera, Opera GX, Vivaldi, Zen, Floorp, Waterfox, Chromium, etc.).
- Works out of the box for any user on Windows and Linux.

Usage:
  Double-click (or run with no args) -> Run one-time setup & registration
  python osu_map_handler.py --set-browser <name|path> -> Configure fallback browser
  python osu_map_handler.py --list-browsers -> List all detected browsers
  python osu_map_handler.py <url> -> Handle incoming URL from game / OS

Build Windows executable:
  pyinstaller OsuMapHandler.spec --noconfirm
"""

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
import socket
import urllib.parse
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    import winreg

APP_NAME = "OsuMapHandler"
FRIENDLY_NAME = "osu! Map Handler"
DESCRIPTION = "Auto-downloads osu! beatmaps from multiplayer / links"

DOWNLOAD_DIR = Path(os.environ.get("TEMP" if IS_WINDOWS else "XDG_CACHE_HOME", "/tmp" if IS_LINUX else ".")) / "osu_auto_import"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = Path(os.environ.get("TEMP" if IS_WINDOWS else "XDG_CACHE_HOME", "/tmp" if IS_LINUX else ".")) / "osu_map_handler_log.txt"

LINUX_CONFIG_DIR = Path.home() / ".config" / "osu_map_handler"
LINUX_CONFIG_FILE = LINUX_CONFIG_DIR / "config.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# High-throughput HTTP adapter tuning TCP_NODELAY and socket receive buffers
class FastHTTPAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        opts = []
        if hasattr(socket, "IPPROTO_TCP") and hasattr(socket, "TCP_NODELAY"):
            opts.append((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1))
        if hasattr(socket, "SOL_SOCKET") and hasattr(socket, "SO_RCVBUF"):
            opts.append((socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576))
        if opts:
            kwargs["socket_options"] = opts
        super().init_poolmanager(*args, **kwargs)

BEATMAP_RE = re.compile(
    r"^https?://(?:osu\.ppy\.sh|old\.ppy\.sh)/(?:beatmapsets|beatmaps|b|s)/.+$",
    re.I,
)

VK_SHIFT = 0x10


def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def msgbox(text: str, title: str = FRIENDLY_NAME, icon: int = 0x40) -> None:
    if IS_WINDOWS:
        ctypes.windll.user32.MessageBoxW(0, text, title, icon | 0x40000)
    elif IS_LINUX:
        if shutil.which("zenity"):
            subprocess.run(["zenity", "--info", "--title", title, "--text", text], check=False)
        elif shutil.which("kdialog"):
            subprocess.run(["kdialog", "--title", title, "--msgbox", text], check=False)
        else:
            print(f"\n[{title}]\n{text}\n")


def is_shift_held() -> bool:
    """Check if the Shift key is physically down at invocation time."""
    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            return bool(
                (user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                or (user32.GetKeyState(VK_SHIFT) & 0x8000)
            )
        except Exception as e:
            log(f"is_shift_held failed: {e}")
            return False
    return False


def get_foreground_process_info() -> tuple[str | None, str | None]:
    """Returns (process_name, window_title) of the foreground window."""
    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return (None, None)

            title_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title_buf, 512)
            window_title = title_buf.value

            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return (None, window_title)

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not hproc:
                return (None, window_title)

            try:
                buf = ctypes.create_unicode_buffer(260)
                size = ctypes.c_ulong(len(buf))
                ok = kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size))
                if ok:
                    return (Path(buf.value).name.lower(), window_title)
                return (None, window_title)
            finally:
                kernel32.CloseHandle(hproc)
        except Exception as e:
            log(f"get_foreground_process_info failed: {e}")
            return (None, None)

    elif IS_LINUX:
        if shutil.which("xdotool"):
            try:
                win_id = subprocess.check_output(["xdotool", "getactivewindow"], timeout=0.4).decode().strip()
                title = subprocess.check_output(["xdotool", "getwindowname", win_id], timeout=0.4).decode().strip()
                pid = subprocess.check_output(["xdotool", "getwindowpid", win_id], timeout=0.4).decode().strip()
                comm_path = Path(f"/proc/{pid}/comm")
                proc_name = comm_path.read_text(encoding="utf-8").strip().lower() if comm_path.exists() else None
                return (proc_name, title)
            except Exception:
                pass
        return (None, None)

    return (None, None)


def is_osu_foreground(proc_name: str | None, window_title: str | None) -> bool:
    """Check whether the originating window was osu!."""
    if proc_name and proc_name in ("osu!.exe", "osu.exe", "osu", "wine-preloader", "wine64-preloader"):
        return True
    if window_title and "osu!" in window_title.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# Songs Folder Detection
# ---------------------------------------------------------------------------

def check_cfg_songs(osu_dir: Path) -> Path | None:
    if not osu_dir.is_dir():
        return None
    for cfg in osu_dir.glob("osu!.*.cfg"):
        try:
            with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.strip().startswith("BeatmapDirectory"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            val = parts[1].strip()
                            custom = Path(val)
                            if custom.is_absolute() and custom.is_dir():
                                return custom
                            rel = osu_dir / val
                            if rel.is_dir():
                                return rel
        except Exception:
            pass
    songs_default = osu_dir / "Songs"
    if songs_default.is_dir():
        return songs_default
    return None


def find_osu_songs_path() -> Path:
    """Dynamically locates the osu! Songs directory."""
    if IS_WINDOWS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_CLASSES_ROOT):
            for sub in (
                r"Software\Classes\osu\DefaultIcon",
                r"osu\DefaultIcon",
                r"Software\Classes\osustable.File.osz\shell\open\command",
            ):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        val, _ = winreg.QueryValueEx(k, None)
                        if val:
                            p = val.split(",")[0].strip('"')
                            p_dir = Path(p).parent
                            if (p_dir / "osu!.exe").is_file():
                                songs = check_cfg_songs(p_dir)
                                if songs:
                                    return songs
                except Exception:
                    pass

        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            p_dir = Path(local_app_data) / "osu!"
            songs = check_cfg_songs(p_dir)
            if songs:
                return songs
            if (p_dir / "Songs").is_dir():
                return p_dir / "Songs"

        return Path(local_app_data) / "osu!" / "Songs"

    elif IS_LINUX:
        home = Path.home()
        candidates = [
            home / ".local" / "share" / "osu" / "Songs",
            home / ".osu" / "Songs",
            home / ".wine" / "drive_c" / "osu!" / "Songs",
            home / ".local" / "share" / "osu-wine" / "drive_c" / "osu!" / "Songs",
            home / ".local" / "share" / "lutris" / "runners" / "wine" / "pfx" / "drive_c" / "osu!" / "Songs",
            home / "osu-wine" / "drive_c" / "osu!" / "Songs",
            home / ".var" / "app" / "sh.ppy.osu" / "data" / "osu" / "Songs",
        ]
        for c in candidates:
            if c.is_dir():
                return c
        return candidates[0]

    return Path.home() / "osu!" / "Songs"


# ---------------------------------------------------------------------------
# Browser Detection & Management
# ---------------------------------------------------------------------------

COMMON_BROWSER_PATHS_WINDOWS = [
    Path(os.environ.get("ProgramFiles", "")) / "Mozilla Firefox" / "firefox.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Mozilla Firefox" / "firefox.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Zen Browser" / "zen.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Zen Browser" / "zen.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Floorp" / "floorp.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Waterfox" / "waterfox.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("ProgramFiles", "")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Opera GX" / "launcher.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Opera" / "launcher.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Opera GX" / "launcher.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Opera" / "launcher.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Vivaldi" / "Application" / "vivaldi.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
]

LINUX_BROWSER_BINARIES = [
    "firefox",
    "zen-browser",
    "google-chrome-stable",
    "google-chrome",
    "brave-bin",
    "brave",
    "chromium",
    "opera",
    "vivaldi-stable",
    "vivaldi",
    "floorp",
    "waterfox",
    "microsoft-edge-stable",
]


def extract_exe_from_command(cmd: str) -> str | None:
    if not cmd:
        return None
    cmd = cmd.strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end != -1:
            return cmd[1:end].strip()
    parts = cmd.split()
    if parts:
        return parts[0].strip('"')
    return None


def get_installed_browsers() -> list[str]:
    found = []
    if IS_WINDOWS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Clients\StartMenuInternet") as k:
                    i = 0
                    while True:
                        sub = winreg.EnumKey(k, i)
                        i += 1
                        if "iexplore" in sub.lower() or APP_NAME.lower() in sub.lower():
                            continue
                        try:
                            with winreg.OpenKey(k, rf"{sub}\shell\open\command") as ck:
                                cmd, _ = winreg.QueryValueEx(ck, None)
                                exe = extract_exe_from_command(cmd)
                                if exe and Path(exe).is_file() and exe not in found:
                                    found.append(exe)
                        except Exception:
                            pass
            except OSError:
                pass

        for p in COMMON_BROWSER_PATHS_WINDOWS:
            s = str(p)
            if p.is_file() and s not in found:
                found.append(s)

    elif IS_LINUX:
        for b in LINUX_BROWSER_BINARIES:
            bin_path = shutil.which(b)
            if bin_path and bin_path not in found:
                found.append(bin_path)

    return found


def query_windows_default_browser_cmd() -> str | None:
    if not IS_WINDOWS:
        return None
    try:
        AssocQueryStringW = ctypes.windll.shlwapi.AssocQueryStringW
        AssocQueryStringW.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        AssocQueryStringW.restype = ctypes.c_long
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        hr = AssocQueryStringW(0, 1, "https", "open", buf, ctypes.byref(size))
        if hr == 0 and buf.value:
            cmd = buf.value
            exe = extract_exe_from_command(cmd)
            if exe and APP_NAME.lower() not in Path(exe).name.lower():
                return cmd
    except Exception:
        pass
    return None


def get_saved_browser_cmd() -> str | None:
    if IS_WINDOWS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, rf"SOFTWARE\{APP_NAME}\Config") as k:
                    val, _ = winreg.QueryValueEx(k, "FallbackBrowserCmd")
                    if val:
                        exe = extract_exe_from_command(val)
                        if exe and Path(exe).is_file():
                            return val
            except Exception:
                pass
    elif IS_LINUX:
        if LINUX_CONFIG_FILE.is_file():
            try:
                data = json.loads(LINUX_CONFIG_FILE.read_text(encoding="utf-8"))
                cmd = data.get("fallback_browser")
                if cmd and (Path(cmd).is_file() or shutil.which(cmd)):
                    return cmd
            except Exception:
                pass
    return None


def save_browser_cmd(cmd_or_path: str) -> None:
    cmd = cmd_or_path.strip()
    if IS_WINDOWS:
        if not cmd.startswith('"') and Path(cmd).is_file():
            cmd = f'"{cmd}" "%1"'
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.CreateKey(root, rf"SOFTWARE\{APP_NAME}\Config") as k:
                    winreg.SetValueEx(k, "FallbackBrowserCmd", 0, winreg.REG_SZ, cmd)
            except PermissionError:
                pass
            except Exception as e:
                log(f"save_browser_cmd failed for {root}: {e}")

    elif IS_LINUX:
        try:
            LINUX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            LINUX_CONFIG_FILE.write_text(json.dumps({"fallback_browser": cmd}, indent=2), encoding="utf-8")
        except Exception as e:
            log(f"save_browser_cmd failed on Linux: {e}")


def resolve_fallback_browser() -> tuple[str, str | None]:
    saved = get_saved_browser_cmd()
    if saved:
        exe = extract_exe_from_command(saved)
        if exe and (Path(exe).is_file() or (IS_LINUX and shutil.which(exe))):
            return (exe, saved)

    installed = get_installed_browsers()

    if IS_WINDOWS:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        exe_map = {Path(p).name.lower(): p for p in installed}
        running_found = [None]

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _check_running(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            wpid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if not wpid.value:
                return True
            hproc = kernel32.OpenProcess(0x1000, False, wpid.value)
            if not hproc:
                return True
            try:
                buf = ctypes.create_unicode_buffer(260)
                sz = ctypes.c_ulong(len(buf))
                if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                    name = Path(buf.value).name.lower()
                    if name in exe_map:
                        running_found[0] = exe_map[name]
                        return False
            finally:
                kernel32.CloseHandle(hproc)
            return True

        try:
            user32.EnumWindows(_check_running, 0)
        except Exception:
            pass

        if running_found[0]:
            return (running_found[0], None)

        non_edge = [p for p in installed if "msedge.exe" not in p.lower()]
        if non_edge:
            return (non_edge[0], None)
        if installed:
            return (installed[0], None)

        edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        return (edge, None)

    elif IS_LINUX:
        env_browser = os.environ.get("BROWSER")
        if env_browser and shutil.which(env_browser):
            return (env_browser, None)
        if installed:
            return (installed[0], None)
        return ("xdg-open", None)

    return ("firefox", None)


def force_window_foreground_by_exe(exe_path: str | None, timeout: float = 2.5) -> None:
    if not IS_WINDOWS or not exe_path:
        return

    target_name = Path(exe_path).name.lower()
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    found_hwnd = [None]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum_proc(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        wpid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if not wpid.value:
            return True

        hproc = kernel32.OpenProcess(0x1000, False, wpid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_ulong(len(buf))
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                if Path(buf.value).name.lower() == target_name:
                    found_hwnd[0] = hwnd
                    return False
        finally:
            kernel32.CloseHandle(hproc)
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        found_hwnd[0] = None
        user32.EnumWindows(_enum_proc, 0)
        if found_hwnd[0]:
            break
        time.sleep(0.08)

    hwnd = found_hwnd[0]
    if not hwnd:
        return

    try:
        fg_hwnd = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        cur_thread = kernel32.GetCurrentThreadId()

        user32.AttachThreadInput(cur_thread, fg_thread, True)
        user32.AttachThreadInput(target_thread, fg_thread, True)

        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)

        user32.AttachThreadInput(cur_thread, fg_thread, False)
        user32.AttachThreadInput(target_thread, fg_thread, False)
    except Exception as e:
        log(f"force_window_foreground_by_exe failed: {e}")


def open_in_real_browser(url: str, bring_to_front: bool = False) -> None:
    exe, cmd = resolve_fallback_browser()
    env = os.environ.copy()
    env["MOZ_DISABLE_SAFE_MODE_KEY"] = "1"

    try:
        if IS_WINDOWS:
            if cmd and "%1" in cmd:
                if '"%1"' in cmd:
                    full = cmd.replace('"%1"', f'"{url}"')
                else:
                    full = cmd.replace('%1', f'"{url}"')
                subprocess.Popen(full, shell=True, env=env)
            elif exe and Path(exe).is_file():
                subprocess.Popen([exe, url], env=env)
            else:
                os.startfile(url)

            if bring_to_front and exe:
                force_window_foreground_by_exe(exe)

        elif IS_LINUX:
            if exe == "xdg-open":
                subprocess.Popen(["xdg-open", url], env=env)
            elif exe:
                subprocess.Popen([exe, url], env=env)
            else:
                subprocess.Popen(["xdg-open", url], env=env)

    except Exception as e:
        log(f"open_in_real_browser failed for {url} with {exe}: {e}")
        try:
            if IS_WINDOWS:
                os.startfile(url)
            else:
                subprocess.Popen(["xdg-open", url])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Beatmap ID Resolution & Parallel Racing Download
# ---------------------------------------------------------------------------

def extract_set_id(url: str) -> str | None:
    m = re.search(r"beatmapsets/(\d+)", url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"/s/(\d+)", url, re.I)
    if m:
        return m.group(1)
    return None


def resolve_set_id(url: str) -> str | None:
    direct = extract_set_id(url)
    if direct:
        return direct

    bid_match = re.search(r"/(?:b|beatmaps)/(\d+)", url, re.I)
    bid = bid_match.group(1) if bid_match else None

    # Method 1: osu.ppy.sh HEAD request
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.head(url, allow_redirects=True, timeout=(3, 5), headers=headers)
        m = re.search(r"beatmapsets/(\d+)", r.url, re.I)
        if m:
            return m.group(1)
        loc = r.headers.get("Location", "")
        m = re.search(r"beatmapsets/(\d+)", loc, re.I)
        if m:
            return m.group(1)
    except Exception as e:
        log(f"osu.ppy.sh redirect check failed: {e}")

    if not bid:
        return None

    # Method 2: Catboy API
    try:
        r = requests.get(f"https://catboy.best/api/v2/b/{bid}", headers=headers, timeout=3)
        if r.status_code == 200:
            data = r.json()
            sid = data.get("beatmapset_id")
            if sid:
                return str(sid)
    except Exception as e:
        log(f"catboy API resolve failed: {e}")

    # Method 3: Sayobot API
    try:
        r = requests.get(f"https://api.sayobot.cn/v2/beatmapinfo?K={bid}&T=1", headers=headers, timeout=3)
        if r.status_code == 200:
            sid = r.json().get("data", {}).get("sid")
            if sid:
                return str(sid)
    except Exception as e:
        log(f"sayobot API resolve failed: {e}")

    return None


def is_installed(set_id: str, songs_path: Path) -> bool:
    if not songs_path.is_dir():
        return False
    prefix = f"{set_id} "
    try:
        for p in songs_path.iterdir():
            if p.is_dir() and (p.name.startswith(prefix) or p.name == str(set_id)):
                return True
    except Exception:
        pass
    return False


def download_osz(set_id: str) -> Path | None:
    """
    Downloads beatmapset .osz using Optimized Tiered & Hedged Mirror Racing.
    1. Tier 1: High-Speed Cloud CDN (storage.osu.direct / Cloudflare / AWS S3)
       - Primary: osu.direct (no-video) -> Fast, high quality audio, stripped video, ~3-5 seconds.
       - Fallback 1a: osu.direct (full) -> Full mapset if no-video variant is missing.
    2. Tier 2: Secondary Community Mirrors (concurrent race pool)
       - Catboy (no-video): https://catboy.best/d/{set_id}?noVideo=1
       - Sayobot (no-video): https://dl.sayobot.cn/beatmaps/download/novideo/{set_id}
    3. Tier 3: Last Resort Fallback (concurrent race pool)
       - Catboy (full): https://catboy.best/d/{set_id}
       - Sayobot (full): https://dl.sayobot.cn/beatmaps/download/full/{set_id}
    Uses 512KB stream buffers, TCP_NODELAY, and 1MB socket receive buffers.
    """
    session = requests.Session()
    adapter = FastHTTPAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})

    tier1_mirrors = [
        ("osu.direct (no-video)", f"https://osu.direct/d/{set_id}?noVideo=1"),
        ("osu.direct", f"https://osu.direct/d/{set_id}"),
    ]

    tier2_mirrors = [
        ("Catboy (no-video)", f"https://catboy.best/d/{set_id}?noVideo=1"),
        ("Sayobot (no-video)", f"https://dl.sayobot.cn/beatmaps/download/novideo/{set_id}"),
    ]

    tier3_mirrors = [
        ("Catboy (full)", f"https://catboy.best/d/{set_id}"),
        ("Sayobot (full)", f"https://dl.sayobot.cn/beatmaps/download/full/{set_id}"),
    ]

    winner = None

    # Step 1: Probe Tier 1 high-speed Cloud CDN
    for name, url in tier1_mirrors:
        try:
            r = session.get(url, stream=True, timeout=(2.5, 20))
            if r.status_code == 200 and "html" not in r.headers.get("content-type", "").lower():
                winner = (name, r)
                break
            r.close()
        except Exception:
            pass

    def probe(m):
        name, url = m
        try:
            r = session.get(url, stream=True, timeout=(3, 20))
            if r.status_code == 200 and "html" not in r.headers.get("content-type", "").lower():
                return (name, r)
            r.close()
        except Exception:
            pass
        return None

    # Step 2: If Tier 1 failed or 404'd, race Tier 2 concurrently
    if not winner:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tier2_mirrors)) as ex:
            futures = {ex.submit(probe, m): m for m in tier2_mirrors}
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                if res:
                    winner = res
                    for f in futures:
                        f.cancel()
                    break

    # Step 3: Last resort Tier 3 full video fallback
    if not winner:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tier3_mirrors)) as ex:
            futures = {ex.submit(probe, m): m for m in tier3_mirrors}
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                if res:
                    winner = res
                    for f in futures:
                        f.cancel()
                    break

    if not winner:
        log(f"All mirrors failed for set {set_id}")
        return None

    name, r = winner
    log(f"Download stream established with {name} for set {set_id}")

    try:
        fname = f"{set_id}.osz"
        cd = r.headers.get("content-disposition", "")
        m = re.search(r'filename[*]?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
        if m:
            fname = m.group(1).strip()
        fname = urllib.parse.unquote(fname)
        fname = re.sub(r'[\\/*?:"<>|]', "", fname)
        if not fname.endswith(".osz"):
            fname += ".osz"

        out = DOWNLOAD_DIR / fname
        part_file = DOWNLOAD_DIR / f"{fname}.part"

        downloaded = 0
        t0 = time.time()
        # 512KB chunk buffer for maximum throughput over high-latency TCP
        with open(part_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=524288):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

        if downloaded < 1024:
            part_file.unlink(missing_ok=True)
            return None

        # Atomic rename
        if out.exists():
            out.unlink()
        part_file.rename(out)

        elapsed = time.time() - t0
        speed_mb = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
        log(f"Successfully downloaded {fname} ({downloaded} bytes in {elapsed:.2f}s @ {speed_mb:.2f} MB/s) from {name}")
        return out
    except Exception as e:
        log(f"Error streaming from {name}: {e}")
        return None


def import_beatmap_into_osu(osz_path: Path, songs_path: Path) -> bool:
    """Imports downloaded .osz into osu! cleanly on Windows or Linux."""
    try:
        if IS_WINDOWS:
            log(f"Importing {osz_path} into osu! via os.startfile")
            os.startfile(str(osz_path))
            return True
        elif IS_LINUX:
            if shutil.which("osu-lazer"):
                subprocess.Popen(["osu-lazer", str(osz_path)])
                return True
            if shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", str(osz_path)])
                return True
            if songs_path.is_dir():
                target = songs_path / osz_path.name
                shutil.copy2(osz_path, target)
                log(f"Copied {osz_path.name} directly to {songs_path}")
                return True
    except Exception as e:
        log(f"import_beatmap_into_osu failed: {e}")
    return False


# ---------------------------------------------------------------------------
# Setup & Registration (Windows & Linux)
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    elif IS_LINUX:
        return os.geteuid() == 0
    return False


def relaunch_as_admin() -> None:
    if IS_WINDOWS:
        exe = sys.executable
        if getattr(sys, "frozen", False):
            params = " ".join(f'"{a}"' for a in sys.argv[1:])
        else:
            params = f'"{Path(__file__).resolve()}" ' + " ".join(f'"{a}"' for a in sys.argv[1:])
        ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)


def run_setup() -> None:
    if IS_WINDOWS:
        current_browser_cmd = query_windows_default_browser_cmd()

        if not is_admin():
            relaunch_as_admin()
            return

        if getattr(sys, "frozen", False):
            self_path = str(Path(sys.executable).resolve())
            command = f'"{self_path}" "%1"'
        else:
            py_exe = str(Path(sys.executable).resolve())
            script = str(Path(__file__).resolve())
            command = f'"{py_exe}" "{script}" "%1"'
            self_path = py_exe

        fallback_cmd = current_browser_cmd or get_saved_browser_cmd()
        if not fallback_cmd:
            detected_browsers = get_installed_browsers()
            non_edge = [p for p in detected_browsers if "msedge.exe" not in p.lower()]
            best_browser = non_edge[0] if non_edge else (detected_browsers[0] if detected_browsers else None)
            if best_browser:
                fallback_cmd = f'"{best_browser}" "%1"'

        hklm = winreg.HKEY_LOCAL_MACHINE

        with winreg.CreateKey(hklm, rf"SOFTWARE\{APP_NAME}\Capabilities") as k:
            winreg.SetValueEx(k, "ApplicationName", 0, winreg.REG_SZ, FRIENDLY_NAME)
            winreg.SetValueEx(k, "ApplicationDescription", 0, winreg.REG_SZ, DESCRIPTION)
            winreg.SetValueEx(k, "ApplicationIcon", 0, winreg.REG_SZ, f"{self_path},0")

        with winreg.CreateKey(hklm, rf"SOFTWARE\{APP_NAME}\Capabilities\URLAssociations") as k:
            winreg.SetValueEx(k, "http", 0, winreg.REG_SZ, f"{APP_NAME}URL")
            winreg.SetValueEx(k, "https", 0, winreg.REG_SZ, f"{APP_NAME}URL")

        with winreg.CreateKey(hklm, r"SOFTWARE\RegisteredApplications") as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, rf"SOFTWARE\{APP_NAME}\Capabilities")

        with winreg.CreateKey(hklm, rf"SOFTWARE\Classes\{APP_NAME}URL") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f"{FRIENDLY_NAME} Document")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
            winreg.SetValueEx(k, "FriendlyTypeName", 0, winreg.REG_SZ, FRIENDLY_NAME)

        with winreg.CreateKey(hklm, rf"SOFTWARE\Classes\{APP_NAME}URL\DefaultIcon") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f"{self_path},0")

        with winreg.CreateKey(hklm, rf"SOFTWARE\Classes\{APP_NAME}URL\shell\open\command") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, command)

        if fallback_cmd:
            save_browser_cmd(fallback_cmd)

        detected_exe = extract_exe_from_command(fallback_cmd) if fallback_cmd else None
        detected_name = Path(detected_exe).name if detected_exe else "Auto-detect"
        songs_path = find_osu_songs_path()

        msgbox(
            f"Registered successfully!\n\n"
            f"• Fallback Browser: {detected_name}\n"
            f"• osu! Songs Directory: {songs_path}\n\n"
            f"Final Step (Windows Default Apps):\n"
            f"1. The Default Apps settings page will now open.\n"
            f"2. Search for \"osu! Map Handler\" or \"Web browser\".\n"
            f"3. Select \"osu! Map Handler\" as your default web browser.\n\n"
            f"How to use:\n"
            f"• Normal Click in multiplayer lobby/chat: Auto-downloads & imports into osu!\n"
            f"• Shift + Click: Opens the beatmap in your browser ({detected_name})\n"
            f"• User profiles & outside links: Opens directly in your browser",
            f"{FRIENDLY_NAME} Setup",
        )

        try:
            os.startfile("ms-settings:defaultapps")
        except Exception as e:
            log(f"could not open Default Apps: {e}")

    elif IS_LINUX:
        script_path = str(Path(__file__).resolve())
        apps_dir = Path.home() / ".local" / "share" / "applications"
        apps_dir.mkdir(parents=True, exist_ok=True)
        desktop_file = apps_dir / "osu-map-handler.desktop"

        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={FRIENDLY_NAME}\n"
            f"Comment={DESCRIPTION}\n"
            f"Exec=python3 \"{script_path}\" %u\n"
            "Icon=osu\n"
            "Terminal=false\n"
            "MimeType=x-scheme-handler/http;x-scheme-handler/https;\n"
            "Categories=Game;\n"
        )
        desktop_file.write_text(content, encoding="utf-8")

        if shutil.which("xdg-mime"):
            subprocess.run(["xdg-mime", "default", "osu-map-handler.desktop", "x-scheme-handler/http"], check=False)
            subprocess.run(["xdg-mime", "default", "osu-map-handler.desktop", "x-scheme-handler/https"], check=False)
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(apps_dir)], check=False)

        detected = get_installed_browsers()
        best = detected[0] if detected else "firefox"
        save_browser_cmd(best)

        songs_path = find_osu_songs_path()
        msgbox(
            f"osu! Map Handler registered on Linux!\n\n"
            f"• Desktop File: {desktop_file}\n"
            f"• Fallback Browser: {best}\n"
            f"• osu! Songs Directory: {songs_path}\n\n"
            f"Configured as default handler for http and https protocols.",
            f"{FRIENDLY_NAME} Setup",
        )


def run_uninstall() -> None:
    """Removes registration as default URL handler on Windows or Linux."""
    if IS_WINDOWS:
        if not is_admin():
            relaunch_as_admin()
            return

        hklm = winreg.HKEY_LOCAL_MACHINE
        keys_to_delete = [
            (hklm, r"SOFTWARE\RegisteredApplications", APP_NAME),
            (hklm, rf"SOFTWARE\Classes\{APP_NAME}URL\shell\open\command", None),
            (hklm, rf"SOFTWARE\Classes\{APP_NAME}URL\shell\open", None),
            (hklm, rf"SOFTWARE\Classes\{APP_NAME}URL\shell", None),
            (hklm, rf"SOFTWARE\Classes\{APP_NAME}URL\DefaultIcon", None),
            (hklm, rf"SOFTWARE\Classes\{APP_NAME}URL", None),
            (hklm, rf"SOFTWARE\{APP_NAME}\Capabilities\URLAssociations", None),
            (hklm, rf"SOFTWARE\{APP_NAME}\Capabilities", None),
            (hklm, rf"SOFTWARE\{APP_NAME}", None),
        ]
        for root, subkey, val_name in keys_to_delete:
            try:
                if val_name is not None:
                    with winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.DeleteValue(k, val_name)
                else:
                    winreg.DeleteKey(root, subkey)
            except Exception:
                pass

        msgbox(
            "osu! Map Handler registration removed successfully!\n\n"
            "Windows Settings (Default Apps) will open so you can set your preferred browser back to default.",
            f"{FRIENDLY_NAME} Uninstall",
        )
        try:
            os.startfile("ms-settings:defaultapps")
        except Exception:
            pass

    elif IS_LINUX:
        desktop_file = Path.home() / ".local" / "share" / "applications" / "osu-map-handler.desktop"
        if desktop_file.exists():
            desktop_file.unlink()
        apps_dir = Path.home() / ".local" / "share" / "applications"
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(apps_dir)], check=False)
        msgbox("osu! Map Handler desktop file removed successfully.", f"{FRIENDLY_NAME} Uninstall")


# ---------------------------------------------------------------------------
# Main Handler Routing
# ---------------------------------------------------------------------------

def run_handler(url: str, force_browser: bool = False, foreground_proc: str | None = None, window_title: str | None = None) -> None:
    if url.lower().startswith("osu-map://"):
        url = "https://" + url[10:]
    elif url.lower().startswith("osu://b/"):
        url = f"https://osu.ppy.sh/b/{url[8:]}"
    elif url.lower().startswith("osu://s/"):
        url = f"https://osu.ppy.sh/s/{url[8:]}"

    # Non-beatmap URLs (e.g. user profile https://osu.ppy.sh/users/...)
    if not BEATMAP_RE.match(url):
        log(f"Non-beatmap link: forwarding to browser: {url}")
        open_in_real_browser(url, bring_to_front=True)
        return

    # Shift held -> Open in browser
    if force_browser:
        log(f"Shift held -> Forwarding beatmap link to browser: {url}")
        open_in_real_browser(url, bring_to_front=True)
        return

    # Check if click originated from inside osu!
    from_osu = is_osu_foreground(foreground_proc, window_title)
    if not from_osu:
        log(f"Clicked outside osu! (foreground was {foreground_proc!r}, title: {window_title!r}) -> Forwarding: {url}")
        open_in_real_browser(url, bring_to_front=True)
        return

    # Resolve beatmapset ID
    set_id = resolve_set_id(url)
    if not set_id:
        log(f"Could not resolve set ID for {url} -> Forwarding to browser")
        open_in_real_browser(url, bring_to_front=True)
        return

    # Check if already installed
    songs_path = find_osu_songs_path()
    if is_installed(set_id, songs_path):
        log(f"Beatmap set {set_id} already installed -> Staying in game")
        return

    # Parallel racing download
    osz = download_osz(set_id)
    if not osz:
        log(f"All mirrors failed for set {set_id} -> Forwarding to browser as fallback")
        open_in_real_browser(url, bring_to_front=True)
        return

    # Import into osu!
    import_beatmap_into_osu(osz, songs_path)


# ---------------------------------------------------------------------------
# CLI & Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    shift_held = is_shift_held()
    foreground_proc, window_title = get_foreground_process_info()

    args = sys.argv[1:]

    if not args:
        run_setup()
        return

    first_arg = args[0]

    if first_arg in ("--setup", "-s", "/setup"):
        run_setup()
        return

    if first_arg in ("--uninstall", "-u", "/uninstall"):
        run_uninstall()
        return

    if first_arg in ("--list-browsers", "-l"):
        print("Detected installed browsers:")
        for b in get_installed_browsers():
            print(f"  - {b}")
        saved = get_saved_browser_cmd()
        print(f"Configured FallbackBrowserCmd: {saved}")
        return

    if first_arg in ("--set-browser", "-b"):
        if len(args) < 2:
            print("Usage: --set-browser <browser_name_or_path>")
            return
        target = args[1].lower()
        installed = get_installed_browsers()
        matched = None
        for b in installed:
            if target in Path(b).name.lower() or target in b.lower():
                matched = b
                break
        if not matched and (Path(args[1]).is_file() or shutil.which(args[1])):
            matched = str(Path(args[1]).resolve()) if Path(args[1]).is_file() else args[1]

        if matched:
            save_browser_cmd(f'"{matched}" "%1"' if IS_WINDOWS else matched)
            print(f"Successfully set fallback browser to: {matched}")
        else:
            print(f"Browser not found matching: {args[1]}")
            print("Installed browsers:")
            for b in installed:
                print(f"  - {b}")
        return

    if first_arg in ("--help", "-h", "/?"):
        print(__doc__)
        return

    run_handler(first_arg, force_browser=shift_held, foreground_proc=foreground_proc, window_title=window_title)


if __name__ == "__main__":
    main()