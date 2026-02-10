@echo off
REM Create installer for SPX Income Trader
REM
REM Tries Inno Setup first (if ISCC.exe is on PATH or in default location),
REM then falls back to creating a ZIP archive via PowerShell.
REM
REM Prerequisites:
REM   1. Run: python build/build_windows.py --clean
REM   2. Then run this script from the project root: build\create_installer.bat

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."
set "DIST_DIR=%PROJECT_ROOT%\dist"
set "APP_NAME=SPX Income Trader"
set "APP_DIR=%DIST_DIR%\%APP_NAME%"
set "ISS_FILE=%SCRIPT_DIR%installer.iss"
set "VERSION=1.0.0"

echo ============================================================
echo  SPX Income Trader - Installer Builder
echo ============================================================
echo.

REM Verify the build exists
if not exist "%APP_DIR%\%APP_NAME%.exe" (
    echo ERROR: Build not found at "%APP_DIR%"
    echo        Run 'python build/build_windows.py --clean' first.
    exit /b 1
)

echo Build found: %APP_DIR%
echo.

REM ---------------------------------------------------------------------------
REM Try Inno Setup
REM ---------------------------------------------------------------------------

set "ISCC="

REM Check PATH first
where iscc >nul 2>&1
if %errorlevel% equ 0 (
    set "ISCC=iscc"
    goto :found_iscc
)

REM Check default install locations
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"
) do (
    if exist %%P (
        set "ISCC=%%~P"
        goto :found_iscc
    )
)

goto :no_iscc

:found_iscc
echo Found Inno Setup: %ISCC%
echo Compiling installer...
echo.
"%ISCC%" "%ISS_FILE%"
if %errorlevel% equ 0 (
    echo.
    echo Installer created: %SCRIPT_DIR%Output\SPXIncomeTrader_Setup.exe
    goto :done
) else (
    echo.
    echo Inno Setup compilation failed. Falling back to ZIP...
    goto :make_zip
)

:no_iscc
echo Inno Setup not found. Creating ZIP archive instead...

:make_zip
set "ZIP_FILE=%DIST_DIR%\SPXIncomeTrader_v%VERSION%.zip"
if exist "%ZIP_FILE%" del "%ZIP_FILE%"

echo Compressing to %ZIP_FILE% ...
powershell -NoProfile -Command "Compress-Archive -Path '%APP_DIR%' -DestinationPath '%ZIP_FILE%' -Force"
if %errorlevel% equ 0 (
    echo.
    echo ZIP archive created: %ZIP_FILE%
) else (
    echo.
    echo ERROR: Failed to create ZIP archive.
    exit /b 1
)

:done
echo.
echo Done.
endlocal
