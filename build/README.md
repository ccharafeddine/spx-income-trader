# Building SPX Income Trader

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

### 5. Code Signing

Unsigned apps trigger Gatekeeper warnings on macOS ("app is damaged" or "unidentified developer"). For personal use this is fine - right-click > Open bypasses the warning. For distribution, sign the app:

#### Ad-hoc signing (removes Gatekeeper "damaged" warning, no Apple account needed)

```bash
codesign --force --deep --sign - "dist/SPX Income Trader.app"
```

#### Signing with a Developer ID (required for distribution outside the App Store)

1. Enroll in the [Apple Developer Program](https://developer.apple.com/programs/) ($99/year)
2. Create a "Developer ID Application" certificate in Xcode or the developer portal
3. Sign the app:

```bash
codesign --force --deep --sign "Developer ID Application: Your Name (TEAM_ID)" \
    --options runtime \
    --entitlements build/entitlements.plist \
    "dist/SPX Income Trader.app"
```

4. Sign the DMG too:

```bash
codesign --force --sign "Developer ID Application: Your Name (TEAM_ID)" \
    "dist/SPXIncomeTrader.dmg"
```

#### Verifying the signature

```bash
codesign --verify --verbose "dist/SPX Income Trader.app"
spctl --assess --verbose "dist/SPX Income Trader.app"
```

### 6. Notarization (Optional, for public distribution)

Notarization tells macOS that Apple has scanned your app and found no malware. Without it, users downloading the app from the internet will see extra Gatekeeper warnings.

1. Ensure you have signed with a Developer ID certificate (step 5 above) and `--options runtime` was used
2. Create an app-specific password at [appleid.apple.com](https://appleid.apple.com) (Security > App-Specific Passwords)
3. Store credentials in the keychain:

```bash
xcrun notarytool store-credentials "AC_PASSWORD" \
    --apple-id "your@email.com" \
    --team-id "TEAM_ID" \
    --password "app-specific-password"
```

4. Submit the DMG for notarization:

```bash
xcrun notarytool submit "dist/SPXIncomeTrader.dmg" \
    --keychain-profile "AC_PASSWORD" \
    --wait
```

5. Once approved, staple the ticket to the DMG:

```bash
xcrun stapler staple "dist/SPXIncomeTrader.dmg"
```

6. Verify:

```bash
xcrun stapler validate "dist/SPXIncomeTrader.dmg"
spctl --assess --type open --context context:primary-signature "dist/SPXIncomeTrader.dmg"
```

The full cycle (sign + notarize + staple) takes about 5-10 minutes. Apple's notarization service runs automated security checks and typically approves within 2-3 minutes.

---

## Project Structure

```
build/
    build_windows.py   # PyInstaller build script (Windows)
    build_macos.py     # py2app build script (macOS)
    installer.iss      # Inno Setup installer template (Windows)
    create_dmg.sh      # DMG creation script (macOS)
    README.md          # This file
    _pyinstaller/      # (generated) Windows build artifacts
    _py2app/           # (generated) macOS build artifacts
    Output/            # (generated) Windows installer output
assets/
    icon.ico           # Windows application icon
    icon.icns          # macOS application icon (generated at build time if missing)
dist/
    SPXIncomeTrader/   # (generated) Windows PyInstaller output
    SPX Income Trader.app  # (generated) macOS app bundle
    SPXIncomeTrader.dmg    # (generated) macOS disk image
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
