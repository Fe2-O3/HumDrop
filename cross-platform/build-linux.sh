#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "Building HumDrop for Linux..."
pip install -r requirements.txt pyinstaller 2>/dev/null
pyinstaller --onefile --windowed \
    --name "HumDrop" \
    humdrop.py
echo ""
echo "Done! Executable is at: dist/HumDrop"
