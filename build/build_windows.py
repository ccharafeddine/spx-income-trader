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
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent          # build/
PROJECT_ROOT = SCRIPT_DIR.parent                      # repo root
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build" / "_pyinstaller"   # temp build artifacts
ENTRY_POINT = PROJECT_ROOT / "app_desktop.py"
ICON = PROJECT_ROOT / "assets" / "icon.ico"
APP_NAME = "SPXIncomeTrader"

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
    # App icon
    (ICON,                                                       "assets"),
]

# If dashboard/static exists, include everything in it
STATIC_DIR = PROJECT_ROOT / "dashboard" / "static"
if STATIC_DIR.is_dir():
    DATA_FILES.append((STATIC_DIR, "dashboard/static"))

# ---------------------------------------------------------------------------
# Hidden imports PyInstaller can't detect automatically
# ---------------------------------------------------------------------------

HIDDEN_IMPORTS = [
    "keyring.backends.Windows",
    "webview",
    "engineio.async_drivers.threading",  # only if python-engineio installed
    # Flask / Jinja internals sometimes missed
    "jinja2.ext",
    # SQLAlchemy dialects (module path varies by version)
    "sqlalchemy.dialects.sqlite",
    # Ensure keyring backend chain works
    "keyring.backends",
    "keyring.backends.null",
]

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


def build(debug: bool = False):
    """Run PyInstaller."""
    if not ENTRY_POINT.exists():
        sys.exit(f"Entry point not found: {ENTRY_POINT}")
    if not ICON.exists():
        sys.exit(f"Icon not found: {ICON}. Run the placeholder generator first.")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onedir",
        "--icon", str(ICON),
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(PROJECT_ROOT),
    ]

    if not debug:
        cmd.append("--windowed")

    # Data files: --add-data "src;dest"
    for src, dest in DATA_FILES:
        if not Path(src).exists():
            print(f"WARNING: data file missing, skipping: {src}")
            continue
        cmd.extend(["--add-data", f"{src};{dest}"])

    # Hidden imports
    for imp in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", imp])

    # Exclude heavyweight packages not needed at runtime
    for exc in ["pytest", "black", "flake8", "pytest_cov"]:
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

    errors = []

    if not exe.exists():
        errors.append(f"Executable missing: {exe}")

    # Check bundled data files
    expected = [
        "_internal/dashboard/templates/index.html",
        "_internal/dashboard/templates/settings.html",
        "_internal/dashboard/templates/setup.html",
        "_internal/config/strategy_params.yaml",
        "_internal/assets/icon.ico",
    ]
    for rel in expected:
        full = app_dir / rel
        if not full.exists():
            errors.append(f"Missing bundled file: {rel}")

    if errors:
        print("\nVerification FAILED:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\nVerification PASSED -- all expected files present.")
        print(f"  Executable: {exe}")
        print(f"  Bundle size: {_dir_size_mb(app_dir):.1f} MB")


def _dir_size_mb(path: Path) -> float:
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
