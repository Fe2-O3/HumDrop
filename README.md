<p align="center">
  <img src="cross-platform/HumDrop.ico" width="128" alt="HumDrop icon">
</p>

<h1 align="center">HumDrop</h1>

<p align="center">
  <strong>Pull full 4K video and photos from your WiFi bird camera &mdash; straight to your computer.</strong><br>
  No phone app. No cloud. No quality loss.
</p>

<p align="center">
  <a href="https://github.com/Fe2-O3/HumDrop/releases/latest"><img src="https://img.shields.io/github/v/release/Fe2-O3/HumDrop?style=flat-square&color=1AB89E&label=Download" alt="Latest Release"></a>&nbsp;
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-1AB89E?style=flat-square" alt="Platform">&nbsp;
  <img src="https://img.shields.io/badge/python-3.9%2B-1AB89E?style=flat-square" alt="Python">&nbsp;
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Fe2-O3/HumDrop?style=flat-square&color=1AB89E" alt="License"></a>
</p>

---

## The Problem

WiFi bird cameras like the **Hibird 4K** shoot gorgeous 4K footage, but the phone app only transfers downscaled 1080p clips. You're losing half your resolution every time.

## The Solution

HumDrop connects directly to the camera over your local WiFi and pulls the **original full-resolution files** &mdash; 4K MP4 video and full-res JPG photos &mdash; straight from the SD card to your computer.

---

## Install

### macOS &amp; Linux &mdash; Homebrew (recommended)

```bash
brew install Fe2-O3/tap/humdrop
humdrop
```

Builds from source &mdash; no Gatekeeper warning on macOS, no hassle on Linux.

### curl (macOS &amp; Linux &mdash; no Gatekeeper)

```bash
# macOS
curl -sL https://github.com/Fe2-O3/HumDrop/releases/latest/download/HumDrop-macOS.zip -o /tmp/HumDrop.zip \
  && unzip -o /tmp/HumDrop.zip -d /Applications && rm /tmp/HumDrop.zip \
  && xattr -cr /Applications/HumDrop.app && open /Applications/HumDrop.app

# Linux
curl -sL https://github.com/Fe2-O3/HumDrop/releases/latest/download/HumDrop-Linux.tar.gz | tar -xz -C ~/
~/HumDrop/HumDrop
```

Downloads, installs, and strips the quarantine flag &mdash; app opens with no Gatekeeper warning.

### Direct Download

| Platform | Download | Run |
|----------|----------|-----|
| **macOS** | [HumDrop-macOS.zip](https://github.com/Fe2-O3/HumDrop/releases/latest) | Unzip, open `HumDrop.app` (see [first launch](#first-launch)) |
| **Windows** | [HumDrop-Windows.zip](https://github.com/Fe2-O3/HumDrop/releases/latest) | Extract folder, run `HumDrop.exe` (see [first launch](#first-launch)) |
| **Linux** | [HumDrop-Linux.tar.gz](https://github.com/Fe2-O3/HumDrop/releases/latest) | Extract, run `./HumDrop` |

### Run from Source (all platforms)

```bash
git clone https://github.com/Fe2-O3/HumDrop.git
cd HumDrop/cross-platform
pip install -r requirements.txt
python humdrop.py
```

---

## Features

### Connect & Sync

- **Auto-discover** cameras on your network, or enter IP manually
- **Download at full resolution** &mdash; 4K MP4 video, full-res JPG photos
- **Live speed and ETA** during transfers (MB/s, time remaining)
- **Smart sync** &mdash; skips files you already have (size-based detection)
- **5-second heartbeat** monitors connection in real time
- **Disconnect handling** &mdash; returns to home screen, updates status, sends system notification

### Organize

Seven naming schemes with live preview in the dropdown:

| Scheme | Example |
|--------|---------|
| `Custom · Date · Seq` | BirdCam_2026-01-31_001.mp4 |
| `Custom · Timestamp` | BIR_20260131_140144.mp4 |
| `Date · Time · Custom` | 2026-01-31_14-01_BirdCam.mp4 |
| `Date · Seq · Custom` | 2026-01-31_001_BirdCam.mp4 |
| `Custom · Seq` | BirdCam_001.mp4 |
| `Seq · Custom` | 001_BirdCam.mp4 |
| `Camera Original` | SCKR1000.mp4 |

Plus:
- **Underscore or space** separator toggle
- **Custom prefix** &mdash; name your files anything
- **Date subfolders** &mdash; auto-sort into `YYYY-MM-DD/` directories
- **Open folder** when download finishes

### Camera Storage

- **Visual storage bar** &mdash; used/free/total with color-coded fill (teal &rarr; orange at 80% &rarr; red at 95%)
- **Persistent storage display** &mdash; last-known data survives disconnect and app restart
- **Delete synced** &mdash; remove only files you've already downloaded
- **Wipe all** &mdash; clear entire camera with confirmation
- **Auto-delete** &mdash; remove from camera immediately after each download

### Presets & History

- **Camera and Settings Presets** &mdash; save and switch between named configurations
- **Session history** with timestamps &mdash; full log of downloads and deletes
- **Export to CSV** for record-keeping

### System Integration

- **Desktop notifications** on download complete and connection loss (all platforms)
- **Persistent settings** &mdash; naming scheme, prefix, separator, folder, storage data, and all options persist across launches
- **macOS TCC permissions** &mdash; pre-configured for Desktop/Documents/Downloads folder access

---

## Compatible Cameras

| Camera | Status |
|--------|--------|
| [Hibird 4K](https://hibird.com) (all models) | Confirmed |
| [Hibird DIY](https://hibird.com) (BK800 + SP700) | Confirmed |

HumDrop works with WiFi cameras that expose a telnet server (port 23) and busybox HTTP file server (port 8080). This includes most **Hibird/Camojojo** bird cameras and potentially other trail cameras with similar firmware.

> **Got a camera that works?** [Let us know.](../../issues/new?title=Camera+compatibility&body=Camera+model:+%0APlatform:+%0ANotes:+)

---

## First Launch

<details>
<summary><strong>macOS</strong> &mdash; Gatekeeper warning (one-time, direct download only)</summary>

The app is not code-signed. On first launch:

1. Double-click **HumDrop.app** &mdash; macOS will block it
2. Open **System Settings &rarr; Privacy & Security**
3. Scroll down and click **"Open Anyway"**
4. Done &mdash; won't ask again

Or use [Sentinel](https://github.com/alienator88/Sentinel) to remove the quarantine flag before opening &mdash; a free, open-source tool that makes managing unsigned apps easy.

**Tip:** Installing via Homebrew (`brew install Fe2-O3/tap/humdrop`) avoids this entirely.

</details>

<details>
<summary><strong>Windows</strong> &mdash; SmartScreen warning (one-time)</summary>

Windows Defender SmartScreen may flag the executable (common for PyInstaller apps, not a real threat):

1. Click **"More info"**
2. Click **"Run anyway"**
3. If your firewall prompts for network access, click **Allow** &mdash; HumDrop needs local network access to reach the camera

</details>

<details>
<summary><strong>Linux</strong> &mdash; no special steps</summary>

Requires a desktop environment (GNOME, KDE, Xfce, etc.). Notifications use `notify-send` if available.

```bash
tar -xzf HumDrop-Linux.tar.gz
cd HumDrop
./HumDrop
```

</details>

---

## How It Works

```
You                         HumDrop                        Camera
 |                            |                              |
 |  Click "Connect"           |                              |
 |--------------------------->|  Telnet (port 23)            |
 |                            |----------------------------->|
 |                            |  Start HTTP server           |
 |                            |----------------------------->|
 |                            |  List /mnt/mmc/DCIM/         |
 |                            |<-----------------------------|
 |  See files, click Download |                              |
 |--------------------------->|  HTTP GET each file (8080)   |
 |                            |<-----------------------------|
 |                            |  Rename + save to disk       |
 |  Done! Files on your       |                              |
 |  computer at full 4K       |                              |
```

The camera's phone app caps transfers at 1080p. HumDrop bypasses the app entirely and pulls raw files from the camera's SD card over HTTP.

---

## Building from Source

```bash
cd cross-platform
pip install customtkinter pyinstaller

# macOS
pyinstaller --onedir --windowed --name HumDrop --icon HumDrop.icns \
  --osx-bundle-identifier com.fe2o3.humdrop --collect-all customtkinter humdrop.py

# Windows
pyinstaller --onedir --windowed --name HumDrop --collect-all customtkinter humdrop.py

# Linux
pyinstaller --onedir --windowed --name HumDrop --collect-all customtkinter humdrop.py
```

Automated builds run via GitHub Actions on every version tag &mdash; macOS, Windows, and Linux artifacts are attached to each [release](https://github.com/Fe2-O3/HumDrop/releases).

---

## License

[MIT](LICENSE)

---

<p align="center">
  Built for birders by <a href="https://github.com/Fe2-O3">Fe2-O3</a><br>
  <sub>If HumDrop helps you, consider <a href="https://ko-fi.com/fe2_o3">buying me a coffee</a>.</sub>
</p>
