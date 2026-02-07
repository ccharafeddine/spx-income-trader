#!/usr/bin/env bash
#
# Create a distributable .dmg from the SPX Income Trader .app bundle.
#
# Usage:
#     bash build/create_dmg.sh                    # Standard DMG
#     bash build/create_dmg.sh --no-layout        # Skip Finder layout (faster)
#
# Prerequisites:
#     - A completed py2app build (dist/*.app must exist)
#     - macOS with hdiutil and osascript available
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DIST_DIR="$PROJECT_ROOT/dist"

APP_NAME="SPX Income Trader"
DMG_NAME="SPXIncomeTrader"
DMG_VOLUME_NAME="$APP_NAME"
DMG_FINAL="$DIST_DIR/${DMG_NAME}.dmg"
DMG_TEMP="$DIST_DIR/${DMG_NAME}_temp.dmg"

# Find the .app bundle
APP_BUNDLE=""
for candidate in "$DIST_DIR/$APP_NAME.app" "$DIST_DIR/SPXIncomeTrader.app"; do
    if [ -d "$candidate" ]; then
        APP_BUNDLE="$candidate"
        break
    fi
done

if [ -z "$APP_BUNDLE" ]; then
    # Try any .app in dist/
    APP_BUNDLE="$(find "$DIST_DIR" -maxdepth 1 -name '*.app' -print -quit 2>/dev/null || true)"
fi

if [ -z "$APP_BUNDLE" ] || [ ! -d "$APP_BUNDLE" ]; then
    echo "ERROR: No .app bundle found in $DIST_DIR"
    echo "       Run 'python build/build_macos.py' first."
    exit 1
fi

echo "App bundle: $APP_BUNDLE"

# Parse arguments
SKIP_LAYOUT=false
for arg in "$@"; do
    case "$arg" in
        --no-layout) SKIP_LAYOUT=true ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Clean previous DMG
# ---------------------------------------------------------------------------

[ -f "$DMG_FINAL" ] && rm -f "$DMG_FINAL"
[ -f "$DMG_TEMP" ] && rm -f "$DMG_TEMP"

# ---------------------------------------------------------------------------
# Create temporary DMG
# ---------------------------------------------------------------------------

echo ""
echo "Creating temporary DMG..."

# Calculate size needed (app size + 20MB buffer)
APP_SIZE_KB=$(du -sk "$APP_BUNDLE" | cut -f1)
DMG_SIZE_KB=$((APP_SIZE_KB + 20480))

hdiutil create \
    -srcfolder "$APP_BUNDLE" \
    -volname "$DMG_VOLUME_NAME" \
    -fs HFS+ \
    -fsargs "-c c=64,a=16,e=16" \
    -format UDRW \
    -size "${DMG_SIZE_KB}k" \
    "$DMG_TEMP"

# ---------------------------------------------------------------------------
# Mount and configure layout
# ---------------------------------------------------------------------------

echo ""
echo "Mounting DMG..."

MOUNT_POINT=$(hdiutil attach -readwrite -noverify -noautoopen "$DMG_TEMP" | \
    grep -E '^\S+\s+Apple_HFS' | awk '{print $3}')

if [ -z "$MOUNT_POINT" ]; then
    echo "ERROR: Failed to mount DMG"
    exit 1
fi

echo "Mounted at: $MOUNT_POINT"

# Add Applications symlink for drag-to-install
ln -sf /Applications "$MOUNT_POINT/Applications"

if [ "$SKIP_LAYOUT" = false ]; then
    echo "Configuring Finder layout..."

    # Use AppleScript to set the Finder window appearance
    osascript <<APPLESCRIPT
tell application "Finder"
    tell disk "$DMG_VOLUME_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set bounds of container window to {200, 120, 760, 500}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 96

        -- Position the app icon on the left, Applications on the right
        set position of item "$(basename "$APP_BUNDLE")" of container window to {140, 200}
        set position of item "Applications" of container window to {420, 200}

        close
        open
        update without registering applications
        delay 2
        close
    end tell
end tell
APPLESCRIPT

    # Give Finder time to write .DS_Store
    sync
    sleep 2
fi

# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------

echo ""
echo "Unmounting..."
hdiutil detach "$MOUNT_POINT" -quiet

echo "Compressing DMG..."
hdiutil convert "$DMG_TEMP" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$DMG_FINAL"

rm -f "$DMG_TEMP"

# Show result
DMG_SIZE=$(du -sh "$DMG_FINAL" | cut -f1)
echo ""
echo "DMG created successfully:"
echo "  Path: $DMG_FINAL"
echo "  Size: $DMG_SIZE"
echo ""
echo "To test: open \"$DMG_FINAL\""
