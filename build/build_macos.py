"""
Build script for SPX Income Trader macOS distribution.

Creates a py2app .app bundle from app_desktop.py with all
required data files (templates, config, icon) and proper Info.plist.

Usage:
    python build/build_macos.py          # Standard build
    python build/build_macos.py --clean  # Clean previous build first
    python build/build_macos.py --debug  # Alias (symlink) mode for debugging
"""

import subprocess
import sys
import shutil
import platform
from pathlib import Path

if platform.system() != "Darwin":
    sys.exit("This build script must be run on macOS.")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent          # build/
PROJECT_ROOT = SCRIPT_DIR.parent                      # repo root
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build" / "_py2app"        # temp build artifacts
ENTRY_POINT = PROJECT_ROOT / "app_desktop.py"
ICON_ICNS = PROJECT_ROOT / "assets" / "icon.icns"
ICON_ICO = PROJECT_ROOT / "assets" / "icon.ico"
APP_NAME = "SPX Income Trader"
BUNDLE_ID = "com.spxincometrader.app"
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Data files to bundle  (dest_folder_in_Resources, [source_files])
# ---------------------------------------------------------------------------

DATA_FILES = [
    ("dashboard/templates", [
        str(PROJECT_ROOT / "dashboard" / "templates" / "index.html"),
        str(PROJECT_ROOT / "dashboard" / "templates" / "settings.html"),
        str(PROJECT_ROOT / "dashboard" / "templates" / "setup.html"),
    ]),
    ("config", [
        str(PROJECT_ROOT / "config" / "strategy_params.yaml"),
    ]),
]

# If dashboard/static exists, include everything in it
STATIC_DIR = PROJECT_ROOT / "dashboard" / "static"
if STATIC_DIR.is_dir():
    static_files = [str(f) for f in STATIC_DIR.rglob("*") if f.is_file()]
    if static_files:
        # Preserve subdirectory structure under dashboard/static
        for f in STATIC_DIR.rglob("*"):
            if f.is_file():
                rel = f.relative_to(STATIC_DIR)
                dest = f"dashboard/static/{rel.parent}" if rel.parent != Path(".") else "dashboard/static"
                DATA_FILES.append((dest, [str(f)]))

# ---------------------------------------------------------------------------
# py2app options
# ---------------------------------------------------------------------------

PY2APP_OPTIONS = {
    "argv_emulation": False,
    "iconfile": str(ICON_ICNS),
    "bundle_identifier": BUNDLE_ID,
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundleExecutable": APP_NAME.replace(" ", ""),
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        # Menu-bar only capable (set to True to hide from Dock)
        "LSUIElement": False,
        # Retina / HiDPI support
        "NSHighResolutionCapable": True,
        # Minimum macOS version
        "LSMinimumSystemVersion": "10.15",
        # App category
        "LSApplicationCategoryType": "public.app-category.finance",
        # Privacy descriptions (required by modern macOS)
        "NSAppleEventsUsageDescription": "SPX Income Trader needs automation access.",
    },
    "packages": [
        # Project packages (no __init__.py -- namespace packages)
        "src",
        "src.brokers",
        "src.core",
        "src.data",
        "src.models",
        "src.utils",
        "dashboard",
        "config",
        "database",
        # Third-party
        "flask",
        "jinja2",
        "sqlalchemy",
        "keyring",
        "plotly",
        "yaml",
        "engineio",
        "requests",
        "requests_oauthlib",
        "platformdirs",
        "dotenv",
        "pytz",
        "yfinance",
        "pystray",
        "PIL",
    ],
    "includes": [
        "keyring.backends",
        "keyring.backends.macOS",
        "webview",
        "engineio.async_drivers.threading",
        "jinja2.ext",
        "sqlalchemy.dialects.sqlite",
    ],
    "excludes": [
        "pytest",
        "black",
        "flake8",
        "pytest_cov",
        "tkinter",
        "matplotlib",
    ],
    "resources": [],
    "site_packages": True,
    "strip": True,
    "optimize": 2,
}

# ---------------------------------------------------------------------------
# Icon generation
# ---------------------------------------------------------------------------


def _ensure_icon():
    """Create a placeholder .icns icon if one doesn't exist."""
    if ICON_ICNS.exists():
        return

    ICON_ICNS.parent.mkdir(parents=True, exist_ok=True)

    # Try converting from .ico using sips (macOS built-in)
    if ICON_ICO.exists():
        print(f"Converting {ICON_ICO} to .icns via sips...")
        try:
            # sips can convert to .icns via iconutil pipeline
            # First convert to png, then build iconset, then iconutil
            _convert_ico_to_icns()
            if ICON_ICNS.exists():
                print(f"  Created {ICON_ICNS}")
                return
        except Exception as e:
            print(f"  sips conversion failed: {e}")

    # Fallback: generate a placeholder using Pillow
    print("Generating placeholder .icns icon...")
    _generate_placeholder_icns()


def _convert_ico_to_icns():
    """Convert icon.ico to icon.icns using macOS tools."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        png_path = tmpdir / "icon_512.png"

        # Convert .ico to PNG using sips
        subprocess.run(
            ["sips", "-s", "format", "png", "-z", "512", "512",
             str(ICON_ICO), "--out", str(png_path)],
            check=True, capture_output=True,
        )

        # Create iconset directory with required sizes
        iconset = tmpdir / "icon.iconset"
        iconset.mkdir()

        sizes = [16, 32, 64, 128, 256, 512]
        for size in sizes:
            out = iconset / f"icon_{size}x{size}.png"
            subprocess.run(
                ["sips", "-z", str(size), str(size),
                 str(png_path), "--out", str(out)],
                check=True, capture_output=True,
            )
            # @2x variant (retina)
            if size <= 256:
                out_2x = iconset / f"icon_{size}x{size}@2x.png"
                retina_size = size * 2
                subprocess.run(
                    ["sips", "-z", str(retina_size), str(retina_size),
                     str(png_path), "--out", str(out_2x)],
                    check=True, capture_output=True,
                )

        # Convert iconset to icns
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(ICON_ICNS)],
            check=True, capture_output=True,
        )


def _generate_placeholder_icns():
    """Generate a simple placeholder .icns using Pillow and iconutil."""
    import tempfile

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit(
            "Pillow is required to generate a placeholder icon.\n"
            "Install it with: pip install Pillow"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create a 512x512 icon image
        img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Dark rounded rectangle background
        draw.rounded_rectangle(
            [(20, 20), (492, 492)], radius=80, fill=(10, 14, 23, 255)
        )
        # Green dollar sign / chart line motif
        draw.text(
            (180, 100), "SPX", fill=(0, 200, 120, 255),
        )
        draw.ellipse([(180, 200), (332, 352)], outline=(0, 200, 120, 255), width=12)

        iconset = tmpdir / "icon.iconset"
        iconset.mkdir()

        sizes = [16, 32, 64, 128, 256, 512]
        for size in sizes:
            resized = img.resize((size, size), Image.LANCZOS)
            resized.save(iconset / f"icon_{size}x{size}.png")
            if size <= 256:
                retina = img.resize((size * 2, size * 2), Image.LANCZOS)
                retina.save(iconset / f"icon_{size}x{size}@2x.png")

        # Use iconutil to create .icns
        result = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(ICON_ICNS)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  iconutil failed: {result.stderr}")
            print("  Creating minimal .icns from PNG fallback...")
            # Last resort: save a single PNG as the icon reference
            img.save(ICON_ICNS.with_suffix(".png"))
            print(f"  Saved PNG icon to {ICON_ICNS.with_suffix('.png')}")
            print("  Convert manually: iconutil -c icns icon.iconset -o assets/icon.icns")
        else:
            print(f"  Created {ICON_ICNS}")


# ---------------------------------------------------------------------------
# Setup.py generation and build
# ---------------------------------------------------------------------------


def _write_setup_py() -> Path:
    """Generate a temporary setup.py for py2app."""
    setup_py = PROJECT_ROOT / "setup.py"

    # Format data_files for the setup.py
    data_files_repr = "[\n"
    for dest, files in DATA_FILES:
        data_files_repr += f"    ({dest!r}, {files!r}),\n"
    data_files_repr += "]"

    content = f'''"""Auto-generated setup.py for py2app. Do not edit manually."""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})

from setuptools import setup

APP = [{str(ENTRY_POINT)!r}]
DATA_FILES = {data_files_repr}
OPTIONS = {PY2APP_OPTIONS!r}

setup(
    name={APP_NAME!r},
    app=APP,
    data_files=DATA_FILES,
    options={{"py2app": OPTIONS}},
    setup_requires=["py2app"],
)
'''
    setup_py.write_text(content)
    return setup_py


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def clean():
    """Remove previous build artifacts."""
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            print(f"Removing {d}")
            shutil.rmtree(d)
    setup_py = PROJECT_ROOT / "setup.py"
    if setup_py.exists():
        setup_py.unlink()


def build(debug: bool = False):
    """Run py2app build."""
    if not ENTRY_POINT.exists():
        sys.exit(f"Entry point not found: {ENTRY_POINT}")

    _ensure_icon()

    setup_py = _write_setup_py()

    try:
        mode = "py2app"
        cmd = [sys.executable, str(setup_py), mode]

        if debug:
            cmd.extend(["-A"])  # alias mode: symlinks instead of copying
            print("Building in ALIAS mode (for development/debugging)...")
        else:
            print("Building standalone .app bundle...")

        print(f"  Command: {' '.join(cmd)}\n")
        result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        if result.returncode != 0:
            sys.exit(f"py2app failed with exit code {result.returncode}")
    finally:
        # Clean up generated setup.py
        if setup_py.exists():
            setup_py.unlink()

    app_path = DIST_DIR / f"{APP_NAME}.app"
    if not app_path.exists():
        # py2app may use different naming
        candidates = list(DIST_DIR.glob("*.app"))
        if candidates:
            app_path = candidates[0]

    print(f"\nBuild complete: {app_path}")


def verify():
    """Verify the .app bundle contains required files."""
    app_bundle = None
    for candidate in [
        DIST_DIR / f"{APP_NAME}.app",
        DIST_DIR / "SPXIncomeTrader.app",
    ]:
        if candidate.exists():
            app_bundle = candidate
            break

    if not app_bundle:
        candidates = list(DIST_DIR.glob("*.app"))
        if candidates:
            app_bundle = candidates[0]
        else:
            sys.exit("No .app bundle found in dist/")

    resources = app_bundle / "Contents" / "Resources"
    macos_dir = app_bundle / "Contents" / "MacOS"

    errors = []

    # Check executable exists
    executables = list(macos_dir.glob("*")) if macos_dir.exists() else []
    if not executables:
        errors.append(f"No executable found in {macos_dir}")

    # Check Info.plist
    plist = app_bundle / "Contents" / "Info.plist"
    if not plist.exists():
        errors.append("Info.plist missing")

    # Check bundled data files
    expected = [
        "dashboard/templates/index.html",
        "dashboard/templates/settings.html",
        "dashboard/templates/setup.html",
        "config/strategy_params.yaml",
    ]
    for rel in expected:
        full = resources / rel
        if not full.exists():
            errors.append(f"Missing bundled file: {rel}")

    if errors:
        print("\nVerification FAILED:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\nVerification PASSED -- all expected files present.")
        print(f"  App bundle: {app_bundle}")
        print(f"  Bundle size: {_dir_size_mb(app_bundle):.1f} MB")


def _dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build SPX Income Trader for macOS")
    parser.add_argument("--clean", action="store_true", help="Remove previous build artifacts first")
    parser.add_argument("--debug", action="store_true", help="Alias mode (symlinks, faster, for development)")
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
