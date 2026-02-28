<p align="center">
  <img src="cross-platform/HumDrop.ico" width="128" alt="HumDrop icon">
</p>

<h1 align="center">HumDrop</h1>

<p align="center">
  <strong>Sync videos and photos directly from WiFi bird and trail cameras.</strong><br>
  Full 4K originals over your local network &mdash; no phone app, no quality loss.
</p>

<p align="center">
  <a href="https://github.com/Fe2-O3/HumDrop/releases/latest"><img src="https://img.shields.io/github/v/release/Fe2-O3/HumDrop?style=flat-square&label=Download" alt="Latest Release"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-blue?style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/python-3.9%2B-green?style=flat-square" alt="Python">
  <img src="https://img.shields.io/github/license/Fe2-O3/HumDrop?style=flat-square" alt="License">
</p>

---

## Why HumDrop?

WiFi bird cameras like the [Hibird 4K](https://hibird.com) shoot gorgeous 4K footage, but the phone app only transfers downscaled 1080p clips. HumDrop connects directly to the camera over your local network and pulls the **original full-resolution files** straight from storage.

No cloud subscription. No quality loss. No phone required.

---

## Download

Pre-built executables for every platform:

| Platform | Download | Notes |
|----------|----------|-------|
| **macOS** | [HumDrop-macOS.zip](https://github.com/Fe2-O3/HumDrop/releases/latest) | Universal (Apple Silicon + Intel). See [macOS install notes](#macos). |
| **Windows** | [HumDrop-Windows.zip](https://github.com/Fe2-O3/HumDrop/releases/latest) | Extract folder, run `HumDrop.exe`. See [Windows install notes](#windows). |
| **Linux** | [HumDrop-Linux.tar.gz](https://github.com/Fe2-O3/HumDrop/releases/latest) | Extract, run `./HumDrop`. Requires a desktop environment. |

Or run from source (any platform with Python 3.9+):

```bash
cd cross-platform
pip install -r requirements.txt
python humdrop.py
```

---

## Features

### Connect & Sync

- **Auto-discover** cameras on your network or enter IP manually
- **Download videos and photos** at full original resolution (4K MP4, full-res JPG)
- **Live speed and ETA** during downloads (MB/s, time remaining)
- **Size-based sync detection** skips files you already have
- **5-second heartbeat** monitors the connection in real time
- **Robust disconnect handling** returns to home screen, changes status indicator, and sends a system notification if the camera drops

### Organize

- **7 naming schemes** with live preview:
  - `Custom · Date · Seq` &mdash; BirdCam_2026-01-31_001.mp4
  - `Custom · Timestamp` &mdash; BIR_20260131_140144.mp4
  - `Date · Time · Custom` &mdash; 2026-01-31_14-01_BirdCam.mp4
  - `Date · Seq · Custom` &mdash; 2026-01-31_001_BirdCam.mp4
  - `Custom · Seq` &mdash; BirdCam_001.mp4
  - `Seq · Custom` &mdash; 001_BirdCam.mp4
  - `Camera Original` &mdash; SCKR1000.mp4 (with collision warning)
- **Underscore or space separator** &mdash; toggle between `BirdCam_001` and `BirdCam 001`
- **Custom prefix** &mdash; type anything (capitals, spaces allowed)
- **Date subfolders** &mdash; auto-organize into `YYYY-MM-DD/` directories
- **Auto-open folder** after download completes

### Manage Camera Storage

- **Storage usage bar** shows used/free/total with a visual indicator (teal &rarr; orange at 80% &rarr; red at 95%)
- **Delete synced files** removes only what you've already downloaded
- **Wipe all** clears the entire camera (with confirmation)
- **Auto-delete** option removes files from camera immediately after download

### Presets & History

- **Camera and Settings Presets** &mdash; save and switch between named configurations (IP, folder, naming scheme, prefix, separator)
- **Session history** &mdash; log of all downloads and deletes with timestamps
- **Export history to CSV** for record-keeping

### System Integration

- **Desktop notifications** on download complete and connection loss (macOS, Windows, Linux)
- **macOS folder permissions** &mdash; Info.plist includes Desktop/Documents/Downloads usage descriptions for TCC
- **Remembers your last-used settings** &mdash; naming scheme, prefix, separator, folder, and all options persist across launches

---

## Compatible Cameras

| Camera | Status |
|--------|--------|
| [Hibird 4K](https://hibird.com) (all models) | **Confirmed** |
| [Hibird DIY](https://hibird.com) (BK800 + SP700) | **Confirmed** |

HumDrop works with WiFi cameras that run a telnet server (port 23) and busybox HTTP server (port 8080) for file access. This includes most Hibird/Camojojo bird cameras and potentially other trail cameras with similar firmware.

Have a camera that works with HumDrop? [Report it here.](../../issues/new?template=camera-compatibility.md)

---

## Platform Notes

### macOS

The app is not code-signed. On first launch:

1. Double-click `HumDrop.app` &mdash; macOS will block it
2. Open **System Settings &rarr; Privacy & Security**
3. Scroll down and click **"Open Anyway"**
4. You only need to do this once

### Windows

Windows Defender may flag the executable on first run (this is common with PyInstaller-built apps and is a false positive):

1. Click **"More info"** on the SmartScreen warning
2. Click **"Run anyway"**
3. If your firewall prompts for network access, click **Allow** &mdash; HumDrop needs to reach the camera on your local network

### Linux

Requires a desktop environment (GNOME, KDE, Xfce, etc.) for the GUI. Notifications use `notify-send` if available.

```bash
tar -xzf HumDrop-Linux.tar.gz
cd HumDrop
./HumDrop
```

---

## Building from Source

### Requirements

- Python 3.9+ (3.12 recommended)
- `customtkinter` (installed via `pip install -r requirements.txt`)
- `Pillow` (for icon generation only)
- `pyinstaller` (for building executables)

### Build an Executable

```bash
cd cross-platform
pip install customtkinter pyinstaller Pillow

# macOS
pyinstaller --onedir --windowed --name HumDrop --icon HumDrop.icns \
  --osx-bundle-identifier com.fe2o3.humdrop --collect-all customtkinter humdrop.py

# Windows
pyinstaller --onedir --windowed --name HumDrop --collect-all customtkinter humdrop.py

# Linux
pyinstaller --onedir --windowed --name HumDrop --collect-all customtkinter humdrop.py
```

Builds are also automated via GitHub Actions. Push a tag like `v0.08` to trigger builds for all three platforms with artifacts uploaded to the release.

---

## How It Works

1. **Connect** &mdash; HumDrop opens a telnet session (port 23) to the camera and starts its built-in HTTP server (busybox httpd, port 8080)
2. **Scan** &mdash; Lists files from `/mnt/mmc/DCIM/100SYCAM/` (photos) and `/mnt/mmc/DCIM/101SYCAM/` (videos)
3. **Download** &mdash; Fetches each file over HTTP at full original resolution
4. **Rename** &mdash; Applies your chosen naming scheme with proper sequencing
5. **Clean up** &mdash; Optionally deletes synced files from camera storage

The camera's phone app streams at 1080p max. HumDrop bypasses the app entirely and pulls the raw 4K MP4 files and full-resolution JPGs directly from the camera's storage.

---

## License

MIT

---

If you find HumDrop useful, consider [supporting the project](https://ko-fi.com/fe2_o3).
