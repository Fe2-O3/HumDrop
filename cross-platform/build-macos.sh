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
echo ""
echo "Done! App is at: dist/HumDrop.app"
