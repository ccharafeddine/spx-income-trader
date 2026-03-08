"""
Build script for SPX Income Trader macOS distribution.

Creates a py2app .app bundle from app_desktop.py with all
required data files (templates, config, icon) and proper Info.plist.

Automatically creates an isolated virtual environment for the build
to avoid conflicts with Anaconda or other bloated site-packages.

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
VENV_DIR = PROJECT_ROOT / "build" / "_venv"           # isolated build venv
ENTRY_POINT = PROJECT_ROOT / "app_desktop.py"
ICON_ICNS = PROJECT_ROOT / "assets" / "icon.icns"
ICON_ICO = PROJECT_ROOT / "assets" / "icon.ico"
ICON_PNG = PROJECT_ROOT / "assets" / "icon.png"
APP_NAME = "The Daily Melt"
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
        str(PROJECT_ROOT / "config" / "economic_calendar.json"),
    ]),
    ("assets", [
        str(ICON_PNG),
    ]),
]

# If dashboard/static exists, include everything in it
STATIC_DIR = PROJECT_ROOT / "dashboard" / "static"
if STATIC_DIR.is_dir():
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
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundleExecutable": APP_NAME.replace(" ", ""),
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "LSUIElement": False,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "10.15",
        "LSApplicationCategoryType": "public.app-category.finance",
        "NSAppleEventsUsageDescription": "SPX Income Trader needs automation access.",
    },
    "packages": [
        # Project packages
        "src",
        "src.brokers",
        "src.core",
        "src.data",
        "src.models",
        "src.utils",
        "src.analytics",
        "src.backtest",
        "src.demo",
        "src.shadow",
        "dashboard",
        "config",
        "database",
        # Third-party
        "flask",
        "werkzeug",
        "jinja2",
        "sqlalchemy",
        "keyring",
        "yaml",
        "engineio",
        "socketio",
        "requests",
        "requests_oauthlib",
        "platformdirs",
        "dotenv",
        "pytz",
        "yfinance",
        "pystray",
        "PIL",
        "plotly",
        "reportlab",
        "pandas",
        "numpy",
        # Schwab broker
        "schwab",
        "authlib",
        "httpx",
    ],
    "includes": [
        "keyring.backends",
        "keyring.backends.macOS",
        "keyring.backends.null",
        "webview",
        "engineio.async_drivers.threading",
        "jinja2.ext",
        "sqlalchemy.dialects.sqlite",
        "werkzeug.serving",
        "werkzeug.middleware",
    ],
    "excludes": [
        "pytest", "pytest_cov", "_pytest",
        "black", "flake8",
        "tkinter",
        "matplotlib",
        "scipy",
        "test", "tests",
    ],
    "resources": [],
    "site_packages": True,
    "strip": True,
    "optimize": 2,
}

# ---------------------------------------------------------------------------
# Virtual environment for isolated builds
# ---------------------------------------------------------------------------


def _get_native_arch() -> str:
    """Return the native CPU architecture (arm64 or x86_64).

    Uses hw.optional.arm64 because hw.machine lies under Rosetta.
    """
    result = subprocess.run(
        ["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip() == "1":
        return "arm64"
    return "x86_64"


def _find_system_python() -> tuple:
    """Find a suitable Python >=3.10 interpreter for the build venv.

    Returns (python_path, needs_arch_prefix) where needs_arch_prefix is True
    if we need to use 'arch -arm64' to force the correct architecture.
    """
    native_arch = _get_native_arch()

    # Candidates in preference order: framework Python, Homebrew, system
    candidates = [
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
        "/opt/homebrew/bin/python3",     # Homebrew Apple Silicon
        "/usr/local/bin/python3",        # Homebrew Intel
        "/usr/bin/python3",              # macOS system Python
    ]
    for c in candidates:
        if not Path(c).exists():
            continue
        try:
            # Check version >= 3.10
            result = subprocess.run(
                [c, "-c", "import sys; print(sys.version_info[:2])"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                continue
            version_tuple = eval(result.stdout.strip())
            if version_tuple < (3, 10):
                continue

            # Check architecture
            arch_result = subprocess.run(
                [c, "-c", "import struct; print(struct.calcsize('P') * 8)"],
                capture_output=True, text=True, timeout=5,
            )
            file_result = subprocess.run(
                ["file", c], capture_output=True, text=True, timeout=5,
            )
            file_info = file_result.stdout

            # Determine if we need arch prefix
            needs_arch = False
            if native_arch == "arm64" and "universal" in file_info:
                # Universal binary running under Rosetta — force arm64
                needs_arch = True
            elif native_arch == "arm64" and "arm64" in file_info:
                needs_arch = False  # already native
            elif native_arch == "arm64" and "x86_64" in file_info and "arm64" not in file_info:
                continue  # x86_64-only, skip

            ver = f"Python {version_tuple[0]}.{version_tuple[1]}"
            arch_note = f" (will force {native_arch})" if needs_arch else ""
            print(f"  Found: {c} ({ver}){arch_note}")
            return c, needs_arch
        except Exception:
            continue

    print("  Warning: using current python3 (may be Anaconda)")
    return sys.executable, False


def _run_in_venv(cmd, **kwargs):
    """Run a command, prefixing with 'arch -arm64' if needed on Apple Silicon."""
    if _RUN_WITH_ARCH:
        native = _get_native_arch()
        cmd = ["arch", f"-{native}"] + list(cmd)
    return subprocess.run(cmd, **kwargs)


# Module-level flag set during venv creation
_RUN_WITH_ARCH = False


def _ensure_venv() -> Path:
    """Create (or reuse) an isolated venv with only build dependencies."""
    global _RUN_WITH_ARCH

    venv_python = VENV_DIR / "bin" / "python"

    if venv_python.exists():
        # Verify venv python matches native arch
        native = _get_native_arch()
        result = subprocess.run(
            ["arch", f"-{native}", str(venv_python), "-c",
             "import platform; print(platform.machine())"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and native in result.stdout:
            print(f"Reusing build venv at {VENV_DIR}")
            return venv_python
        else:
            print(f"Venv arch mismatch, recreating...")
            shutil.rmtree(VENV_DIR)

    print("Creating isolated build virtual environment...")
    base_python, needs_arch = _find_system_python()
    _RUN_WITH_ARCH = needs_arch

    _run_in_venv(
        [base_python, "-m", "venv", str(VENV_DIR)],
        check=True,
    )

    venv_pip = str(VENV_DIR / "bin" / "pip")

    # Upgrade pip
    _run_in_venv(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
        check=True, capture_output=True,
    )

    # Install py2app and project dependencies
    # Use --no-cache-dir to avoid stale x86_64 wheels from Anaconda's cache
    print("Installing build dependencies in venv...")
    _run_in_venv(
        [venv_pip, "install", "--no-cache-dir", "py2app", "setuptools"],
        check=True,
    )

    # Install runtime requirements
    req_desktop = PROJECT_ROOT / "requirements-desktop.txt"
    req_base = PROJECT_ROOT / "requirements.txt"
    req_file = req_desktop if req_desktop.exists() else req_base
    print(f"Installing runtime dependencies from {req_file.name}...")
    _run_in_venv(
        [venv_pip, "install", "--no-cache-dir", "-r", str(req_file)],
        check=True,
    )

    print(f"Build venv ready at {VENV_DIR}\n")
    return venv_python


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

        subprocess.run(
            ["sips", "-s", "format", "png", "-z", "512", "512",
             str(ICON_ICO), "--out", str(png_path)],
            check=True, capture_output=True,
        )

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
            if size <= 512:
                out_2x = iconset / f"icon_{size}x{size}@2x.png"
                retina_size = size * 2
                subprocess.run(
                    ["sips", "-z", str(retina_size), str(retina_size),
                     str(png_path), "--out", str(out_2x)],
                    check=True, capture_output=True,
                )

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

        img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [(20, 20), (492, 492)], radius=80, fill=(10, 14, 23, 255)
        )
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
            if size <= 512:
                retina = img.resize((size * 2, size * 2), Image.LANCZOS)
                retina.save(iconset / f"icon_{size}x{size}@2x.png")

        result = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(ICON_ICNS)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  iconutil failed: {result.stderr}")
            print("  Creating minimal .icns from PNG fallback...")
            img.save(ICON_ICNS.with_suffix(".png"))
            print(f"  Saved PNG icon to {ICON_ICNS.with_suffix('.png')}")
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
sys.setrecursionlimit(5000)
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
    """Remove previous macOS build artifacts."""
    app_bundle = DIST_DIR / f"{APP_NAME}.app"
    for d in [app_bundle, BUILD_DIR, VENV_DIR]:
        if d.exists():
            print(f"Removing {d}")
            shutil.rmtree(d)
    setup_py = PROJECT_ROOT / "setup.py"
    if setup_py.exists():
        setup_py.unlink()


def build(debug: bool = False):
    """Run py2app build using an isolated venv."""
    if not ENTRY_POINT.exists():
        sys.exit(f"Entry point not found: {ENTRY_POINT}")

    _ensure_icon()

    venv_python = _ensure_venv()
    setup_py = _write_setup_py()

    try:
        mode = "py2app"
        cmd = [str(venv_python), str(setup_py), mode]

        if debug:
            cmd.extend(["-A"])
            print("Building in ALIAS mode (for development/debugging)...")
        else:
            print("Building standalone .app bundle...")

        print(f"  Command: {' '.join(cmd)}\n")
        result = _run_in_venv(cmd, cwd=str(PROJECT_ROOT))
        if result.returncode != 0:
            sys.exit(f"py2app failed with exit code {result.returncode}")
    finally:
        if setup_py.exists():
            setup_py.unlink()

    app_path = DIST_DIR / f"{APP_NAME}.app"
    if not app_path.exists():
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

    executables = list(macos_dir.glob("*")) if macos_dir.exists() else []
    if not executables:
        errors.append(f"No executable found in {macos_dir}")

    plist = app_bundle / "Contents" / "Info.plist"
    if not plist.exists():
        errors.append("Info.plist missing")

    expected = [
        "dashboard/templates/index.html",
        "dashboard/templates/settings.html",
        "dashboard/templates/setup.html",
        "config/strategy_params.yaml",
        "config/economic_calendar.json",
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
