#!/usr/bin/env python3
"""
HumDrop v0.05 — Cross-platform camera sync utility
By Kenneth Russell DeGraff

Syncs videos and photos from WiFi-enabled trail/bird cameras.
Downloads original files directly from camera storage over local network.
"""

import os
import sys
import re
import json
import math
import time
import socket
import shutil
import platform
import subprocess
import threading
import webbrowser
import urllib.request
import tkinter as tk
from enum import Enum
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Callable
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk
import customtkinter as ctk

# ============================================================
# MARK: - Naming Schemes
# ============================================================

class NamingScheme(Enum):
    PREFIX_DATE = "prefix_date"
    TIMESTAMP_FULL = "timestamp_full"
    DATE_PREFIX = "date_prefix"
    DATE_SEQ_CUSTOM = "date_seq_custom"
    CUSTOM_SEQ = "custom_seq"
    SEQ_CUSTOM = "seq_custom"
    ORIGINAL = "original"

    @property
    def display_name(self):
        return {
            NamingScheme.PREFIX_DATE: "custom_date_seq",
            NamingScheme.TIMESTAMP_FULL: "CUSTOM_timestamp",
            NamingScheme.DATE_PREFIX: "date_time_custom",
            NamingScheme.DATE_SEQ_CUSTOM: "date_seq_custom",
            NamingScheme.CUSTOM_SEQ: "custom_seq",
            NamingScheme.SEQ_CUSTOM: "seq_custom",
            NamingScheme.ORIGINAL: "Camera original",
        }[self]

    def example(self, prefix: str) -> str:
        p = prefix or "cam"
        short = p[:3].upper()
        return {
            NamingScheme.PREFIX_DATE: f"{p}_2026-01-31_001.mp4",
            NamingScheme.TIMESTAMP_FULL: f"{short}_20260131_140144.mp4",
            NamingScheme.DATE_PREFIX: f"2026-01-31_14-01_{p}.mp4",
            NamingScheme.DATE_SEQ_CUSTOM: f"2026-01-31_001_{p}.mp4",
            NamingScheme.CUSTOM_SEQ: f"{p}_001.mp4",
            NamingScheme.SEQ_CUSTOM: f"001_{p}.mp4",
            NamingScheme.ORIGINAL: "SCKR1000.mp4",
        }[self]

    def generate_name(self, prefix: str, date: Optional[datetime], ext: str, counter: int) -> str:
        p = prefix or "cam"
        short = p[:3].upper()
        d = date or datetime.now()
        if self == NamingScheme.PREFIX_DATE:
            return f"{p}_{d.year:04d}-{d.month:02d}-{d.day:02d}_{counter:03d}.{ext}"
        elif self == NamingScheme.TIMESTAMP_FULL:
            return f"{short}_{d.year:04d}{d.month:02d}{d.day:02d}_{d.hour:02d}{d.minute:02d}{d.second:02d}.{ext}"
        elif self == NamingScheme.DATE_PREFIX:
            return f"{d.year:04d}-{d.month:02d}-{d.day:02d}_{d.hour:02d}-{d.minute:02d}_{p}.{ext}"
        elif self == NamingScheme.DATE_SEQ_CUSTOM:
            return f"{d.year:04d}-{d.month:02d}-{d.day:02d}_{counter:03d}_{p}.{ext}"
        elif self == NamingScheme.CUSTOM_SEQ:
            return f"{p}_{counter:03d}.{ext}"
        elif self == NamingScheme.SEQ_CUSTOM:
            return f"{counter:03d}_{p}.{ext}"
        return ""


# ============================================================
# MARK: - Data Model
# ============================================================

@dataclass
class CameraFile:
    name: str
    directory: str
    is_video: bool
    is_downloaded: bool = False
    size_bytes: int = 0
    selected: bool = True
    local_name: str = ""
    remote_timestamp: Optional[datetime] = None

    def __post_init__(self):
        if not self.local_name:
            self.local_name = self.name

    @property
    def is_renamed(self) -> bool:
        return self.local_name != self.name

    @property
    def remote_path(self) -> str:
        return f"/mnt/mmc/DCIM/{self.directory}/{self.name}"

    @property
    def http_path(self) -> str:
        return f"{self.directory}/{self.name}"

    @property
    def size_string(self) -> str:
        if self.size_bytes <= 0:
            return ""
        mb = self.size_bytes / 1_048_576
        if mb >= 1000:
            return f"{mb / 1024:.1f} GB"
        return f"{mb:.1f} MB"


# ============================================================
# MARK: - Camera Manager
# ============================================================

class CameraManager:
    def __init__(self):
        self.telnet_port = 23
        self.http_port = 8080
        self._persistent_sock: Optional[socket.socket] = None
        self._load_settings()

    def _settings_path(self) -> Path:
        system = platform.system()
        if system == "Darwin":
            base = Path.home() / "Library" / "Application Support" / "HumDrop"
        elif system == "Windows":
            base = Path(os.environ.get("APPDATA", Path.home())) / "HumDrop"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "humdrop"
        base.mkdir(parents=True, exist_ok=True)
        return base / "settings.json"

    def _default_video_dir(self) -> Path:
        return Path.home() / "Desktop" / "Hummingbird" / "videos"

    def _load_settings(self):
        self.camera_ip = "192.168.1."
        self.video_dir = self._default_video_dir()
        self.naming_scheme = NamingScheme.PREFIX_DATE
        self.naming_prefix = "BirdCam"
        try:
            with open(self._settings_path()) as f:
                data = json.load(f)
            self.camera_ip = data.get("camera_ip", self.camera_ip)
            d = data.get("video_dir")
            if d:
                self.video_dir = Path(d)
            s = data.get("naming_scheme", "")
            for ns in NamingScheme:
                if ns.value == s:
                    self.naming_scheme = ns
                    break
            self.naming_prefix = data.get("naming_prefix", self.naming_prefix)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        self.video_dir.mkdir(parents=True, exist_ok=True)

    def save_settings(self):
        data = {
            "camera_ip": self.camera_ip,
            "video_dir": str(self.video_dir),
            "naming_scheme": self.naming_scheme.value,
            "naming_prefix": self.naming_prefix,
        }
        try:
            with open(self._settings_path(), "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def change_video_dir(self, path: Path):
        self.video_dir = path
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.save_settings()

    # --- Debug logging ---

    def _log_path(self) -> Path:
        return self.video_dir.parent / "debug.log"

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        try:
            with open(self._log_path(), "a") as f:
                f.write(line)
        except OSError:
            pass

    # --- TCP / Telnet ---

    def _open_tcp(self, timeout: float = 5.0) -> Optional[socket.socket]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((self.camera_ip, self.telnet_port))
            return sock
        except (OSError, socket.error) as e:
            self.log(f"[TCP] Failed to connect: {e}")
            return None

    def _drain(self, sock: socket.socket) -> bytes:
        data = b""
        sock.setblocking(False)
        try:
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                data += chunk
        except BlockingIOError:
            pass
        except OSError:
            pass
        sock.setblocking(True)
        return data

    @staticmethod
    def _strip_iac(data: bytes) -> str:
        clean = bytearray()
        i = 0
        while i < len(data):
            if data[i] == 0xFF and i + 2 < len(data):
                i += 3
            else:
                clean.append(data[i])
                i += 1
        try:
            return clean.decode("utf-8")
        except UnicodeDecodeError:
            return clean.decode("ascii", errors="replace")

    def _run_fresh_command(self, command: str, read_delay: float = 2.0) -> str:
        sock = self._open_tcp(timeout=3.0)
        if not sock:
            self.log(f"[CMD] Failed to open TCP for: {command}")
            return ""
        try:
            time.sleep(1.0)
            self._drain(sock)
            sock.sendall((command + "\n").encode("utf-8"))
            time.sleep(read_delay)
            sock.settimeout(0.5)
            raw = b""
            try:
                while True:
                    chunk = sock.recv(8192)
                    if not chunk:
                        break
                    raw += chunk
            except (socket.timeout, BlockingIOError, OSError):
                pass
            result = self._strip_iac(raw)
            self.log(f"[CMD] '{command}' -> {len(raw)} bytes, {len(result)} chars")
            return result
        finally:
            sock.close()

    def is_reachable(self) -> bool:
        self.log(f"[REACH] Trying TCP to {self.camera_ip}:{self.telnet_port}...")
        sock = self._open_tcp(timeout=5.0)
        if sock:
            sock.close()
            self.log("[REACH] OK")
            return True
        self.log("[REACH] FAILED")
        return False

    def start_httpd(self):
        if self._persistent_sock:
            try:
                self._persistent_sock.close()
            except OSError:
                pass
        sock = self._open_tcp(timeout=5.0)
        if not sock:
            self.log("[HTTPD] Failed to open TCP")
            return
        self._persistent_sock = sock
        time.sleep(1.0)
        self._drain(sock)
        cmd = f"killall busybox 2>/dev/null; busybox httpd -p {self.http_port} -h /mnt/mmc/DCIM\n"
        sock.sendall(cmd.encode("utf-8"))
        time.sleep(3.0)
        ok = self.is_httpd_running()
        self.log(f"[HTTPD] Running: {ok}")
        ls = self._run_fresh_command("ls /mnt/mmc/DCIM/", read_delay=2.0)
        self.log(f"[HTTPD] ls DCIM: {ls[:500]}")

    def is_httpd_running(self) -> bool:
        try:
            url = f"http://{self.camera_ip}:{self.http_port}/"
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status < 500
        except Exception:
            return False

    # --- File listing ---

    def list_files_via_telnet(self) -> List[CameraFile]:
        files = []
        for directory, ext, is_video in [("101SYCAM", "mp4", True), ("100SYCAM", "jpg", False)]:
            output = self._run_fresh_command(f"ls -la /mnt/mmc/DCIM/{directory}/", read_delay=3.0)
            self.log(f"[TELNET {directory}] ({len(output)} chars): {output[:500]}")
            files.extend(self._parse_ls_la(output, directory, ext, is_video))
        return sorted(files, key=lambda f: f.name.lower())

    def _parse_ls_la(self, output: str, directory: str, ext: str, is_video: bool) -> List[CameraFile]:
        clean = re.sub(r'\x1b\[[\d;]*m', '', output)
        files = []
        seen = set()
        now = datetime.now()
        current_year = now.year
        months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                  "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

        pattern = re.compile(
            r'(\d+)\s+(\w{3})\s+(\d{1,2})\s+(\d{2}:\d{2}|\d{4})\s+([\w][\w\-.]+\.' + ext + r')\s*$',
            re.IGNORECASE | re.MULTILINE
        )

        for m in pattern.finditer(clean):
            size_str, mon_str, day_str, time_or_year, filename = m.groups()
            if filename in seen:
                continue
            seen.add(filename)

            f = CameraFile(name=filename, directory=directory, is_video=is_video)
            f.size_bytes = int(size_str) if size_str.isdigit() else 0

            mon_num = months.get(mon_str.lower(), 1)
            day_num = int(day_str)

            if ":" in time_or_year:
                hh, mm = map(int, time_or_year.split(":"))
                try:
                    ts = datetime(current_year, mon_num, day_num, hh, mm)
                    if ts > now:
                        ts = datetime(current_year - 1, mon_num, day_num, hh, mm)
                    f.remote_timestamp = ts
                except ValueError:
                    pass
            else:
                try:
                    f.remote_timestamp = datetime(int(time_or_year), mon_num, day_num)
                except ValueError:
                    pass

            files.append(f)

        if not files:
            for m in re.finditer(r'SCKR\d+\.' + ext, clean):
                name = m.group()
                if name not in seen:
                    seen.add(name)
                    files.append(CameraFile(name=name, directory=directory, is_video=is_video))

        return files

    def _fetch_url(self, url: str) -> tuple:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return (resp.status, body)
        except Exception as e:
            self.log(f"[HTTP] fetch {url}: {e}")
            return (0, "")

    def list_files_via_http(self) -> List[CameraFile]:
        files = []
        root_status, root_body = self._fetch_url(f"http://{self.camera_ip}:{self.http_port}/")
        self.log(f"[HTTP root] status={root_status} body={root_body[:500]}")

        for subdir, ext, is_video in [("101SYCAM", "mp4", True), ("100SYCAM", "jpg", False)]:
            files.extend(self._fetch_http_listing(subdir, ext, is_video))

        if not files and root_status == 200:
            for m in re.finditer(r'href="([^"]+)/"', root_body):
                subdir = m.group(1)
                if ".." in subdir:
                    continue
                files.extend(self._fetch_http_listing(subdir, "mp4", True))
                files.extend(self._fetch_http_listing(subdir, "jpg", False))

        return sorted(files, key=lambda f: f.name.lower())

    def _fetch_http_listing(self, subdir: str, ext: str, is_video: bool) -> List[CameraFile]:
        url = f"http://{self.camera_ip}:{self.http_port}/{subdir}/"
        status, body = self._fetch_url(url)
        self.log(f"[HTTP {subdir}] status={status} body={body[:500]}")
        files = []
        seen = set()

        for m in re.finditer(r'SCKR\d+\.' + ext, body):
            name = m.group()
            if name not in seen:
                seen.add(name)
                f = CameraFile(name=name, directory=subdir, is_video=is_video)
                f.is_downloaded = (self.video_dir / name).exists()
                f.selected = not f.is_downloaded
                files.append(f)

        if not files:
            for m in re.finditer(r'[\w\-]+\.' + ext, body, re.IGNORECASE):
                name = m.group()
                if name not in seen:
                    seen.add(name)
                    f = CameraFile(name=name, directory=subdir, is_video=is_video)
                    f.is_downloaded = (self.video_dir / name).exists()
                    f.selected = not f.is_downloaded
                    files.append(f)

        return files

    def list_files(self) -> List[CameraFile]:
        self.log("=== list_files() starting ===")
        files = self.list_files_via_telnet()
        self.log(f"Telnet found {len(files)} files")
        if not files:
            files = self.list_files_via_http()
            self.log(f"HTTP found {len(files)} files")
        if files:
            self.resolve_file_names(files)
        return files

    # --- Size-based sync + naming ---

    def _build_local_size_map(self) -> Dict[int, str]:
        size_map = {}
        try:
            for entry in self.video_dir.iterdir():
                if entry.suffix.lower() in (".mp4", ".jpg"):
                    size = entry.stat().st_size
                    if size > 0:
                        size_map[size] = entry.name
        except OSError:
            pass
        return size_map

    def _find_next_counter(self, date: Optional[datetime], ext: str) -> int:
        scheme = self.naming_scheme
        p = self.naming_prefix or "cam"
        max_counter = 0
        try:
            for entry in self.video_dir.iterdir():
                if not entry.name.endswith(f".{ext}"):
                    continue
                if scheme == NamingScheme.CUSTOM_SEQ:
                    # Match: {prefix}_{NNN}.{ext}
                    m = re.search(r'^' + re.escape(p) + r'_(\d{3})\.' + re.escape(ext) + r'$', entry.name)
                elif scheme == NamingScheme.SEQ_CUSTOM:
                    # Match: {NNN}_{prefix}.{ext}
                    m = re.search(r'^(\d{3})_' + re.escape(p) + r'\.' + re.escape(ext) + r'$', entry.name)
                else:
                    # Date-based schemes: match by date string
                    if date:
                        date_str = f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
                        if date_str not in entry.name:
                            continue
                    m = re.search(r'_(\d{3})\.' + re.escape(ext) + r'$', entry.name)
                if m:
                    max_counter = max(max_counter, int(m.group(1)))
        except OSError:
            pass
        return max_counter + 1

    def resolve_file_names(self, files: List[CameraFile]):
        local_sizes = self._build_local_size_map()
        date_counters: Dict[str, int] = {}

        for f in files:
            if f.size_bytes > 0 and f.size_bytes in local_sizes:
                f.is_downloaded = True
                f.selected = False
                f.local_name = local_sizes[f.size_bytes]
                self.log(f"[RESOLVE] {f.name} -> already have {f.local_name} (size={f.size_bytes})")
                continue

            f.is_downloaded = False
            f.selected = True

            if self.naming_scheme == NamingScheme.ORIGINAL:
                f.local_name = f.name
                candidate = f.local_name
                n = 1
                while (self.video_dir / candidate).exists():
                    base = Path(f.name).stem
                    ext = "mp4" if f.is_video else "jpg"
                    candidate = f"{base}_{n}.{ext}"
                    n += 1
                f.local_name = candidate
            else:
                ext = "mp4" if f.is_video else "jpg"
                date = f.remote_timestamp or datetime.now()
                # Dateless schemes use a global counter; date-based use per-date
                if self.naming_scheme in (NamingScheme.CUSTOM_SEQ, NamingScheme.SEQ_CUSTOM):
                    date_key = "_global"
                    counter_date = None
                else:
                    date_key = f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
                    counter_date = date
                if date_key not in date_counters:
                    date_counters[date_key] = self._find_next_counter(counter_date, ext)
                counter = date_counters[date_key]
                date_counters[date_key] = counter + 1
                f.local_name = self.naming_scheme.generate_name(self.naming_prefix, date, ext, counter)

            self.log(f"[RESOLVE] {f.name} -> {f.local_name} (new)")

    # --- Download ---

    def download_file(self, file: CameraFile, progress_cb: Callable, done_cb: Callable,
                      cancel_check: Optional[Callable] = None):
        url = f"http://{self.camera_ip}:{self.http_port}/{file.http_path}"
        dest = self.video_dir / file.local_name
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as out:
                    while True:
                        if cancel_check and cancel_check():
                            self.log(f"[DOWNLOAD] {file.name} cancelled")
                            break
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            progress_cb(downloaded / total)

            # Check for cancellation or incomplete download
            if cancel_check and cancel_check():
                self._remove_partial(dest)
                done_cb(False)
                return

            if total > 0 and downloaded < total:
                self.log(f"[DOWNLOAD] {file.name} incomplete: {downloaded}/{total} bytes")
                self._remove_partial(dest)
                done_cb(False)
                return

            if file.remote_timestamp:
                ts = file.remote_timestamp.timestamp()
                os.utime(dest, (ts, ts))

            done_cb(dest.stat().st_size > 0)
        except Exception as e:
            self.log(f"[DOWNLOAD] {file.name} error: {e}")
            self._remove_partial(dest)
            done_cb(False)

    def _remove_partial(self, path: Path):
        """Remove a partially-downloaded file to avoid corruption."""
        try:
            if path.exists():
                path.unlink()
                self.log(f"[CLEANUP] Removed partial file: {path.name}")
        except OSError:
            pass

    def delete_file(self, file: CameraFile):
        result = self._run_fresh_command(f"rm -f {file.remote_path}", read_delay=1.0)
        self.log(f"[DELETE] {file.name}: {result}")

    def wipe_all(self):
        r1 = self._run_fresh_command("rm -f /mnt/mmc/DCIM/101SYCAM/*", read_delay=2.0)
        self.log(f"[WIPE] videos: {r1}")
        r2 = self._run_fresh_command("rm -f /mnt/mmc/DCIM/100SYCAM/*", read_delay=2.0)
        self.log(f"[WIPE] photos: {r2}")

    def cleanup(self):
        if self._persistent_sock:
            try:
                self._persistent_sock.close()
            except OSError:
                pass
            self._persistent_sock = None

    # --- Camera discovery ---

    def find_camera(self, status_cb: Callable) -> Optional[str]:
        status_cb("Checking ARP table...")
        found_ip = None
        method = ""

        # ARP table scan
        try:
            if platform.system() == "Linux" and os.path.exists("/proc/net/arp"):
                with open("/proc/net/arp") as f:
                    for line in f:
                        if "50:5a:65" in line.lower():
                            found_ip = line.split()[0]
                            method = "MAC address (/proc)"
                            break
            if not found_ip:
                arp_cmd = ["arp", "-a"]
                if platform.system() == "Darwin":
                    arp_cmd = ["/usr/sbin/arp", "-a"]
                result = subprocess.run(arp_cmd, capture_output=True, text=True, timeout=5)
                mac_sep = "-" if platform.system() == "Windows" else ":"
                mac_prefix = f"50{mac_sep}5a{mac_sep}65"
                for line in result.stdout.splitlines():
                    if mac_prefix in line.lower():
                        m = re.search(r'\(([\d.]+)\)', line)
                        if m:
                            found_ip = m.group(1)
                            method = "MAC address"
                            break
        except Exception as e:
            self.log(f"[FIND] ARP error: {e}")

        if found_ip:
            return found_ip, method

        # Port scan fallback
        status_cb("Scanning ports...")
        candidates = []
        try:
            arp_cmd = ["arp", "-a"]
            if platform.system() == "Darwin":
                arp_cmd = ["/usr/sbin/arp", "-a"]
            result = subprocess.run(arp_cmd, capture_output=True, text=True, timeout=5)
            for m in re.finditer(r'\(([\d.]+)\)', result.stdout):
                candidates.append(m.group(1))
        except Exception:
            pass

        for last in [83, 84, 85, 80, 81, 82, 86, 87, 88, 89, 90]:
            ip = f"192.168.1.{last}"
            if ip not in candidates:
                candidates.append(ip)

        found = [None]
        lock = threading.Lock()

        def check_ip(ip):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.8)
                s.connect((ip, 23))
                s.close()
                s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s2.settimeout(0.8)
                s2.connect((ip, 8080))
                s2.close()
                with lock:
                    if found[0] is None:
                        found[0] = ip
            except (OSError, socket.error):
                pass

        with ThreadPoolExecutor(max_workers=20) as pool:
            pool.map(check_ip, candidates)

        if found[0]:
            return found[0], "port scan (telnet+HTTP)"
        return None, ""


# ============================================================
# MARK: - Colors
# ============================================================

TEAL = "#1AB89E"
TEAL_DARK = "#158F7A"
TEAL_HOVER = "#1FD4B5"
ORANGE = "#E8863A"
ORANGE_HOVER = "#F09848"
GREEN = "#34B759"
RED = "#F24D40"

# Adaptive color pairs: (light_mode, dark_mode)
# CustomTkinter widgets accept these tuples directly
BG_CARD = ("#ffffff", "#333333")
BG_HEADER = ("#eef0f2", "#262628")
TEXT_PRI = ("#1a1a1a", "#f0f0f0")
TEXT_SEC = ("#555555", "#b0b0b0")
TEXT_MUTED = ("#888888", "#707070")
BORDER = ("#d0d0d0", "#444444")
STRIPE = ("#f5f5f7", "#383838")
# Secondary button colors — visible against card backgrounds
BTN_SEC = ("#e0e2e6", "#484848")
BTN_SEC_HOVER = ("#cfd1d5", "#5a5a5a")


# ============================================================
# MARK: - App UI
# ============================================================

class HumDropApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.camera = CameraManager()
        self.files: List[CameraFile] = []
        self.is_connected = False
        self.is_downloading = False
        self._download_cancel = False
        self.auto_delete_var = ctk.BooleanVar(value=False)

        self.title("HumDrop")
        self.geometry("720x800")
        self.minsize(600, 650)
        ctk.set_appearance_mode("system")

        self._build_ui()
        self._show_disconnected()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Keyboard shortcuts
        self.bind_all("<Command-r>", lambda e: self._refresh())
        self.bind_all("<Control-r>", lambda e: self._refresh())
        self.bind_all("<Command-o>", lambda e: self._open_folder())
        self.bind_all("<Control-o>", lambda e: self._open_folder())

        # Monitor appearance changes to update Treeview
        self._last_mode = self._is_dark()
        self._poll_appearance()

    def _is_dark(self) -> bool:
        return ctk.get_appearance_mode() == "Dark"

    def _resolve(self, color_pair):
        """Resolve a (light, dark) color tuple to current mode's color."""
        if isinstance(color_pair, tuple):
            return color_pair[1] if self._is_dark() else color_pair[0]
        return color_pair

    def _poll_appearance(self):
        """Check for system appearance changes and update non-CTk widgets."""
        dark = self._is_dark()
        if dark != self._last_mode:
            self._last_mode = dark
            self._apply_tree_theme()
            self._update_bird_icons()
        self.after(1000, self._poll_appearance)

    def _create_bird_canvas(self, parent, size=120):
        """Draw a stylized bird icon similar to the native Swift app icon."""
        bg = self._resolve(("#f0f0f0", "#1e1e1e"))
        c = tk.Canvas(parent, width=size, height=size, highlightthickness=0, bg=bg)

        s = size
        pad = 2
        r = s * 0.22

        # Rounded rectangle background — deep teal base
        pts = [
            pad + r, pad,
            s - pad - r, pad,
            s - pad, pad,
            s - pad, pad + r,
            s - pad, s - pad - r,
            s - pad, s - pad,
            s - pad - r, s - pad,
            pad + r, s - pad,
            pad, s - pad,
            pad, s - pad - r,
            pad, pad + r,
            pad, pad,
        ]
        c.create_polygon(pts, smooth=True, fill=TEAL_DARK, outline="")

        # Lighter teal overlay on top half for gradient feel
        top_pts = [
            pad + r, pad,
            s - pad - r, pad,
            s - pad, pad,
            s - pad, pad + r,
            s - pad, s * 0.50,
            pad, s * 0.50,
            pad, pad + r,
            pad, pad,
        ]
        c.create_polygon(top_pts, smooth=True, fill=TEAL, outline="", stipple="gray50")

        # Subtle border
        c.create_polygon(pts, smooth=True, fill="", outline="white", width=1)

        # Branch — a gentle curved line across the middle
        branch_y = s * 0.54
        c.create_line(
            s * 0.06, branch_y + s * 0.02,
            s * 0.30, branch_y - s * 0.01,
            s * 0.60, branch_y + s * 0.01,
            s * 0.94, branch_y - s * 0.005,
            fill="#5C3118", width=max(2, s * 0.028), smooth=True, capstyle="round"
        )
        # Small twig
        c.create_line(
            s * 0.72, branch_y, s * 0.79, branch_y - s * 0.09,
            fill="#5C3118", width=max(1, s * 0.014), capstyle="round"
        )

        # Bird body (larger white oval, sitting on branch)
        bx = s * 0.50
        by = branch_y - s * 0.13
        bw = s * 0.10
        bh = s * 0.13
        c.create_oval(bx - bw, by - bh, bx + bw, by + bh, fill="white", outline="")

        # Head (circle, overlapping top of body)
        hr = s * 0.068
        hx = bx + s * 0.018
        hy = by - bh - hr * 0.15
        c.create_oval(hx - hr, hy - hr, hx + hr, hy + hr, fill="white", outline="")

        # Eye
        er = max(1.5, s * 0.014)
        ex = hx + hr * 0.38
        ey = hy - hr * 0.08
        c.create_oval(ex - er, ey - er, ex + er, ey + er, fill="#2a1510", outline="")
        # Eye highlight
        hlr = er * 0.45
        c.create_oval(ex - hlr + er * 0.35, ey - hlr + er * 0.35,
                      ex + hlr + er * 0.35, ey + hlr + er * 0.35,
                      fill="white", outline="")

        # Beak (orange triangle, pointing right)
        c.create_polygon(
            hx + hr * 0.75, hy + hr * 0.05,
            hx + hr * 1.7, hy + hr * 0.15,
            hx + hr * 0.75, hy + hr * 0.4,
            fill=ORANGE, outline=""
        )

        # Tail feathers (extending left from body)
        c.create_polygon(
            bx - bw * 0.35, by,
            bx - bw * 2.4, by - bh * 0.15,
            bx - bw * 2.2, by + bh * 0.12,
            bx - bw * 0.35, by + bh * 0.12,
            fill="white", outline="", smooth=True
        )

        # Legs on branch
        leg_w = max(1, s * 0.010)
        foot_y = branch_y - 1
        c.create_line(bx - s * 0.022, by + bh * 0.65, bx - s * 0.028, foot_y,
                      fill="#5C3118", width=leg_w, capstyle="round")
        c.create_line(bx + s * 0.022, by + bh * 0.65, bx + s * 0.016, foot_y,
                      fill="#5C3118", width=leg_w, capstyle="round")

        # Download arrow icon below branch
        dl_cx = s * 0.50
        dl_cy = s * 0.77
        dl_w = max(2, s * 0.018)
        shaft_h = s * 0.07
        arrow_w = s * 0.045

        # Arrow shaft (vertical line pointing down)
        c.create_line(dl_cx, dl_cy - shaft_h, dl_cx, dl_cy + shaft_h * 0.3,
                      fill="white", width=dl_w, capstyle="round")
        # Arrowhead (triangle at bottom)
        c.create_polygon(
            dl_cx, dl_cy + shaft_h * 0.7,
            dl_cx - arrow_w, dl_cy,
            dl_cx + arrow_w, dl_cy,
            fill="white", outline=""
        )
        # Tray line underneath
        tray_y = dl_cy + shaft_h * 0.85
        tray_w = s * 0.06
        c.create_line(dl_cx - tray_w, tray_y, dl_cx + tray_w, tray_y,
                      fill="white", width=dl_w, capstyle="round")

        return c

    def _apply_tree_theme(self):
        """Apply Treeview colors for the current appearance mode."""
        dark = self._is_dark()
        bg = self._resolve(BG_CARD)
        fg = "#f0f0f0" if dark else "#1a1a1a"
        heading_bg = "#3a3a3a" if dark else "#e0e2e5"
        heading_fg = "#e0e0e0" if dark else "#333333"
        stripe_bg = self._resolve(STRIPE)
        sec = self._resolve(TEXT_SEC)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.Treeview",
                        background=bg, foreground=fg, fieldbackground=bg,
                        rowheight=34, borderwidth=0, relief="flat", font=("", 15))
        style.configure("Dark.Treeview.Heading",
                        background=heading_bg, foreground=heading_fg,
                        borderwidth=0, relief="flat", font=("", 15, "bold"))
        style.map("Dark.Treeview",
                  background=[("selected", TEAL_DARK)],
                  foreground=[("selected", "white")])
        style.layout("Dark.Treeview", [("Dark.Treeview.treearea", {"sticky": "nsew"})])

        if hasattr(self, 'tree'):
            self.tree.tag_configure("synced", foreground=sec)
            self.tree.tag_configure("new", foreground=GREEN)
            self.tree.tag_configure("stripe", background=stripe_bg)
            self.tree.tag_configure("video_type", foreground="#5B9BD5")
            self.tree.tag_configure("photo_type", foreground=ORANGE)

    def _update_bird_icons(self):
        """Redraw bird canvases after appearance change."""
        # The canvases are recreated with the frames, so just force redraw
        if hasattr(self, '_bird_canvases'):
            for canvas_info in self._bird_canvases:
                canvas_info['canvas'].destroy()
                new_c = self._create_bird_canvas(canvas_info['parent'], canvas_info['size'])
                new_c.pack(pady=canvas_info.get('pady', (0, 16)))
                canvas_info['canvas'] = new_c

    def _build_ui(self):
        self._bird_canvases = []
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # --- Accent strip ---
        self.accent_strip = ctk.CTkFrame(self, height=4, fg_color=TEAL, corner_radius=0)
        self.accent_strip.grid(row=0, column=0, sticky="ew")

        # --- Header ---
        header = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0)
        header.grid(row=1, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        # Title row
        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.grid(row=0, column=0, columnspan=3, sticky="w", padx=20, pady=(12, 0))

        ctk.CTkLabel(title_frame, text="HumDrop", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkLabel(title_frame, text="Camera Sync", font=ctk.CTkFont(size=16),
                     text_color=TEAL).pack(side="left", padx=(8, 0), pady=(4, 0))

        # Right side buttons
        btn_frame = ctk.CTkFrame(header, fg_color="transparent")
        btn_frame.grid(row=0, column=2, rowspan=2, sticky="ne", padx=20, pady=(10, 0))

        self.connect_btn = ctk.CTkButton(btn_frame, text="Connect", width=100, fg_color=TEAL,
                                          hover_color=TEAL_HOVER, text_color="white",
                                          command=self._connect)
        self.connect_btn.pack(pady=(0, 4))

        ctk.CTkButton(btn_frame, text="About", width=100, fg_color=TEAL,
                      hover_color=TEAL_HOVER, text_color="white",
                      command=self._show_about).pack()

        # IP row
        ip_frame = ctk.CTkFrame(header, fg_color="transparent")
        ip_frame.grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(6, 0))

        ctk.CTkLabel(ip_frame, text="Camera IP", font=ctk.CTkFont(size=16),
                     text_color=TEXT_SEC).pack(side="left")

        self.ip_entry = ctk.CTkEntry(ip_frame, width=150, font=ctk.CTkFont(family="Courier", size=16))
        self.ip_entry.pack(side="left", padx=(8, 4))
        self.ip_entry.insert(0, self.camera.camera_ip)

        self.find_btn = ctk.CTkButton(ip_frame, text="Find", width=50, height=30,
                                       fg_color=TEAL, hover_color=TEAL_HOVER,
                                       text_color="white", command=self._find_camera)
        self.find_btn.pack(side="left", padx=(0, 10))

        # Status dot + label
        self.status_dot = ctk.CTkLabel(ip_frame, text="\u25CF", font=ctk.CTkFont(size=18),
                                        text_color=RED, width=18)
        self.status_dot.pack(side="left")

        self.status_label = ctk.CTkLabel(ip_frame, text="Disconnected", font=ctk.CTkFont(size=16),
                                          text_color=TEXT_SEC)
        self.status_label.pack(side="left", padx=(4, 0))

        # Folder row
        folder_frame = ctk.CTkFrame(header, fg_color="transparent")
        folder_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=20, pady=(6, 12))
        folder_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(folder_frame, text="Save to:", font=ctk.CTkFont(size=16),
                     text_color=TEXT_SEC).grid(row=0, column=0, sticky="w")

        self.folder_btn = ctk.CTkButton(
            folder_frame, text=self._short_path(self.camera.video_dir),
            font=ctk.CTkFont(family="Courier", size=16), anchor="w",
            fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI, height=32,
            command=self._change_folder
        )
        self.folder_btn.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        self.openfolder_btn = ctk.CTkButton(
            folder_frame, text="Open Folder", width=110, height=32,
            fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
            command=self._open_folder
        )
        self.openfolder_btn.grid(row=0, column=2, sticky="e", padx=(8, 0))

        # --- Main content area ---
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=2, column=0, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        # Instruction overlay
        self.instruction_frame = ctk.CTkFrame(self.content, fg_color="transparent")
        self.instruction_frame.grid(row=0, column=0, sticky="nsew")

        inner = ctk.CTkFrame(self.instruction_frame, fg_color="transparent")
        inner.place(relx=0.5, rely=0.4, anchor="center")

        bird_c = self._create_bird_canvas(inner, size=120)
        bird_c.pack(pady=(0, 16))
        self._bird_canvases.append({'canvas': bird_c, 'parent': inner, 'size': 120, 'pady': (0, 16)})

        ctk.CTkLabel(inner, text="To get started:\n\n"
                     "1.  Open the camera app on your phone\n"
                     "2.  Start the camera stream\n"
                     "3.  Make sure you're on the same Wi-Fi network\n"
                     '4.  Enter the camera IP, or tap "Find" to auto-discover,\n'
                     '     then "Connect"',
                     font=ctk.CTkFont(size=15), text_color=TEXT_SEC,
                     justify="left").pack()

        # Files section
        self.files_frame = ctk.CTkFrame(self.content, fg_color="transparent")

        self._build_files_section()

    def _build_files_section(self):
        f = self.files_frame
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)

        # Header row
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(12, 0))
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(hdr, text="Files", font=ctk.CTkFont(size=18, weight="bold")).grid(row=0, column=0, sticky="w")

        btn_row = ctk.CTkFrame(hdr, fg_color="transparent")
        btn_row.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(btn_row, text="Select All", width=95, height=32, font=ctk.CTkFont(size=15),
                      fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
                      command=self._select_all).pack(side="left", padx=(0, 4))
        ctk.CTkButton(btn_row, text="Select New", width=95, height=32, font=ctk.CTkFont(size=15),
                      fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
                      command=self._select_new).pack(side="left")

        # Table (ttk.Treeview with themed styling)
        self.table_frame = ctk.CTkFrame(f, fg_color=BG_CARD, corner_radius=8,
                                         border_width=0)
        self.table_frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=(8, 0))
        self.table_frame.grid_rowconfigure(0, weight=1)
        self.table_frame.grid_columnconfigure(0, weight=1)

        self._apply_tree_theme()

        cols = ("check", "camera", "saveas", "size", "type", "status")
        self.tree = ttk.Treeview(self.table_frame, columns=cols, show="headings",
                                 style="Dark.Treeview", selectmode="none")

        self.tree.heading("check", text="")
        self.tree.heading("camera", text="Camera File", anchor="w")
        self.tree.heading("saveas", text="Save As", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("type", text="Type", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")

        self.tree.column("check", width=36, minwidth=36, stretch=False, anchor="center")
        self.tree.column("camera", width=130, minwidth=80, anchor="w")
        self.tree.column("saveas", width=180, minwidth=100, anchor="w")
        self.tree.column("size", width=70, minwidth=50, stretch=False, anchor="e")
        self.tree.column("type", width=55, minwidth=45, stretch=False, anchor="w")
        self.tree.column("status", width=65, minwidth=50, stretch=False, anchor="w")

        # Apply tag colors now that tree exists
        self._apply_tree_theme()

        scrollbar = ctk.CTkScrollbar(self.table_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4)

        self.tree.bind("<Button-1>", self._on_tree_click)

        # Summary
        self.summary_label = ctk.CTkLabel(f, text="", font=ctk.CTkFont(size=16), text_color=TEXT_SEC)
        self.summary_label.grid(row=2, column=0, sticky="w", padx=20, pady=(4, 0))

        # Progress
        self.progress_bar = ctk.CTkProgressBar(f, fg_color=BG_CARD, progress_color=TEAL, height=8)
        self.progress_bar.set(0)
        self.progress_label = ctk.CTkLabel(f, text="", font=ctk.CTkFont(size=16), text_color=TEXT_SEC)

        # Action buttons
        action_frame = ctk.CTkFrame(f, fg_color="transparent")
        action_frame.grid(row=5, column=0, sticky="ew", padx=20, pady=(8, 0))
        action_frame.grid_columnconfigure(1, weight=1)

        self.refresh_btn = ctk.CTkButton(action_frame, text="Refresh", width=80,
                                          fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
                                          command=self._refresh)
        self.refresh_btn.grid(row=0, column=0, sticky="w")

        self.download_btn = ctk.CTkButton(action_frame, text="Download Selected", width=160,
                                           fg_color=TEAL, hover_color=TEAL_HOVER,
                                           text_color="white", command=self._download)
        self.download_btn.grid(row=0, column=1)

        self.delete_btn = ctk.CTkButton(action_frame, text="Delete Selected", width=120,
                                         fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
                                         command=self._delete_selected)
        self.delete_btn.grid(row=0, column=2, sticky="e")

        # Auto-delete checkbox
        self.auto_delete_cb = ctk.CTkCheckBox(
            f, text="Auto-delete from camera after download",
            variable=self.auto_delete_var,
            font=ctk.CTkFont(size=16), text_color=TEXT_SEC,
            fg_color=TEAL, hover_color=TEAL_HOVER,
            border_color=BORDER, checkmark_color="white"
        )
        self.auto_delete_cb.grid(row=6, column=0, sticky="w", padx=24, pady=(6, 0))

        # Separator
        sep = ctk.CTkFrame(f, height=1, fg_color=BORDER)
        sep.grid(row=7, column=0, sticky="ew", padx=20, pady=(12, 0))

        # Naming config
        naming_frame = ctk.CTkFrame(f, fg_color="transparent")
        naming_frame.grid(row=8, column=0, sticky="ew", padx=20, pady=(8, 0))

        ctk.CTkLabel(naming_frame, text="Naming", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")

        scheme_values = [f"{s.display_name}  ({s.example(self.camera.naming_prefix)})" for s in NamingScheme]
        self.scheme_var = ctk.StringVar(value=scheme_values[list(NamingScheme).index(self.camera.naming_scheme)])
        self.scheme_menu = ctk.CTkOptionMenu(
            naming_frame, values=scheme_values, variable=self.scheme_var,
            width=320, height=34, font=ctk.CTkFont(size=16),
            fg_color=BTN_SEC, text_color=TEXT_PRI,
            button_color=ORANGE, button_hover_color=ORANGE_HOVER,
            command=self._scheme_changed
        )
        self.scheme_menu.pack(side="left", padx=(8, 0))

        prefix_frame = ctk.CTkFrame(f, fg_color="transparent")
        prefix_frame.grid(row=9, column=0, sticky="ew", padx=20, pady=(4, 0))

        self.prefix_label_w = ctk.CTkLabel(prefix_frame, text="Custom:", font=ctk.CTkFont(size=16),
                                            text_color=TEXT_SEC)
        self.prefix_label_w.pack(side="left")

        self.prefix_entry = ctk.CTkEntry(prefix_frame, width=150, height=32,
                                          font=ctk.CTkFont(family="Courier", size=16))
        self.prefix_entry.pack(side="left", padx=(4, 0))
        self.prefix_entry.insert(0, self.camera.naming_prefix)
        self.prefix_entry.bind("<Return>", lambda e: self._prefix_changed())
        self.prefix_entry.bind("<FocusOut>", lambda e: self._prefix_changed())

        self.example_label = ctk.CTkLabel(prefix_frame, text="", font=ctk.CTkFont(family="Courier", size=14),
                                           text_color=TEXT_MUTED)
        self.example_label.pack(side="left", padx=(8, 0))
        self._update_example()

        # Separator 2
        sep2 = ctk.CTkFrame(f, height=1, fg_color=BORDER)
        sep2.grid(row=10, column=0, sticky="ew", padx=20, pady=(12, 0))

        # Bottom buttons
        bottom_frame = ctk.CTkFrame(f, fg_color="transparent")
        bottom_frame.grid(row=11, column=0, sticky="ew", padx=20, pady=(10, 16))
        bottom_frame.grid_columnconfigure(1, weight=1)

        self.clean_btn = ctk.CTkButton(bottom_frame, text="Delete Synced from Camera", width=240, height=36,
                                        fg_color="transparent", border_width=1, border_color=BORDER,
                                        text_color=TEXT_PRI, hover_color=BTN_SEC,
                                        font=ctk.CTkFont(size=15),
                                        command=self._clean)
        self.clean_btn.grid(row=0, column=0, sticky="w")

        self.wipe_btn = ctk.CTkButton(bottom_frame, text="Wipe All Camera Files", width=200, height=36,
                                       fg_color="transparent", border_width=1, border_color=RED,
                                       text_color=RED, hover_color=("#ffe0de", "#3a1515"),
                                       font=ctk.CTkFont(size=15),
                                       command=self._wipe)
        self.wipe_btn.grid(row=0, column=2, sticky="e")

    # --- UI state ---

    def _show_disconnected(self):
        self.files_frame.grid_forget()
        self.instruction_frame.grid(row=0, column=0, sticky="nsew")
        self.accent_strip.configure(fg_color=TEAL)
        self.table_frame.configure(border_width=0)

    def _show_connected(self):
        self.instruction_frame.grid_forget()
        self.files_frame.grid(row=0, column=0, sticky="nsew")
        self.accent_strip.configure(fg_color=ORANGE)
        self.table_frame.configure(border_width=2, border_color=ORANGE)

    def _update_status(self, text: str, connected: Optional[bool] = None):
        self.status_label.configure(text=text)
        if connected is not None:
            self.status_dot.configure(text_color=GREEN if connected else RED)

    def _start_connect_spinner(self):
        """Show an animated spinner on the instruction overlay during connect."""
        if not hasattr(self, '_spinner_label'):
            self._spinner_label = ctk.CTkLabel(
                self.instruction_frame, text="",
                font=ctk.CTkFont(size=16), text_color=TEAL
            )
        self._spinner_label.place(relx=0.5, rely=0.72, anchor="center")
        self._spinner_frames = ["Connecting ●○○", "Connecting ○●○", "Connecting ○○●"]
        self._spinner_idx = 0
        self._spinner_running = True
        self._animate_spinner()

    def _animate_spinner(self):
        if not self._spinner_running:
            return
        self._spinner_label.configure(text=self._spinner_frames[self._spinner_idx])
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
        self.after(400, self._animate_spinner)

    def _stop_connect_spinner(self):
        self._spinner_running = False
        if hasattr(self, '_spinner_label'):
            self._spinner_label.place_forget()

    def _short_path(self, path: Path) -> str:
        home = str(Path.home())
        s = str(path)
        if s.startswith(home):
            s = "~" + s[len(home):]
        if len(s) > 50:
            parts = s.split(os.sep)
            if len(parts) > 4:
                s = os.sep.join(parts[:2]) + "/.../" + os.sep.join(parts[-2:])
        return s

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for i, f in enumerate(self.files):
            check = "\u2611" if f.selected else "\u2610"
            save_as = f.local_name if f.is_renamed else "\u2014"
            type_str = "Video" if f.is_video else "Photo"
            status_str = "Synced" if f.is_downloaded else "New"

            tags = []
            if f.is_downloaded:
                tags.append("synced")
            else:
                tags.append("new")
            if i % 2 == 1:
                tags.append("stripe")

            self.tree.insert("", "end", iid=str(i),
                             values=(check, f.name, save_as, f.size_string, type_str, status_str),
                             tags=tuple(tags))

    def _update_summary(self):
        total = len(self.files)
        downloaded = sum(1 for f in self.files if f.is_downloaded)
        new_count = total - downloaded
        selected = sum(1 for f in self.files if f.selected)
        videos = sum(1 for f in self.files if f.is_video)
        photos = total - videos
        total_bytes = sum(f.size_bytes for f in self.files)
        total_mb = total_bytes / 1_048_576
        size_str = f"{total_mb / 1024:.1f} GB" if total_mb >= 1000 else f"{total_mb:.0f} MB"
        self.summary_label.configure(
            text=f"{total} files ({videos} vid, {photos} photo, {size_str})  \u2022  "
                 f"{downloaded} synced  \u2022  {new_count} new  \u2022  {selected} selected"
        )

    # --- Tree click handling ---

    def _on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        idx = int(row_id)
        # Click on any column toggles the checkbox
        if col == "#1":  # check column
            if 0 <= idx < len(self.files):
                self.files[idx].selected = not self.files[idx].selected
                check = "\u2611" if self.files[idx].selected else "\u2610"
                vals = list(self.tree.item(row_id, "values"))
                vals[0] = check
                self.tree.item(row_id, values=vals)
                self._update_summary()

    # --- Actions ---

    def _sync_ip(self):
        raw = self.ip_entry.get().strip()
        if not raw:
            return
        if raw.isdigit() and 0 <= int(raw) <= 255:
            raw = f"192.168.1.{raw}"
            self.ip_entry.delete(0, "end")
            self.ip_entry.insert(0, raw)
        self.camera.camera_ip = raw
        self.camera.save_settings()

    def _connect(self):
        if self.is_connected:
            # Cancel any in-progress download before disconnecting
            if self.is_downloading:
                self._download_cancel = True
                self.is_downloading = False
                self._re_enable_buttons()
                self._hide_progress()
            self.camera.cleanup()
            self.is_connected = False
            self.files = []
            self._show_disconnected()
            self._update_status("Disconnected", connected=False)
            self.connect_btn.configure(text="Connect")
            return

        self._sync_ip()
        self.connect_btn.configure(state="disabled")
        self._update_status("Connecting...", connected=None)
        self._start_connect_spinner()

        def do_connect():
            reachable = self.camera.is_reachable()
            if not reachable:
                self.after(0, lambda: self._connect_failed())
                return
            self.after(0, lambda: self._update_status("Starting camera server..."))
            self.camera.start_httpd()
            self.after(0, lambda: self._connect_success())

        threading.Thread(target=do_connect, daemon=True).start()

    def _connect_failed(self):
        self._stop_connect_spinner()
        self.connect_btn.configure(state="normal")
        self._update_status("Not found", connected=False)
        messagebox.showwarning("Camera Not Found",
                               f"Could not reach the camera at {self.camera.camera_ip}.\n\n"
                               "Make sure:\n1. The camera app is open and streaming\n"
                               "2. You're on the same Wi-Fi network\n"
                               "3. The camera is powered on")

    def _connect_success(self):
        self._stop_connect_spinner()
        self.is_connected = True
        self.connect_btn.configure(state="normal", text="Disconnect")
        self._update_status("Connected", connected=True)
        self._show_connected()
        self._refresh()

    def _refresh(self):
        if not self.is_connected:
            return
        self._disable_buttons()
        self._update_status("Scanning camera...")

        # Show indeterminate progress bar while scanning
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 0))
        self.progress_bar.start()
        self.progress_label.configure(text="Scanning camera for files\u2026")
        self.progress_label.grid(row=4, column=0, sticky="w", padx=20, pady=(2, 0))

        def do_refresh():
            found = self.camera.list_files()
            self.after(0, lambda: self._refresh_done(found))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _refresh_done(self, found: List[CameraFile]):
        # Stop the scanning progress bar
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(0)
        self.progress_bar.grid_forget()
        self.progress_label.grid_forget()

        self.files = found
        self._refresh_table()
        self._update_summary()
        self._re_enable_buttons()
        self._update_status("Connected (no files found)" if not found else "Connected", connected=True)

    def _download(self):
        if self.is_downloading:
            return
        selected = [(i, f) for i, f in enumerate(self.files) if f.selected]
        if not selected:
            total = len(self.files)
            downloaded = sum(1 for f in self.files if f.is_downloaded)
            if total == 0:
                msg = "No files were found on the camera. Try clicking Refresh."
            elif downloaded == total:
                msg = f"All {total} files are already synced. Click \"Select All\" to re-download."
            else:
                msg = "Select files to download by checking the boxes in the list."
            messagebox.showinfo("Nothing Selected", msg)
            return

        self.is_downloading = True
        self._download_cancel = False
        self._disable_buttons()
        self.progress_bar.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 0))
        self.progress_label.grid(row=4, column=0, sticky="w", padx=20, pady=(2, 0))
        self.progress_bar.set(0)

        total = len(selected)

        def download_seq():
            completed_count = 0
            auto_deleted_count = 0
            connection_lost = False
            for completed, (idx, file) in enumerate(selected):
                # Check cancel flag before each file
                if self._download_cancel:
                    self.camera.log("[DOWNLOAD] Cancelled by user")
                    break

                dl_name = f"{file.name} -> {file.local_name}" if file.is_renamed else file.local_name
                self.after(0, lambda n=dl_name, c=completed: self.progress_label.configure(
                    text=f"Downloading {n} ({c + 1}/{total})..."))

                def prog(pct, c=completed):
                    overall = (c + pct) / total
                    self.after(0, lambda v=overall: self.progress_bar.set(v))

                done_event = threading.Event()
                success_flag = [False]

                def done(ok, flag=success_flag, ev=done_event):
                    flag[0] = ok
                    ev.set()

                self.camera.download_file(file, prog, done,
                                          cancel_check=lambda: self._download_cancel)
                # Wait with timeout — don't hang forever on connection loss
                done_event.wait(timeout=120)

                if not done_event.is_set():
                    # Timed out — connection is probably lost
                    self.camera.log(f"[DOWNLOAD] {file.name} timed out")
                    connection_lost = True
                    break

                if self._download_cancel:
                    break

                if success_flag[0]:
                    completed_count += 1
                    self.files[idx].is_downloaded = True
                    self.files[idx].selected = False
                    self.after(0, self._refresh_table)

                    # Per-file auto-delete: remove from camera right after download
                    if self.auto_delete_var.get():
                        self.after(0, lambda n=file.name, c=completed:
                            self.progress_label.configure(
                                text=f"Auto-deleting {n} from camera ({c + 1}/{total})..."))
                        self.camera.delete_file(file)
                        auto_deleted_count += 1
                        self.camera.log(f"[AUTO-DELETE] {file.name} deleted from camera")
                else:
                    # Download failed — check if camera is still reachable
                    if not self.camera.is_reachable():
                        connection_lost = True
                        self.camera.log("[DOWNLOAD] Connection lost mid-download")
                        break

            self.after(0, lambda: self._download_done(
                completed_count, connection_lost=connection_lost,
                auto_deleted=auto_deleted_count))

        threading.Thread(target=download_seq, daemon=True).start()

    def _download_done(self, total: int, connection_lost: bool = False,
                        auto_deleted: int = 0):
        self.is_downloading = False
        self._download_cancel = False
        self._re_enable_buttons()

        if connection_lost:
            self.progress_bar.set(0)
            msg = f"Downloaded {total} files before connection was lost."
            self.progress_label.configure(text=msg)
            self._update_status("Connection lost", connected=False)
            self.is_connected = False
            self.camera.cleanup()
            self.connect_btn.configure(text="Connect")
            self.accent_strip.configure(fg_color=TEAL)
            self.table_frame.configure(border_width=0)
            self._refresh_table()
            self._update_summary()
            self.after(8000, self._hide_progress)
        elif auto_deleted > 0:
            # Auto-delete happened — refresh the camera file list
            self.progress_bar.set(1.0)
            msg = f"Downloaded {total} files, auto-deleted {auto_deleted} from camera."
            self.progress_label.configure(text=msg)
            self._update_status("Refreshing...")
            self._refresh_table()
            self._update_summary()

            # Transition to indeterminate for refresh
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start()
            self.progress_label.configure(text=f"{msg} Refreshing\u2026")

            def do_refresh():
                found = self.camera.list_files()
                self.after(0, lambda: self._download_auto_delete_refresh_done(
                    found, total, auto_deleted))

            threading.Thread(target=do_refresh, daemon=True).start()
        else:
            self.progress_bar.set(1.0)
            self.progress_label.configure(text=f"Done! Downloaded {total} files.")
            self._refresh_table()
            self._update_summary()
            self.after(4000, self._hide_progress)

    def _download_auto_delete_refresh_done(self, found: list, downloaded: int,
                                            deleted: int):
        """Post-auto-delete refresh: update file list and show final status."""
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0)
        self.progress_label.configure(
            text=f"Done! Downloaded {downloaded} files, deleted {deleted} from camera.")
        self._update_status("Connected", connected=True)
        self.files = found
        self._refresh_table()
        self._update_summary()
        self.after(4000, self._hide_progress)

    def _hide_progress(self):
        self.progress_bar.grid_forget()
        self.progress_label.grid_forget()

    def _re_enable_buttons(self):
        """Centralized helper to re-enable all action buttons."""
        self.download_btn.configure(state="normal")
        self.refresh_btn.configure(state="normal")
        self.clean_btn.configure(state="normal")
        self.wipe_btn.configure(state="normal")
        self.delete_btn.configure(state="normal")
        self.auto_delete_cb.configure(state="normal")

    def _disable_buttons(self):
        """Centralized helper to disable all action buttons during operations."""
        self.download_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.clean_btn.configure(state="disabled")
        self.wipe_btn.configure(state="disabled")
        self.delete_btn.configure(state="disabled")
        self.auto_delete_cb.configure(state="disabled")

    def _delete_selected(self):
        """Delete checked/selected files from camera with progress bar."""
        selected = [f for f in self.files if f.selected]
        if not selected:
            messagebox.showinfo("Nothing Selected", "Select files to delete by checking the boxes in the list.")
            return
        if not messagebox.askyesno("Delete Selected Files?",
                                    f"Delete {len(selected)} selected files from the camera?\n"
                                    "This cannot be undone."):
            return
        self._do_delete(selected)

    def _do_delete(self, files_to_delete: list, status_prefix: str = "Deleting"):
        """Delete files from camera with progress bar feedback, then auto-refresh."""
        self._disable_buttons()
        total = len(files_to_delete)
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(0)
        self.progress_bar.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 0))
        self.progress_label.grid(row=4, column=0, sticky="w", padx=20, pady=(2, 0))
        self._update_status(f"{status_prefix}...")

        def do_delete():
            for i, f in enumerate(files_to_delete):
                def update_ui(name=f.name, n=i):
                    self.progress_label.configure(
                        text=f"{status_prefix} {name} ({n + 1}/{total})...")
                    self.progress_bar.set((n + 1) / total)
                self.after(0, update_ui)
                self.camera.delete_file(f)
            self.after(0, lambda: self._delete_done(total))

        threading.Thread(target=do_delete, daemon=True).start()

    def _delete_done(self, count: int):
        """Post-delete: show done briefly, then refresh file list."""
        self.progress_bar.set(1.0)
        self.progress_label.configure(text=f"Deleted {count} files. Refreshing...")
        self._update_status("Refreshing...")

        # Transition to indeterminate for the refresh
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()

        def do_refresh():
            found = self.camera.list_files()
            self.after(0, lambda: self._delete_refresh_done(found, count))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _delete_refresh_done(self, found: list, deleted_count: int):
        """Post-delete refresh complete: update UI and re-enable."""
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0)
        self.progress_label.configure(text=f"Done! Deleted {deleted_count} files from camera.")
        self._update_status("Connected", connected=True)

        self.files = found
        self._refresh_table()
        self._update_summary()
        self._re_enable_buttons()
        self.after(4000, self._hide_progress)

    def _clean(self):
        downloaded = [f for f in self.files if f.is_downloaded]
        if not downloaded:
            messagebox.showinfo("Nothing to Delete", "No synced files to remove from camera.")
            return
        if not messagebox.askyesno("Delete Synced Files?",
                                    f"Delete {len(downloaded)} synced files from the camera?\n"
                                    "These have already been downloaded to your computer."):
            return
        self._do_delete(downloaded, status_prefix="Deleting synced")

    def _wipe(self):
        if not messagebox.askyesno("Wipe ALL Files?",
                                    "This will permanently delete ALL videos and photos\n"
                                    "from the camera. This cannot be undone.",
                                    icon="warning"):
            return
        if not messagebox.askyesno("Are you sure?",
                                    "All camera files will be permanently deleted.",
                                    icon="warning"):
            return
        self._disable_buttons()
        self._update_status("Wiping all files...")
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 0))
        self.progress_bar.start()
        self.progress_label.configure(text="Wiping all camera files\u2026")
        self.progress_label.grid(row=4, column=0, sticky="w", padx=20, pady=(2, 0))

        def do_wipe():
            self.camera.wipe_all()
            self.after(0, self._wipe_done)

        threading.Thread(target=do_wipe, daemon=True).start()

    def _wipe_done(self):
        self.progress_label.configure(text="Wipe complete. Refreshing\u2026")
        self._update_status("Refreshing...")

        def do_refresh():
            found = self.camera.list_files()
            self.after(0, lambda: self._wipe_refresh_done(found))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _wipe_refresh_done(self, found: list):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0)
        self.progress_label.configure(text="Done! All camera files wiped.")
        self._update_status("Connected", connected=True)

        self.files = found
        self._refresh_table()
        self._update_summary()
        self._re_enable_buttons()
        self.after(4000, self._hide_progress)

    def _select_all(self):
        for f in self.files:
            f.selected = True
        self._refresh_table()
        self._update_summary()

    def _select_new(self):
        for f in self.files:
            f.selected = not f.is_downloaded
        self._refresh_table()
        self._update_summary()

    def _change_folder(self):
        path = filedialog.askdirectory(initialdir=str(self.camera.video_dir),
                                       title="Select download folder")
        if path:
            self.camera.change_video_dir(Path(path))
            self.folder_btn.configure(text=self._short_path(self.camera.video_dir))

    def _open_folder(self):
        path = str(self.camera.video_dir)
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["open", path])
        elif system == "Windows":
            os.startfile(path)
        else:
            subprocess.run(["xdg-open", path])

    def _find_camera(self):
        self.find_btn.configure(state="disabled")

        def do_find():
            result = self.camera.find_camera(
                lambda msg: self.after(0, lambda m=msg: self._update_status(m))
            )
            ip, method = result if result else (None, "")
            self.after(0, lambda: self._find_done(ip, method))

        threading.Thread(target=do_find, daemon=True).start()

    def _find_done(self, ip: Optional[str], method: str):
        self.find_btn.configure(state="normal")
        self._update_status("Disconnected", connected=False)
        if ip:
            self.ip_entry.delete(0, "end")
            self.ip_entry.insert(0, ip)
            self.camera.camera_ip = ip
            self.camera.save_settings()
            messagebox.showinfo("Camera Found!", f"Found camera at {ip}\n(Detected via {method})")
        else:
            messagebox.showwarning("Camera Not Found",
                                    "No camera found on your network.\n\n"
                                    "Make sure:\n1. The camera app is open and streaming\n"
                                    "2. You're on the same Wi-Fi network")

    def _scheme_changed(self, value: str):
        schemes = list(NamingScheme)
        values = [f"{s.display_name}  ({s.example(self.camera.naming_prefix)})" for s in schemes]
        try:
            idx = values.index(value)
            self.camera.naming_scheme = schemes[idx]
            self.camera.save_settings()
        except ValueError:
            pass

        is_original = self.camera.naming_scheme == NamingScheme.ORIGINAL
        state = "disabled" if is_original else "normal"
        self.prefix_entry.configure(state=state)

        self._update_example()

        if self.files and self.is_connected:
            self._refresh()

    def _prefix_changed(self):
        raw = self.prefix_entry.get().strip().lower()
        raw = raw.replace(" ", "_")
        raw = re.sub(r'[^a-z0-9_\-]', '', raw)
        if not raw:
            raw = "cam"
        self.prefix_entry.delete(0, "end")
        self.prefix_entry.insert(0, raw)
        self.camera.naming_prefix = raw
        self.camera.save_settings()
        self._update_scheme_menu()
        self._update_example()
        if self.files and self.is_connected:
            self._refresh()

    def _update_scheme_menu(self):
        values = [f"{s.display_name}  ({s.example(self.camera.naming_prefix)})" for s in NamingScheme]
        self.scheme_menu.configure(values=values)
        idx = list(NamingScheme).index(self.camera.naming_scheme)
        self.scheme_var.set(values[idx])

    def _update_example(self):
        ex = self.camera.naming_scheme.example(self.camera.naming_prefix)
        self.example_label.configure(text=f"e.g. {ex}")

    def _show_about(self):
        about = ctk.CTkToplevel(self)
        about.title("About HumDrop")
        about.geometry("440x540")
        about.resizable(False, False)
        about.transient(self)
        about.grab_set()

        # Teal header banner
        banner = ctk.CTkFrame(about, height=130, fg_color=TEAL_DARK, corner_radius=0)
        banner.pack(fill="x")
        banner.pack_propagate(False)

        banner_inner = ctk.CTkFrame(banner, fg_color="transparent")
        banner_inner.place(relx=0.5, rely=0.5, anchor="center")

        bird_c = self._create_bird_canvas(banner_inner, size=64)
        bird_c.pack()

        ctk.CTkLabel(banner_inner, text="HumDrop",
                     font=ctk.CTkFont(size=26, weight="bold"),
                     text_color="white").pack(pady=(2, 0))
        ctk.CTkLabel(banner_inner, text="v0.05  \u2022  Camera Sync",
                     font=ctk.CTkFont(size=15),
                     text_color=TEAL_HOVER).pack()

        # Body
        body = ctk.CTkFrame(about, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(16, 0))

        ctk.CTkLabel(body, text="By Kenneth Russell DeGraff",
                     font=ctk.CTkFont(size=16, weight="bold")).pack()
        ctk.CTkLabel(body, text="Sync videos and photos from WiFi trail cameras\n"
                     "directly to your computer over your local network.",
                     font=ctk.CTkFont(size=15), text_color=TEXT_SEC,
                     justify="center").pack(pady=(6, 0))

        # Info card
        info_card = ctk.CTkFrame(body, fg_color=BG_HEADER, corner_radius=8)
        info_card.pack(fill="x", pady=(12, 0))

        info_inner = ctk.CTkFrame(info_card, fg_color="transparent")
        info_inner.pack(padx=16, pady=10)

        ctk.CTkLabel(info_inner, text=f"Save folder:  {self._short_path(self.camera.video_dir)}",
                     font=ctk.CTkFont(family="Courier", size=14),
                     text_color=TEXT_SEC).pack(anchor="w")
        ctk.CTkLabel(info_inner, text=f"Camera IP:    {self.camera.camera_ip}",
                     font=ctk.CTkFont(family="Courier", size=14),
                     text_color=TEXT_SEC).pack(anchor="w")

        # Buttons
        btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        btn_frame.pack(pady=(16, 0))

        ctk.CTkButton(btn_frame, text="\u2B50  GitHub  \u2014  Report Bugs & Contribute",
                      width=290, height=38,
                      fg_color=TEAL, hover_color=TEAL_HOVER,
                      text_color="white", font=ctk.CTkFont(size=15),
                      command=lambda: webbrowser.open(
                          "https://github.com/Fe2-O3/HumDrop")
                      ).pack(pady=(0, 6))

        ctk.CTkButton(btn_frame, text="\u2615  Support on Ko-fi",
                      width=290, height=38,
                      fg_color=ORANGE, hover_color=ORANGE_HOVER,
                      text_color="white", font=ctk.CTkFont(size=15),
                      command=lambda: webbrowser.open(
                          "https://ko-fi.com/fe2_o3")
                      ).pack()

        ctk.CTkLabel(body, text="Feedback, bug reports, feature requests,\n"
                     "and code contributions welcome on GitHub!",
                     font=ctk.CTkFont(size=14), text_color=TEXT_MUTED,
                     justify="center").pack(pady=(10, 0))

        # Close button
        ctk.CTkButton(about, text="Close", width=110, height=36,
                      fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER,
                      text_color=TEXT_PRI, font=ctk.CTkFont(size=15),
                      command=about.destroy).pack(pady=(8, 16))

    def _on_close(self):
        self.camera.cleanup()
        self.destroy()


# ============================================================
# MARK: - Main
# ============================================================

if __name__ == "__main__":
    app = HumDropApp()
    app.mainloop()
