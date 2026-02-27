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
import time
import socket
import shutil
import platform
import subprocess
import threading
import webbrowser
import urllib.request
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
    ORIGINAL = "original"

    @property
    def display_name(self):
        return {
            NamingScheme.PREFIX_DATE: "prefix_date_seq",
            NamingScheme.TIMESTAMP_FULL: "PREFIX_timestamp",
            NamingScheme.DATE_PREFIX: "date_prefix",
            NamingScheme.ORIGINAL: "Camera original",
        }[self]

    def example(self, prefix: str) -> str:
        p = prefix or "cam"
        short = p[:3].upper()
        return {
            NamingScheme.PREFIX_DATE: f"{p}_2026-01-31_001.mp4",
            NamingScheme.TIMESTAMP_FULL: f"{short}_20260131_140144.mp4",
            NamingScheme.DATE_PREFIX: f"2026-01-31_14-01_{p}.mp4",
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
        self.naming_prefix = "hummingbird"
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

    def _find_next_counter(self, date: datetime, ext: str) -> int:
        date_str = f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
        max_counter = 0
        try:
            for entry in self.video_dir.iterdir():
                if date_str in entry.name and entry.name.endswith(f".{ext}"):
                    m = re.search(r'_(\d{3})\.' + ext + r'$', entry.name)
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
                date_key = f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
                if date_key not in date_counters:
                    date_counters[date_key] = self._find_next_counter(date, ext)
                counter = date_counters[date_key]
                date_counters[date_key] = counter + 1
                f.local_name = self.naming_scheme.generate_name(self.naming_prefix, date, ext, counter)

            self.log(f"[RESOLVE] {f.name} -> {f.local_name} (new)")

    # --- Download ---

    def download_file(self, file: CameraFile, progress_cb: Callable, done_cb: Callable):
        url = f"http://{self.camera_ip}:{self.http_port}/{file.http_path}"
        dest = self.video_dir / file.local_name
        try:
            with urllib.request.urlopen(url) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as out:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            progress_cb(downloaded / total)

            if file.remote_timestamp:
                ts = file.remote_timestamp.timestamp()
                os.utime(dest, (ts, ts))

            done_cb(dest.stat().st_size > 0)
        except Exception as e:
            self.log(f"[DOWNLOAD] {file.name} error: {e}")
            done_cb(False)

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
GREEN = "#4DD974"
RED = "#F24D40"
BG_DARK = "#1a1a1a"
BG_CARD = "#2b2b2b"
BG_HEADER = "#222222"
TEXT_SEC = "#b0b0b0"       # secondary labels — readable on dark bg
TEXT_MUTED = "#909090"     # muted/example text — still legible


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

        self.title("HumDrop")
        self.geometry("720x800")
        self.minsize(600, 650)
        ctk.set_appearance_mode("dark")

        self._build_ui()
        self._show_disconnected()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Keyboard shortcuts
        self.bind_all("<Command-r>", lambda e: self._refresh())
        self.bind_all("<Control-r>", lambda e: self._refresh())
        self.bind_all("<Command-o>", lambda e: self._open_folder())
        self.bind_all("<Control-o>", lambda e: self._open_folder())

    def _build_ui(self):
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # --- Accent strip ---
        accent = ctk.CTkFrame(self, height=4, fg_color=TEAL, corner_radius=0)
        accent.grid(row=0, column=0, sticky="ew")

        # --- Header ---
        header = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0)
        header.grid(row=1, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        # Title row
        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.grid(row=0, column=0, columnspan=3, sticky="w", padx=20, pady=(12, 0))

        ctk.CTkLabel(title_frame, text="HumDrop", font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkLabel(title_frame, text="Camera Sync", font=ctk.CTkFont(size=12),
                     text_color=TEAL).pack(side="left", padx=(8, 0), pady=(4, 0))

        # Right side buttons
        btn_frame = ctk.CTkFrame(header, fg_color="transparent")
        btn_frame.grid(row=0, column=2, rowspan=2, sticky="ne", padx=20, pady=(10, 0))

        self.connect_btn = ctk.CTkButton(btn_frame, text="Connect", width=100, fg_color=TEAL,
                                          hover_color=TEAL_HOVER, command=self._connect)
        self.connect_btn.pack(pady=(0, 4))

        ctk.CTkButton(btn_frame, text="About", width=100, fg_color="transparent",
                      border_width=1, border_color=TEXT_SEC, hover_color=BG_CARD,
                      command=self._show_about).pack()

        # IP row
        ip_frame = ctk.CTkFrame(header, fg_color="transparent")
        ip_frame.grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(6, 0))

        ctk.CTkLabel(ip_frame, text="Camera IP", font=ctk.CTkFont(size=13),
                     text_color=TEXT_SEC).pack(side="left")

        self.ip_entry = ctk.CTkEntry(ip_frame, width=140, font=ctk.CTkFont(family="Courier", size=12))
        self.ip_entry.pack(side="left", padx=(8, 4))
        self.ip_entry.insert(0, self.camera.camera_ip)

        self.find_btn = ctk.CTkButton(ip_frame, text="Find", width=50, height=28,
                                       fg_color=BG_CARD, hover_color=TEXT_MUTED,
                                       command=self._find_camera)
        self.find_btn.pack(side="left", padx=(0, 10))

        # Status dot + label
        self.status_dot = ctk.CTkLabel(ip_frame, text="\u25CF", font=ctk.CTkFont(size=14),
                                        text_color=RED, width=16)
        self.status_dot.pack(side="left")

        self.status_label = ctk.CTkLabel(ip_frame, text="Disconnected", font=ctk.CTkFont(size=13),
                                          text_color=TEXT_SEC)
        self.status_label.pack(side="left", padx=(4, 0))

        # Folder row
        folder_frame = ctk.CTkFrame(header, fg_color="transparent")
        folder_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=20, pady=(6, 12))
        folder_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(folder_frame, text="Save to:", font=ctk.CTkFont(size=12),
                     text_color=TEXT_SEC).grid(row=0, column=0, sticky="w")

        self.folder_btn = ctk.CTkButton(
            folder_frame, text=self._short_path(self.camera.video_dir),
            font=ctk.CTkFont(family="Courier", size=13), anchor="w",
            fg_color=BG_CARD, hover_color=TEXT_MUTED, height=28,
            command=self._change_folder
        )
        self.folder_btn.grid(row=0, column=1, sticky="ew", padx=(8, 0))

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

        ctk.CTkLabel(inner, text="\U0001F426", font=ctk.CTkFont(size=48)).pack(pady=(0, 16))
        ctk.CTkLabel(inner, text="To get started:\n\n"
                     "1.  Open the camera app on your phone\n"
                     "2.  Start the camera stream\n"
                     "3.  Make sure you're on the same Wi-Fi network\n"
                     '4.  Enter the camera IP, or tap "Find" to auto-discover,\n'
                     '     then "Connect"',
                     font=ctk.CTkFont(size=13), text_color=TEXT_SEC,
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

        ctk.CTkLabel(hdr, text="Files", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w")

        btn_row = ctk.CTkFrame(hdr, fg_color="transparent")
        btn_row.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(btn_row, text="Select All", width=85, height=28, font=ctk.CTkFont(size=13),
                      fg_color=BG_CARD, hover_color=TEXT_MUTED,
                      command=self._select_all).pack(side="left", padx=(0, 4))
        ctk.CTkButton(btn_row, text="Select New", width=85, height=28, font=ctk.CTkFont(size=13),
                      fg_color=BG_CARD, hover_color=TEXT_MUTED,
                      command=self._select_new).pack(side="left")

        # Table (ttk.Treeview with dark styling)
        table_frame = ctk.CTkFrame(f, fg_color=BG_CARD, corner_radius=8)
        table_frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=(8, 0))
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.Treeview",
                        background=BG_CARD, foreground="white", fieldbackground=BG_CARD,
                        rowheight=30, borderwidth=0, relief="flat", font=("", 13))
        style.configure("Dark.Treeview.Heading",
                        background="#383838", foreground="#e0e0e0",
                        borderwidth=0, relief="flat", font=("", 13, "bold"))
        style.map("Dark.Treeview",
                  background=[("selected", TEAL_DARK)],
                  foreground=[("selected", "white")])
        style.layout("Dark.Treeview", [("Dark.Treeview.treearea", {"sticky": "nsew"})])

        cols = ("check", "camera", "saveas", "size", "type", "status")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings",
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

        self.tree.tag_configure("new", foreground=GREEN)
        self.tree.tag_configure("synced", foreground=TEXT_SEC)
        self.tree.tag_configure("video_type", foreground="#5B9BD5")
        self.tree.tag_configure("photo_type", foreground="#ED7D31")
        self.tree.tag_configure("stripe", background="#2f2f2f")

        scrollbar = ctk.CTkScrollbar(table_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4)

        self.tree.bind("<Button-1>", self._on_tree_click)

        # Summary
        self.summary_label = ctk.CTkLabel(f, text="", font=ctk.CTkFont(size=13), text_color=TEXT_SEC)
        self.summary_label.grid(row=2, column=0, sticky="w", padx=20, pady=(4, 0))

        # Progress
        self.progress_bar = ctk.CTkProgressBar(f, fg_color=BG_CARD, progress_color=TEAL, height=8)
        self.progress_bar.set(0)
        self.progress_label = ctk.CTkLabel(f, text="", font=ctk.CTkFont(size=13), text_color=TEXT_SEC)

        # Action buttons
        action_frame = ctk.CTkFrame(f, fg_color="transparent")
        action_frame.grid(row=5, column=0, sticky="ew", padx=20, pady=(8, 0))
        action_frame.grid_columnconfigure(1, weight=1)

        self.refresh_btn = ctk.CTkButton(action_frame, text="Refresh", width=80,
                                          fg_color=BG_CARD, hover_color=TEXT_MUTED,
                                          command=self._refresh)
        self.refresh_btn.grid(row=0, column=0, sticky="w")

        self.download_btn = ctk.CTkButton(action_frame, text="Download Selected", width=150,
                                           fg_color=TEAL, hover_color=TEAL_HOVER,
                                           command=self._download)
        self.download_btn.grid(row=0, column=1)

        self.openfolder_btn = ctk.CTkButton(action_frame, text="Open Folder", width=90,
                                             fg_color=BG_CARD, hover_color=TEXT_MUTED,
                                             command=self._open_folder)
        self.openfolder_btn.grid(row=0, column=2, sticky="e")

        # Separator
        sep = ctk.CTkFrame(f, height=1, fg_color=TEXT_MUTED)
        sep.grid(row=6, column=0, sticky="ew", padx=20, pady=(12, 0))

        # Naming config
        naming_frame = ctk.CTkFrame(f, fg_color="transparent")
        naming_frame.grid(row=7, column=0, sticky="ew", padx=20, pady=(8, 0))

        ctk.CTkLabel(naming_frame, text="Naming", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")

        scheme_values = [f"{s.display_name}  ({s.example(self.camera.naming_prefix)})" for s in NamingScheme]
        self.scheme_var = ctk.StringVar(value=scheme_values[list(NamingScheme).index(self.camera.naming_scheme)])
        self.scheme_menu = ctk.CTkOptionMenu(
            naming_frame, values=scheme_values, variable=self.scheme_var,
            width=280, height=30, font=ctk.CTkFont(size=13),
            fg_color=BG_CARD, button_color=TEXT_MUTED, button_hover_color=TEAL_DARK,
            command=self._scheme_changed
        )
        self.scheme_menu.pack(side="left", padx=(8, 0))

        prefix_frame = ctk.CTkFrame(f, fg_color="transparent")
        prefix_frame.grid(row=8, column=0, sticky="ew", padx=20, pady=(4, 0))

        self.prefix_label_w = ctk.CTkLabel(prefix_frame, text="Prefix:", font=ctk.CTkFont(size=13),
                                            text_color=TEXT_SEC)
        self.prefix_label_w.pack(side="left")

        self.prefix_entry = ctk.CTkEntry(prefix_frame, width=140, height=28,
                                          font=ctk.CTkFont(family="Courier", size=13))
        self.prefix_entry.pack(side="left", padx=(4, 0))
        self.prefix_entry.insert(0, self.camera.naming_prefix)
        self.prefix_entry.bind("<Return>", lambda e: self._prefix_changed())
        self.prefix_entry.bind("<FocusOut>", lambda e: self._prefix_changed())

        self.example_label = ctk.CTkLabel(prefix_frame, text="", font=ctk.CTkFont(family="Courier", size=12),
                                           text_color=TEXT_MUTED)
        self.example_label.pack(side="left", padx=(8, 0))
        self._update_example()

        # Bottom buttons
        bottom_frame = ctk.CTkFrame(f, fg_color="transparent")
        bottom_frame.grid(row=9, column=0, sticky="ew", padx=20, pady=(10, 16))
        bottom_frame.grid_columnconfigure(1, weight=1)

        self.clean_btn = ctk.CTkButton(bottom_frame, text="Delete Downloaded from Camera", width=240, height=32,
                                        fg_color="transparent", border_width=1, border_color=TEXT_SEC,
                                        hover_color=BG_CARD, font=ctk.CTkFont(size=13),
                                        command=self._clean)
        self.clean_btn.grid(row=0, column=0, sticky="w")

        self.wipe_btn = ctk.CTkButton(bottom_frame, text="Wipe All Camera Files", width=180, height=32,
                                       fg_color="transparent", border_width=1, border_color=RED,
                                       text_color=RED, hover_color="#3a1515",
                                       font=ctk.CTkFont(size=13),
                                       command=self._wipe)
        self.wipe_btn.grid(row=0, column=2, sticky="e")

    # --- UI state ---

    def _show_disconnected(self):
        self.files_frame.grid_forget()
        self.instruction_frame.grid(row=0, column=0, sticky="nsew")

    def _show_connected(self):
        self.instruction_frame.grid_forget()
        self.files_frame.grid(row=0, column=0, sticky="nsew")

    def _update_status(self, text: str, connected: Optional[bool] = None):
        self.status_label.configure(text=text)
        if connected is not None:
            self.status_dot.configure(text_color=GREEN if connected else RED)

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
            self.camera.cleanup()
            self.is_connected = False
            self.files = []
            self._show_disconnected()
            self._update_status("Disconnected", connected=False)
            self.connect_btn.configure(text="Connect")
            return

        self._sync_ip()
        self.connect_btn.configure(state="disabled")
        self._update_status("Connecting...")

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
        self.connect_btn.configure(state="normal")
        self._update_status("Not found", connected=False)
        messagebox.showwarning("Camera Not Found",
                               f"Could not reach the camera at {self.camera.camera_ip}.\n\n"
                               "Make sure:\n1. The camera app is open and streaming\n"
                               "2. You're on the same Wi-Fi network\n"
                               "3. The camera is powered on")

    def _connect_success(self):
        self.is_connected = True
        self.connect_btn.configure(state="normal", text="Disconnect")
        self._update_status("Connected", connected=True)
        self._show_connected()
        self._refresh()

    def _refresh(self):
        if not self.is_connected:
            return
        self.refresh_btn.configure(state="disabled")
        self._update_status("Scanning...")

        def do_refresh():
            found = self.camera.list_files()
            self.after(0, lambda: self._refresh_done(found))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _refresh_done(self, found: List[CameraFile]):
        self.files = found
        self._refresh_table()
        self._update_summary()
        self.refresh_btn.configure(state="normal")
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
        self.download_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.clean_btn.configure(state="disabled")
        self.wipe_btn.configure(state="disabled")
        self.progress_bar.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 0))
        self.progress_label.grid(row=4, column=0, sticky="w", padx=20, pady=(2, 0))
        self.progress_bar.set(0)

        total = len(selected)

        def download_seq():
            for completed, (idx, file) in enumerate(selected):
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

                self.camera.download_file(file, prog, done)
                done_event.wait()

                if success_flag[0]:
                    self.files[idx].is_downloaded = True
                    self.files[idx].selected = False
                    self.after(0, self._refresh_table)

            self.after(0, lambda: self._download_done(total))

        threading.Thread(target=download_seq, daemon=True).start()

    def _download_done(self, total: int):
        self.is_downloading = False
        self.download_btn.configure(state="normal")
        self.refresh_btn.configure(state="normal")
        self.clean_btn.configure(state="normal")
        self.wipe_btn.configure(state="normal")
        self.progress_bar.set(1.0)
        self.progress_label.configure(text=f"Done! Downloaded {total} files.")
        self._refresh_table()
        self._update_summary()
        self.after(4000, self._hide_progress)

    def _hide_progress(self):
        self.progress_bar.grid_forget()
        self.progress_label.grid_forget()

    def _clean(self):
        downloaded = [f for f in self.files if f.is_downloaded]
        if not downloaded:
            messagebox.showinfo("Nothing to Clean", "No downloaded files to remove from camera.")
            return
        if not messagebox.askyesno("Delete Downloaded Files?",
                                    f"Delete {len(downloaded)} files from the camera\n"
                                    "that have already been downloaded?"):
            return
        self.clean_btn.configure(state="disabled")
        self._update_status("Cleaning...")

        def do_clean():
            for f in downloaded:
                self.camera.delete_file(f)
                time.sleep(0.5)
            self.after(0, lambda: self._clean_done())

        threading.Thread(target=do_clean, daemon=True).start()

    def _clean_done(self):
        self.clean_btn.configure(state="normal")
        self._update_status("Connected", connected=True)
        self._refresh()

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
        self.wipe_btn.configure(state="disabled")
        self._update_status("Wiping...")

        def do_wipe():
            self.camera.wipe_all()
            self.after(0, lambda: self._wipe_done())

        threading.Thread(target=do_wipe, daemon=True).start()

    def _wipe_done(self):
        self.wipe_btn.configure(state="normal")
        self._update_status("Connected", connected=True)
        self._refresh()

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
        about.geometry("360x340")
        about.resizable(False, False)
        about.transient(self)
        about.grab_set()

        ctk.CTkLabel(about, text="\U0001F426", font=ctk.CTkFont(size=40)).pack(pady=(20, 0))
        ctk.CTkLabel(about, text="HumDrop", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(4, 0))
        ctk.CTkLabel(about, text="v0.05", font=ctk.CTkFont(size=13), text_color=TEXT_SEC).pack()
        ctk.CTkLabel(about, text="By Kenneth Russell DeGraff",
                     font=ctk.CTkFont(size=13)).pack(pady=(8, 0))
        ctk.CTkLabel(about, text="Sync videos and photos from your camera.",
                     font=ctk.CTkFont(size=13), text_color=TEXT_SEC).pack(pady=(12, 0))
        ctk.CTkLabel(about, text=f"Downloads to: {self._short_path(self.camera.video_dir)}",
                     font=ctk.CTkFont(family="Courier", size=12), text_color=TEXT_SEC).pack(pady=(4, 0))
        ctk.CTkLabel(about, text=f"Camera IP: {self.camera.camera_ip}",
                     font=ctk.CTkFont(family="Courier", size=12), text_color=TEXT_SEC).pack()

        kofi_btn = ctk.CTkButton(about, text="Support on Ko-fi", width=140, height=30,
                                  fg_color=TEAL, hover_color=TEAL_HOVER,
                                  command=lambda: webbrowser.open("https://ko-fi.com/fe2_o3"))
        kofi_btn.pack(pady=(16, 0))

        ctk.CTkButton(about, text="OK", width=80, fg_color=BG_CARD, hover_color=TEXT_MUTED,
                      command=about.destroy).pack(pady=(12, 0))

    def _on_close(self):
        self.camera.cleanup()
        self.destroy()


# ============================================================
# MARK: - Main
# ============================================================

if __name__ == "__main__":
    app = HumDropApp()
    app.mainloop()
