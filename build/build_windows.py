"""
Build script for SPX Income Trader Windows distribution.

Creates a PyInstaller --onedir bundle from app_desktop.py with all
required data files (templates, config, icon) and hidden imports.

Usage:
    python build/build_windows.py          # Standard build
    python build/build_windows.py --clean  # Clean previous build first
    python build/build_windows.py --debug  # Console window for debugging
"""

import subprocess
import sys
import shutil
import platform
from pathlib import Path

if platform.system() != "Windows":
    sys.exit("This build script must be run on Windows.")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent          # build/
PROJECT_ROOT = SCRIPT_DIR.parent                      # repo root
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build" / "_pyinstaller"   # temp build artifacts
ENTRY_POINT = PROJECT_ROOT / "app_desktop.py"
APP_NAME = "The Daily Melt"
VERSION = "1.0.0"

# Icon paths (in order of preference)
ICON_ICO = PROJECT_ROOT / "assets" / "icon.ico"
ICON_PNG = PROJECT_ROOT / "assets" / "icon.png"

# ---------------------------------------------------------------------------
# Data files to bundle  (source_path, dest_folder_in_bundle)
# ---------------------------------------------------------------------------

DATA_FILES = [
    # Dashboard HTML templates
    (PROJECT_ROOT / "dashboard" / "templates" / "index.html",    "dashboard/templates"),
    (PROJECT_ROOT / "dashboard" / "templates" / "settings.html", "dashboard/templates"),
    (PROJECT_ROOT / "dashboard" / "templates" / "setup.html",    "dashboard/templates"),
    # Strategy config (bundled default -- user copy lives in AppData)
    (PROJECT_ROOT / "config" / "strategy_params.yaml",           "config"),
]

# Include icons in bundle if they exist
if ICON_ICO.exists():
    DATA_FILES.append((ICON_ICO, "assets"))
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
    # Keyring (Windows backend)
    "keyring",
    "keyring.backends",
    "keyring.backends.Windows",
    "keyring.backends.null",
    # WebView + pythonnet backend
    "webview",
    "pythonnet",
    "clr",
    "clr_loader",
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
    "clr_loader",
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
    "scipy",
]

# ---------------------------------------------------------------------------
# Icon handling
# ---------------------------------------------------------------------------


def _resolve_icon():
    """Find or convert an icon for the build. Returns path string or None."""
    if ICON_ICO.exists():
        print(f"Using icon: {ICON_ICO}")
        return str(ICON_ICO)

    # Try converting .png to .ico using Pillow
    if ICON_PNG.exists():
        print(f"Converting {ICON_PNG} to .ico via Pillow...")
        try:
            from PIL import Image

            img = Image.open(str(ICON_PNG))
            ICON_ICO.parent.mkdir(parents=True, exist_ok=True)
            img.save(
                str(ICON_ICO),
                format="ICO",
                sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
            print(f"  Created {ICON_ICO}")
            return str(ICON_ICO)
        except Exception as e:
            print(f"  Conversion failed: {e}")

    print("WARNING: No icon found. Building without an application icon.")
    return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def clean():
    """Remove previous build artifacts."""
    for d in [DIST_DIR / APP_NAME, BUILD_DIR]:
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

    icon_path = _resolve_icon()

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

    # Data files: --add-data "src;dest" (semicolon separator on Windows)
    for src, dest in DATA_FILES:
        src = Path(src)
        if not src.exists():
            print(f"WARNING: data file missing, skipping: {src}")
            continue
        cmd.extend(["--add-data", f"{src};{dest}"])

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

    print(f"\nBuild complete: {DIST_DIR / APP_NAME}")


def verify():
    """Verify the output directory contains required files."""
    app_dir = DIST_DIR / APP_NAME
    exe = app_dir / f"{APP_NAME}.exe"

    if not app_dir.exists():
        sys.exit(f"Build output directory not found: {app_dir}")

    errors = []
    warnings = []

    # Check executable
    if not exe.exists():
        errors.append(f"Executable missing: {exe}")

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
    icon_in_bundle = app_dir / "_internal/assets/icon.ico"
    if not icon_in_bundle.exists():
        warnings.append("Icon not bundled (non-fatal): _internal/assets/icon.ico")

    # Sanity check: some .pyd or .dll files should exist
    internal = app_dir / "_internal"
    if internal.exists():
        pyd_files = list(internal.rglob("*.pyd"))
        dll_files = list(internal.rglob("*.dll"))
        if not pyd_files and not dll_files:
            errors.append("No .pyd or .dll files in _internal/ -- build may be broken")
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


def _dir_size_mb(path):
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build SPX Income Trader for Windows")
    parser.add_argument("--clean", action="store_true", help="Remove previous build artifacts first")
    parser.add_argument("--debug", action="store_true", help="Keep console window visible (for debugging)")
    parser.add_argument("--verify-only", action="store_true", help="Only run verification on existing build")
    args = parser.parse_args()

    if args.verify_only:
        verify()
        return

    if args.clean:
        clean()

    build(debug=args.debug)
    verify()


if __name__ == "__main__":
    main()
