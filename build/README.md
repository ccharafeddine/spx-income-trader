# Building SPX Income Trader for Windows

## Prerequisites

- Python 3.11 - 3.13 (PyWebView's pythonnet backend does not support 3.14 yet)
- All project dependencies installed
- PyInstaller

## 1. Install Dependencies

```bash
pip install -r requirements-desktop.txt
pip install pyinstaller
```

## 2. Build the Executable

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

### What gets bundled

- `app_desktop.py` as the entry point
- `dashboard/templates/*.html` (Flask templates)
- `config/strategy_params.yaml` (default strategy config)
- `assets/icon.ico` (application icon)
- Hidden imports: `keyring.backends.Windows`, `webview`, `engineio.async_drivers.threading`

## 3. Test the Build

Run the executable directly:

```bash
dist\SPXIncomeTrader\SPXIncomeTrader.exe
```

Or with debug console:

```bash
dist\SPXIncomeTrader\SPXIncomeTrader.exe --dev
```

## 4. Create a Windows Installer (Optional)

Requires [Inno Setup 6+](https://jrsoftware.org/isinfo.php).

### Using the GUI

1. Open Inno Setup Compiler
2. File > Open > `build/installer.iss`
3. Build > Compile

### Using the command line

```bash
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss
```

The installer is written to `build/Output/SPXIncomeTrader_Setup.exe`.

### What the installer provides

- Installs to `C:\Users\<user>\AppData\Local\Programs\SPX Income Trader`
- Start Menu shortcut
- Optional Desktop shortcut (unchecked by default)
- Uninstaller entry in Add/Remove Programs
- Option to launch after install

## Project Structure

```
build/
    build_windows.py   # PyInstaller build script
    installer.iss      # Inno Setup installer template
    README.md          # This file
    _pyinstaller/      # (generated) temporary build artifacts
    Output/            # (generated) installer output
assets/
    icon.ico           # Application icon
dist/
    SPXIncomeTrader/   # (generated) PyInstaller output
```

## Troubleshooting

**Missing module errors at runtime**: Add the module to the `HIDDEN_IMPORTS` list in `build_windows.py` and rebuild.

**Antivirus false positives**: PyInstaller executables are sometimes flagged. You can sign the executable with a code signing certificate to reduce this.

**App can't find templates/config**: The build script bundles data files into `_internal/`. The app's `app_paths.py` uses `sys._MEIPASS` to locate these at runtime.
