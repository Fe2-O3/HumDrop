# HumDrop

Sync videos and photos directly from WiFi-enabled trail and bird cameras.

Downloads the original files straight from the camera's SD card over your local network — no transcoding, no quality loss. Get your full 4K footage instead of the downscaled 1080p that phone apps typically provide.

## Features

- Auto-discover cameras on your network or enter IP manually
- Download videos and photos with original quality preserved
- Configurable file naming schemes with editable prefix
- Camera file timestamps preserved on downloaded files
- Size-based sync detection (won't re-download files you already have)
- Clean up or wipe camera storage after syncing

## Install

### Cross-platform (macOS, Windows, Linux)

Requires Python 3.9+.

```
cd cross-platform
pip install -r requirements.txt
python humdrop.py
```

### macOS native (Swift/AppKit)

Requires macOS 10.15+ and Xcode command line tools.

```
cd macos
bash build.sh
```

The built app appears in `build/HumDrop.app`.

## Compatible Cameras

| Camera | Status |
|--------|--------|
| [HiBird 4K Bird Camera](https://www.hibird.com) | Confirmed |

Have a camera that works with HumDrop? [Report it here.](../../issues/new?template=camera-compatibility.md)

---

If you find HumDrop useful, consider [supporting the project](https://ko-fi.com/fe2_o3).
