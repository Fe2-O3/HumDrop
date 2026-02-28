#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "Building HumDrop for Linux..."

# Generate icons if missing (for .desktop file usage)
if [ ! -f HumDrop.ico ]; then
    echo "Generating app icons..."
    pip install Pillow 2>/dev/null
    python generate_icons.py
fi

pip install -r requirements.txt pyinstaller 2>/dev/null
pyinstaller --onefile --windowed \
    --name "HumDrop" \
    humdrop.py
echo ""
echo "Done! Executable is at: dist/HumDrop"
