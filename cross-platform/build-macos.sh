#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "Building HumDrop for macOS..."

# Generate icons if missing
if [ ! -f HumDrop.icns ]; then
    echo "Generating app icons..."
    pip install Pillow 2>/dev/null
    python generate_icons.py
fi

pip install -r requirements.txt pyinstaller 2>/dev/null
pyinstaller --onedir --windowed \
    --name "HumDrop" \
    --icon "HumDrop.icns" \
    --osx-bundle-identifier "com.fe2o3.humdrop" \
    --add-data "$(python -c 'import customtkinter; import os; print(os.path.dirname(customtkinter.__file__))'):customtkinter" \
    humdrop.py

# Add macOS folder permission descriptions to Info.plist
PLIST="dist/HumDrop.app/Contents/Info.plist"
if [ -f "$PLIST" ]; then
    /usr/libexec/PlistBuddy -c "Add :NSDesktopFolderUsageDescription string 'HumDrop saves downloaded camera files to your Desktop folder.'" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :NSDocumentsFolderUsageDescription string 'HumDrop saves downloaded camera files to your Documents folder.'" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :NSDownloadsFolderUsageDescription string 'HumDrop saves downloaded camera files to your Downloads folder.'" "$PLIST" 2>/dev/null || true
    echo "Added folder permission descriptions to Info.plist"
fi

echo ""
echo "Done! App is at: dist/HumDrop.app"
