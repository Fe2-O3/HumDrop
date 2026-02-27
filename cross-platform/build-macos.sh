#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "Building HumDrop for macOS..."
pip install -r requirements.txt pyinstaller 2>/dev/null
pyinstaller --onedir --windowed \
    --name "HumDrop" \
    --osx-bundle-identifier "com.fe2o3.humdrop" \
    humdrop.py
echo ""
echo "Done! App is at: dist/HumDrop.app"
