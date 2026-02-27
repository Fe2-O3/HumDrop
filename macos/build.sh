#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="HumDrop"
BUNDLE_NAME="HumDrop"
BUILD_DIR="$SCRIPT_DIR/build"
APP_DIR="$BUILD_DIR/${APP_NAME}.app"

echo "Building ${APP_NAME}..."

# Clean previous build
rm -rf "$BUILD_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# Compile Swift source
# Target macOS 10.15+ for broad compatibility
swiftc \
    -O \
    -target arm64-apple-macos10.15 \
    -o "$APP_DIR/Contents/MacOS/$BUNDLE_NAME" \
    "$SCRIPT_DIR/HumDrop.swift"

# Copy Info.plist
cp "$SCRIPT_DIR/Info.plist" "$APP_DIR/Contents/"

echo ""
echo "Build complete!"
echo "App: $APP_DIR"
echo ""
echo "To run:"
echo "  open \"$APP_DIR\""
echo ""
echo "To install (optional):"
echo "  cp -R \"$APP_DIR\" /Applications/"
