"""
Build script for The Daily Melt Linux distribution.

Creates a PyInstaller --onedir bundle from app_desktop.py with all
required data files (templates, config, icon) and hidden imports.
Optionally packages the result as an AppImage.

Usage:
    python build/build_linux.py              # Standard build
    python build/build_linux.py --clean      # Clean previous build first
    python build/build_linux.py --debug      # Console window for debugging
    python build/build_linux.py --appimage   # Build + package as AppImage
"""

import subprocess
import sys
import shutil
import os
import stat
import platform
from pathlib import Path

if sys.platform != "linux":
    sys.exit("This build script must be run on Linux.")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent          # build/
PROJECT_ROOT = SCRIPT_DIR.parent                      # repo root
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build" / "_pyinstaller"   # temp build artifacts
ENTRY_POINT = PROJECT_ROOT / "app_desktop.py"
APP_NAME = "The Daily Melt"
APP_ID = "com.spxincometrader.thedailymelt"
DIST_FOLDER = "SPXIncomeTrader"                       # output folder name
VERSION = "1.0.0"

# Icon (PNG used directly on Linux)
ICON_PNG = PROJECT_ROOT / "assets" / "icon.png"

# ---------------------------------------------------------------------------
# System dependency checks
# ---------------------------------------------------------------------------

def check_system_dependencies():
    """Verify system packages needed by pywebview and pystray are installed."""
    missing = []

    # Check PyGObject (python3-gi)
    try:
        import gi  # noqa: F401
    except ImportError:
        missing.append("python3-gi (PyGObject)")

    # Check GTK 3 typelib
    if not missing:
        try:
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk  # noqa: F401
        except (ValueError, ImportError):
            missing.append("gir1.2-gtk-3.0 (GTK 3 introspection data)")

    # Check WebKit2 typelib (try 4.1 first, then 4.0)
    if not missing:
        webkit_found = False
        for ver in ("4.1", "4.0"):
            try:
                gi.require_version("WebKit2", ver)
                from gi.repository import WebKit2  # noqa: F401
                webkit_found = True
                break
            except (ValueError, ImportError):
                continue
        if not webkit_found:
            missing.append("gir1.2-webkit2-4.1 (WebKit2 introspection data)")

    if missing:
        print("ERROR: Missing system packages required for pywebview:")
        for pkg in missing:
            print(f"  - {pkg}")
        print()
        print("Install them with (Ubuntu/Debian):")
        print("  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1")
        print()
        print("Or (Fedora):")
        print("  sudo dnf install python3-gobject python3-cairo gtk3 webkit2gtk4.1")
        sys.exit(1)

    # Check for appindicator (needed by pystray for system tray) - non-fatal
    has_indicator = False
    try:
        for pkg_cmd in [
            ["dpkg", "-s", "libayatana-appindicator3-dev"],
            ["dpkg", "-s", "libappindicator3-dev"],
            ["rpm", "-q", "libappindicator-gtk3-devel"],
        ]:
            result = subprocess.run(pkg_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                has_indicator = True
                break
    except FileNotFoundError:
        # Neither dpkg nor rpm available; skip this check
        has_indicator = True  # Assume OK on non-standard distros

    if not has_indicator:
        print("WARNING: No appindicator library found (needed by pystray for system tray).")
        print("  Install one of:")
        print("    sudo apt install libayatana-appindicator3-dev")
        print("    sudo apt install libappindicator3-dev")
        print("  The app will work but the system tray icon may not appear.\n")

    print("System dependency check passed.\n")


# ---------------------------------------------------------------------------
# Data files to bundle  (source_path, dest_folder_in_bundle)
# ---------------------------------------------------------------------------

DATA_FILES = [
    # Dashboard HTML templates
    (PROJECT_ROOT / "dashboard" / "templates" / "index.html",    "dashboard/templates"),
    (PROJECT_ROOT / "dashboard" / "templates" / "settings.html", "dashboard/templates"),
    (PROJECT_ROOT / "dashboard" / "templates" / "setup.html",    "dashboard/templates"),
    # Strategy config (bundled default)
    (PROJECT_ROOT / "config" / "strategy_params.yaml",           "config"),
]

# Include icon in bundle
if ICON_PNG.exists():
    DATA_FILES.append((ICON_PNG, "assets"))

# If dashboard/static exists, include everything in it (preserving subdirs)
STATIC_DIR = PROJECT_ROOT / "dashboard" / "static"
if STATIC_DIR.is_dir():
    for f in STATIC_DIR.rglob("*"):
        if f.is_file():
            rel = f.relative_to(STATIC_DIR)
            dest = f"dashboard/static/{rel.parent}" if rel.parent != Path(".") else "dashboard/static"
            DATA_FILES.append((f, dest))

# ---------------------------------------------------------------------------
# Hidden imports PyInstaller commonly misses
# ---------------------------------------------------------------------------

HIDDEN_IMPORTS = [
    # Flask ecosystem
    "flask",
    "flask.json",
    "flask.templating",
    "werkzeug",
    "werkzeug.serving",
    "werkzeug.middleware",
    "jinja2",
    "jinja2.ext",
    # Socket / Engine IO
    "engineio",
    "engineio.async_drivers.threading",
    "socketio",
    # SQLite / SQLAlchemy
    "sqlite3",
    "sqlalchemy",
    "sqlalchemy.dialects.sqlite",
    # Keyring (Linux backends)
    "keyring",
    "keyring.backends",
    "keyring.backends.SecretService",
    "keyring.backends.chainer",
    "keyring.backends.null",
    # WebView (GTK/WebKit backend on Linux)
    "webview",
    # Third-party
    "platformdirs",
    "dotenv",
    "pytz",
    "yfinance",
    "pystray",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "requests",
    "requests_oauthlib",
    "yaml",
    "plotly",
    # Schwab broker
    "schwab",
    "authlib",
    "httpx",
    # GTK / GI (pywebview backend)
    "gi",
    "gi.repository",
]

# Packages whose submodules PyInstaller should collect (dynamic imports)
COLLECT_SUBMODULES = [
    "flask",
    "werkzeug",
    "jinja2",
    "engineio",
    "keyring",
    "pystray",
    "webview",
    "schwab",
]

# Packages to exclude (dev-only, not needed at runtime)
EXCLUDES = [
    "pytest",
    "black",
    "flake8",
    "pytest_cov",
    "tkinter",
    "matplotlib",
]

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def clean():
    """Remove previous build artifacts."""
    # Clean both the renamed output and the PyInstaller intermediate dir
    for d in [DIST_DIR / DIST_FOLDER, DIST_DIR / APP_NAME, BUILD_DIR]:
        if d.exists():
            print(f"Removing {d}")
            shutil.rmtree(d)
    spec = PROJECT_ROOT / f"{APP_NAME}.spec"
    if spec.exists():
        spec.unlink()


def build(debug=False):
    """Run PyInstaller to create a single-folder distribution."""
    if not ENTRY_POINT.exists():
        sys.exit(f"Entry point not found: {ENTRY_POINT}")

    check_system_dependencies()

    icon_path = str(ICON_PNG) if ICON_PNG.exists() else None
    if icon_path:
        print(f"Using icon: {icon_path}")
    else:
        print("WARNING: No icon found. Building without an application icon.")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onedir",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(PROJECT_ROOT),
        "--noconfirm",
    ]

    if icon_path:
        cmd.extend(["--icon", icon_path])

    # --noconsole hides the terminal window in production
    if not debug:
        cmd.append("--noconsole")

    # Data files: --add-data "src:dest" (colon separator on Linux)
    for src, dest in DATA_FILES:
        src = Path(src)
        if not src.exists():
            print(f"WARNING: data file missing, skipping: {src}")
            continue
        cmd.extend(["--add-data", f"{src}:{dest}"])

    # Hidden imports
    for imp in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", imp])

    # Collect submodules for packages with dynamic imports
    for pkg in COLLECT_SUBMODULES:
        cmd.extend(["--collect-submodules", pkg])

    # Exclude dev-only packages
    for exc in EXCLUDES:
        cmd.extend(["--exclude-module", exc])

    cmd.append(str(ENTRY_POINT))

    print("Running PyInstaller...")
    print(f"  Command: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"PyInstaller failed with exit code {result.returncode}")

    # Rename output folder from "The Daily Melt" to "SPXIncomeTrader"
    built_dir = DIST_DIR / APP_NAME
    target_dir = DIST_DIR / DIST_FOLDER
    if built_dir.exists() and built_dir != target_dir:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        built_dir.rename(target_dir)

    print(f"\nBuild complete: {target_dir}")


def verify():
    """Verify the output directory contains required files."""
    app_dir = DIST_DIR / DIST_FOLDER
    exe = app_dir / APP_NAME

    if not app_dir.exists():
        sys.exit(f"Build output directory not found: {app_dir}")

    errors = []
    warnings = []

    # Check executable
    if not exe.exists():
        errors.append(f"Executable missing: {exe}")
    elif not os.access(str(exe), os.X_OK):
        errors.append(f"Executable is not marked as executable: {exe}")

    # Check bundled data files (PyInstaller puts them in _internal/)
    expected_data = [
        "_internal/dashboard/templates/index.html",
        "_internal/dashboard/templates/settings.html",
        "_internal/dashboard/templates/setup.html",
        "_internal/config/strategy_params.yaml",
    ]
    for rel in expected_data:
        full = app_dir / rel
        if not full.exists():
            errors.append(f"Missing bundled file: {rel}")

    # Check icon (non-fatal)
    icon_in_bundle = app_dir / "_internal/assets/icon.png"
    if not icon_in_bundle.exists():
        warnings.append("Icon not bundled (non-fatal): _internal/assets/icon.png")

    # Sanity check: some .so files should exist
    internal = app_dir / "_internal"
    if internal.exists():
        so_files = list(internal.rglob("*.so"))
        if not so_files:
            errors.append("No .so files in _internal/ -- build may be broken")
    else:
        errors.append("_internal/ directory missing -- PyInstaller output is incomplete")

    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  - {w}")

    if errors:
        print("\nVerification FAILED:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\nVerification PASSED -- all expected files present.")
        print(f"  Executable: {exe}")
        print(f"  Bundle size: {_dir_size_mb(app_dir):.1f} MB")


# ---------------------------------------------------------------------------
# AppImage packaging
# ---------------------------------------------------------------------------


def build_appimage():
    """Package the PyInstaller output as an AppImage."""
    app_dir = DIST_DIR / DIST_FOLDER
    if not app_dir.exists():
        sys.exit(f"Build output not found: {app_dir}\nRun the build first (without --appimage-only).")

    appdir = DIST_DIR / "AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)

    print("\nPackaging as AppImage...")

    # Create AppDir structure
    usr_bin = appdir / "usr" / "bin"
    usr_icons = appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
    usr_bin.mkdir(parents=True)
    usr_icons.mkdir(parents=True)

    # Symlink the PyInstaller output into usr/bin
    target = usr_bin / DIST_FOLDER
    target.symlink_to(app_dir.resolve())

    # Copy icon
    if ICON_PNG.exists():
        shutil.copy2(ICON_PNG, usr_icons / "spxincometrader.png")
        shutil.copy2(ICON_PNG, appdir / "spxincometrader.png")

    # Desktop entry
    desktop = appdir / "spxincometrader.desktop"
    desktop.write_text(f"""[Desktop Entry]
Name={APP_NAME}
Exec=SPXIncomeTrader/{APP_NAME}
Icon=spxincometrader
Type=Application
Categories=Finance;
Comment=SPX options income trading bot
""")

    # AppRun launcher
    apprun = appdir / "AppRun"
    apprun.write_text(f"""#!/bin/bash
HERE="$(dirname "$(readlink -f "${{0}}")")"
exec "$HERE/usr/bin/{DIST_FOLDER}/{APP_NAME}" "$@"
""")
    apprun.chmod(apprun.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Find appimagetool
    appimagetool = shutil.which("appimagetool")
    if not appimagetool:
        appimagetool = shutil.which("appimagetool-x86_64.AppImage")

    if not appimagetool:
        print(f"\nAppDir created at: {appdir}")
        print("appimagetool not found on PATH. To finish packaging:")
        print("  1. Download: https://github.com/AppImage/AppImageKit/releases")
        print("  2. chmod +x appimagetool-x86_64.AppImage")
        print(f"  3. ./appimagetool-x86_64.AppImage {appdir} {DIST_DIR}/{APP_NAME}-{VERSION}-x86_64.AppImage")
        return

    output_name = DIST_DIR / f"{APP_NAME}-{VERSION}-x86_64.AppImage"
    print(f"Running appimagetool...")
    result = subprocess.run(
        [appimagetool, str(appdir), str(output_name)],
        env={**os.environ, "ARCH": "x86_64"},
    )
    if result.returncode != 0:
        sys.exit(f"appimagetool failed with exit code {result.returncode}")

    print(f"\nAppImage created: {output_name}")
    print(f"  Size: {output_name.stat().st_size / (1024 * 1024):.1f} MB")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dir_size_mb(path):
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build The Daily Melt for Linux")
    parser.add_argument("--clean", action="store_true", help="Remove previous build artifacts first")
    parser.add_argument("--debug", action="store_true", help="Keep console window visible (for debugging)")
    parser.add_argument("--verify-only", action="store_true", help="Only run verification on existing build")
    parser.add_argument("--appimage", action="store_true", help="Package as AppImage after building")
    parser.add_argument("--appimage-only", action="store_true", help="Only run AppImage packaging (skip build)")
    args = parser.parse_args()

    if args.verify_only:
        verify()
        return

    if args.appimage_only:
        build_appimage()
        return

    if args.clean:
        clean()

    build(debug=args.debug)
    verify()

    if args.appimage:
        build_appimage()


if __name__ == "__main__":
    main()
