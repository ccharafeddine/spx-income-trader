# Building The Daily Melt

## Windows

### Prerequisites

- Python 3.11 - 3.13 (PyWebView's pythonnet backend does not support 3.14 yet)
- All project dependencies installed
- PyInstaller

### 1. Install Dependencies

```bash
pip install -r requirements-desktop.txt
pip install pyinstaller
```

### 2. Build the Executable

From the project root:

```bash
python build/build_windows.py
```

Options:

| Flag | Description |
|---|---|
| `--clean` | Remove previous build artifacts before building |
| `--debug` | Keep the console window visible for debugging |
| `--verify-only` | Skip build, just verify an existing output |

The build produces a directory at `dist/SPXIncomeTrader/` containing `SPXIncomeTrader.exe` and all dependencies.

#### What gets bundled

- `app_desktop.py` as the entry point
- `dashboard/templates/*.html` (Flask templates)
- `config/strategy_params.yaml` (default strategy config)
- `assets/icon.ico` (application icon)
- Hidden imports: `keyring.backends.Windows`, `webview`, `engineio.async_drivers.threading`

### 3. Test the Build

Run the executable directly:

```bash
dist\SPXIncomeTrader\SPXIncomeTrader.exe
```

Or with debug console:

```bash
dist\SPXIncomeTrader\SPXIncomeTrader.exe --dev
```

### 4. Create a Windows Installer (Optional)

Requires [Inno Setup 6+](https://jrsoftware.org/isinfo.php).

#### Using the GUI

1. Open Inno Setup Compiler
2. File > Open > `build/installer.iss`
3. Build > Compile

#### Using the command line

```bash
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss
```

The installer is written to `build/Output/SPXIncomeTrader_Setup.exe`.

#### What the installer provides

- Installs to `C:\Users\<user>\AppData\Local\Programs\SPX Income Trader`
- Start Menu shortcut
- Optional Desktop shortcut (unchecked by default)
- Uninstaller entry in Add/Remove Programs
- Option to launch after install

---

## Linux

### Prerequisites

- Linux (Ubuntu 20.04+, Debian 11+, Fedora 36+, or similar)
- Python 3.11 - 3.13
- All project dependencies installed
- PyInstaller
- System packages for pywebview (GTK + WebKit)

### 1. Install System Dependencies

pywebview uses GTK and WebKit2 on Linux. pystray needs an appindicator library for the system tray.

**Ubuntu / Debian:**

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
sudo apt install libayatana-appindicator3-dev   # for system tray
```

**Fedora:**

```bash
sudo dnf install python3-gobject python3-cairo gtk3 webkit2gtk4.1
sudo dnf install libappindicator-gtk3-devel
```

The build script checks for these and prints the install command if anything is missing.

### 2. Install Python Dependencies

```bash
pip install -r requirements-desktop.txt
pip install pyinstaller
```

### 3. Build the Executable

From the project root:

```bash
python build/build_linux.py
```

Options:

| Flag | Description |
|---|---|
| `--clean` | Remove previous build artifacts before building |
| `--debug` | Keep the console window visible for debugging |
| `--verify-only` | Skip build, just verify an existing output |
| `--appimage` | Package as AppImage after building |
| `--appimage-only` | Only run AppImage packaging (skip build) |

The build produces a directory at `dist/SPXIncomeTrader/` containing `The Daily Melt` executable and all dependencies.

#### What gets bundled

- `app_desktop.py` as the entry point
- `dashboard/templates/*.html` (Flask templates)
- `config/strategy_params.yaml` (default strategy config)
- `assets/icon.png` (application icon)
- Hidden imports: `keyring.backends.SecretService`, `webview`, `engineio.async_drivers.threading`

### 4. Test the Build

```bash
./dist/SPXIncomeTrader/The\ Daily\ Melt
```

Or with debug console:

```bash
./dist/SPXIncomeTrader/The\ Daily\ Melt --dev
```

### 5. Create an AppImage (Optional, Recommended for Distribution)

AppImage is a portable format that runs on most Linux distributions without installation.

```bash
# Build + package in one step
python build/build_linux.py --clean --appimage

# Or package an existing build
python build/build_linux.py --appimage-only
```

This requires [appimagetool](https://github.com/AppImage/AppImageKit/releases) on your PATH. If it's not found, the script creates the AppDir structure and prints instructions.

The output is `dist/The Daily Melt-1.0.0-x86_64.AppImage`, a single self-contained file.

```bash
chmod +x dist/The\ Daily\ Melt-1.0.0-x86_64.AppImage
./dist/The\ Daily\ Melt-1.0.0-x86_64.AppImage
```

---

## macOS

### Prerequisites

- macOS 10.15 (Catalina) or later
- Python 3.11 - 3.13
- All project dependencies installed
- py2app (`pip install py2app`)

### 1. Install Dependencies

```bash
pip install -r requirements-desktop.txt
pip install py2app
```

### 2. Build the .app Bundle

From the project root:

```bash
python build/build_macos.py
```

Options:

| Flag | Description |
|---|---|
| `--clean` | Remove previous build artifacts before building |
| `--debug` | Alias mode (symlinks to source, fast, for development) |
| `--verify-only` | Skip build, just verify an existing output |

The build produces `dist/SPX Income Trader.app`.

#### What gets bundled

- `app_desktop.py` as the entry point
- `dashboard/templates/*.html` (Flask templates)
- `config/strategy_params.yaml` (default strategy config)
- `assets/icon.icns` (application icon, auto-generated from icon.ico if missing)
- Hidden imports: `keyring.backends.macOS`, `webview`, `engineio.async_drivers.threading`

#### Info.plist entries

The build configures these Info.plist keys:

| Key | Value | Purpose |
|---|---|---|
| `CFBundleIdentifier` | `com.spxincometrader.app` | Unique app identifier |
| `LSUIElement` | `false` | Set to `true` to make the app menu-bar only (no Dock icon) |
| `NSHighResolutionCapable` | `true` | Retina/HiDPI display support |
| `LSMinimumSystemVersion` | `10.15` | Minimum macOS version |
| `LSApplicationCategoryType` | `public.app-category.finance` | App Store category |

To make the app menu-bar only (no Dock icon, tray-only), edit `LSUIElement` to `true` in `build/build_macos.py` before building.

### 3. Test the Build

```bash
open dist/SPX\ Income\ Trader.app
```

Or run from the terminal to see console output:

```bash
dist/SPX\ Income\ Trader.app/Contents/MacOS/SPXIncomeTrader
```

### 4. Create a DMG Installer (Optional)

```bash
bash build/create_dmg.sh
```

Options:

| Flag | Description |
|---|---|
| `--no-layout` | Skip Finder window layout (faster build) |

This creates `dist/SPXIncomeTrader.dmg` with a drag-to-Applications layout.

---

## Project Structure

```
build/
    build_windows.py   # PyInstaller build script (Windows)
    build_linux.py     # PyInstaller build script (Linux)
    build_macos.py     # py2app build script (macOS)
    installer.iss      # Inno Setup installer template (Windows)
    create_dmg.sh      # DMG creation script (macOS)
    README.md          # This file
    _pyinstaller/      # (generated) Windows/Linux build artifacts
    _py2app/           # (generated) macOS build artifacts
    Output/            # (generated) Windows installer output
assets/
    icon.ico           # Windows application icon
    icon.png           # Application icon (used by Linux build and dashboard)
    icon.icns          # macOS application icon (generated at build time if missing)
dist/
    The Daily Melt/    # (generated) Windows PyInstaller output
    SPXIncomeTrader/   # (generated) Linux PyInstaller output
    SPX Income Trader.app  # (generated) macOS app bundle
    SPXIncomeTrader.dmg    # (generated) macOS disk image
    *.AppImage         # (generated) Linux AppImage
```

## Troubleshooting

### Windows

**Missing module errors at runtime**: Add the module to the `HIDDEN_IMPORTS` list in `build_windows.py` and rebuild.

**Antivirus false positives**: PyInstaller executables are sometimes flagged. You can sign the executable with a code signing certificate to reduce this.

**App can't find templates/config**: The build script bundles data files into `_internal/`. The app's `app_paths.py` uses `sys._MEIPASS` to locate these at runtime.

### macOS

**"App is damaged and can't be opened"**: The app is unsigned. Either ad-hoc sign it (`codesign --force --deep --sign -`) or right-click > Open to bypass Gatekeeper.

**Missing module errors at runtime**: Add the module to the `packages` or `includes` list in `PY2APP_OPTIONS` in `build_macos.py` and rebuild.

**py2app can't find modules**: Try adding the problematic module to the `packages` list (for whole packages) or `includes` list (for specific modules).

**App launches but shows blank window**: Check console output by running the binary directly from `Contents/MacOS/`. Missing templates or static files will show Flask errors.

**icon.icns not created**: The build script tries to generate it from `icon.ico` using `sips` and `iconutil` (macOS built-in tools). If this fails, create the icon manually using an online converter or [Image2icon](https://img2icnsapp.com/).

**DMG layout not applied**: The Finder layout uses AppleScript which can be flaky. Use `--no-layout` to skip it, or open the DMG and arrange manually before converting to read-only.

### Linux

**`gi` module not found**: Install the GTK Python bindings: `sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0`.

**WebKit2 not found / pywebview shows blank window**: Install the WebKit2 GObject introspection data: `sudo apt install gir1.2-webkit2-4.1`. On older systems you may need `gir1.2-webkit2-4.0` instead (and may need to adjust pywebview's backend).

**System tray icon not visible**: pystray needs an appindicator library. Install `libayatana-appindicator3-dev` (preferred) or `libappindicator3-dev`. On GNOME, you may also need the [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).

**Wayland issues**: pywebview works best under X11. If you're on Wayland and the window doesn't render, try launching with `GDK_BACKEND=x11` set in the environment: `GDK_BACKEND=x11 ./dist/SPXIncomeTrader/The\ Daily\ Melt`.

**Missing module errors at runtime**: Add the module to the `HIDDEN_IMPORTS` list in `build_linux.py` and rebuild.

**App can't find templates/config**: The build script bundles data files into `_internal/`. The app's `app_paths.py` uses `sys._MEIPASS` to locate these at runtime.

**AppImage won't run**: Make sure it's executable (`chmod +x *.AppImage`). On some systems you also need FUSE: `sudo apt install fuse libfuse2`.
