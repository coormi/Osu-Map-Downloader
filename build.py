"""
build.py — One-click build script for osu! Map Handler.

Builds:
1. dist/OsuMapEngine.exe (PyInstaller bundle of Python engine)
2. dist/OsuMapHandler.exe (Instant native Windows launcher with embedded icon)
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

def find_csc() -> Path | None:
    which_csc = shutil.which("csc")
    if which_csc:
        return Path(which_csc)

    windir = os.environ.get("SystemRoot", os.environ.get("windir", r"C:\Windows"))
    candidates = [
        Path(windir) / r"Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        Path(windir) / r"Microsoft.NET\Framework\v4.0.30319\csc.exe",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None

def main():
    if sys.platform != "win32":
        print("Note: build.py compiles Windows executables (.exe).")
        print("On Linux, run 'python3 osu_map_handler.py --setup'.")
        sys.exit(1)

    root = Path(__file__).resolve().parent
    dist_dir = root / "dist"
    dist_dir.mkdir(exist_ok=True)

    spec_file = root / "OsuMapEngine.spec"
    launcher_cs = root / "launcher.cs"
    icon_file = root / "osu_handler.ico"

    if not spec_file.is_file():
        print(f"ERROR: Spec file '{spec_file.name}' not found!")
        sys.exit(1)

    if not launcher_cs.is_file():
        print(f"ERROR: Launcher source '{launcher_cs.name}' not found!")
        sys.exit(1)

    print("=== 1. Building OsuMapEngine.exe with PyInstaller ===")
    try:
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", str(spec_file), "--noconfirm"],
            check=True,
            cwd=root
        )
    except subprocess.CalledProcessError as e:
        print(f"\nERROR: PyInstaller build failed with exit code {e.returncode}.")
        print("Tip: Ensure OsuMapEngine.exe is not currently running.")
        sys.exit(e.returncode)
    except FileNotFoundError:
        print("\nERROR: PyInstaller is not installed. Please run: pip install pyinstaller")
        sys.exit(1)

    print("\n=== 2. Compiling native launcher dist/OsuMapHandler.exe ===")
    csc_path = find_csc()
    if not csc_path:
        print("ERROR: csc.exe compiler not found! Ensure .NET Framework 4.0+ is enabled.")
        sys.exit(1)

    launcher_exe = dist_dir / "OsuMapHandler.exe"
    engine_exe = dist_dir / "OsuMapEngine.exe"
    csc_cmd = [
        str(csc_path),
        "/nologo",
        "/optimize+",
        "/target:winexe",
        f"/out:{launcher_exe}",
        str(launcher_cs)
    ]
    if icon_file.is_file():
        csc_cmd.insert(4, f"/win32icon:{icon_file}")
    if engine_exe.is_file():
        csc_cmd.append(f"/resource:{engine_exe},OsuMapEngine.exe")

    try:
        subprocess.run(csc_cmd, check=True, cwd=root)
    except subprocess.CalledProcessError as e:
        print(f"\nERROR: C# compilation failed with exit code {e.returncode}.")
        print("Tip: Ensure OsuMapHandler.exe is not currently running.")
        sys.exit(e.returncode)

    print(f"\nBuild complete! Executables ready in {dist_dir}:")
    print(f"  - {launcher_exe.name} (Instant native launcher)")
    print(f"  - {(dist_dir / 'OsuMapEngine.exe').name} (Engine)")

if __name__ == "__main__":
    main()
