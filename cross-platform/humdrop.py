#!/usr/bin/env python3
"""
HumDrop v0.10 — Cross-platform camera sync utility
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
import ipaddress
import platform
import subprocess
import threading
import webbrowser
import urllib.request
import tkinter as tk
from enum import Enum
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Callable, Tuple
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk
import customtkinter as ctk

# Download progress display tuning.
# _SPEED_WINDOW_SEC: how far back the rolling rate looks. Short enough to track
#   the current file, long enough to ride out the gap between files.
# _UI_THROTTLE_SEC: floor on the interval between progress pushes to the Tk
#   event loop. ~7/sec still reads as live and keeps the main thread idle
#   enough that it isn't fighting the download for the GIL.
_SPEED_WINDOW_SEC = 5.0
_UI_THROTTLE_SEC = 0.15

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
            NamingScheme.PREFIX_DATE: "Custom · Date · Seq",
            NamingScheme.TIMESTAMP_FULL: "Custom · Timestamp",
            NamingScheme.DATE_PREFIX: "Date · Time · Custom",
            NamingScheme.DATE_SEQ_CUSTOM: "Date · Seq · Custom",
            NamingScheme.CUSTOM_SEQ: "Custom · Seq",
            NamingScheme.SEQ_CUSTOM: "Seq · Custom",
            NamingScheme.ORIGINAL: "Camera Original",
        }[self]

    def example(self, prefix: str, sep: str = "_") -> str:
        p = prefix or "cam"
        short = p[:3].upper()
        s = sep
        return {
            NamingScheme.PREFIX_DATE: f"{p}{s}2026-01-31{s}001.mp4",
            NamingScheme.TIMESTAMP_FULL: f"{short}{s}20260131{s}140144.mp4",
            NamingScheme.DATE_PREFIX: f"2026-01-31{s}14-01{s}{p}.mp4",
            NamingScheme.DATE_SEQ_CUSTOM: f"2026-01-31{s}001{s}{p}.mp4",
            NamingScheme.CUSTOM_SEQ: f"{p}{s}001.mp4",
            NamingScheme.SEQ_CUSTOM: f"001{s}{p}.mp4",
            NamingScheme.ORIGINAL: "SCKR1000.mp4",
        }[self]

    def generate_name(self, prefix: str, date: Optional[datetime], ext: str,
                      counter: int, sep: str = "_") -> str:
        p = prefix or "cam"
        short = p[:3].upper()
        d = date or datetime.now()
        s = sep
        if self == NamingScheme.PREFIX_DATE:
            return f"{p}{s}{d.year:04d}-{d.month:02d}-{d.day:02d}{s}{counter:03d}.{ext}"
        elif self == NamingScheme.TIMESTAMP_FULL:
            return f"{short}{s}{d.year:04d}{d.month:02d}{d.day:02d}{s}{d.hour:02d}{d.minute:02d}{d.second:02d}.{ext}"
        elif self == NamingScheme.DATE_PREFIX:
            return f"{d.year:04d}-{d.month:02d}-{d.day:02d}{s}{d.hour:02d}-{d.minute:02d}{s}{p}.{ext}"
        elif self == NamingScheme.DATE_SEQ_CUSTOM:
            return f"{d.year:04d}-{d.month:02d}-{d.day:02d}{s}{counter:03d}{s}{p}.{ext}"
        elif self == NamingScheme.CUSTOM_SEQ:
            return f"{p}{s}{counter:03d}.{ext}"
        elif self == NamingScheme.SEQ_CUSTOM:
            return f"{counter:03d}{s}{p}.{ext}"
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
    # OUI prefix of this specific camera's WiFi module — survives DHCP IP changes.
    CAMERA_MAC_PREFIX = "50:5a:65"

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
        self.naming_separator = "_"  # "_" or " "
        self.date_subfolders = False
        self.auto_open_folder = False
        self.last_storage = None  # {"used": bytes, "free": bytes, "total": bytes, "timestamp": str}
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
            sep = data.get("naming_separator", "_")
            self.naming_separator = " " if sep == " " else "_"
            self.date_subfolders = data.get("date_subfolders", False)
            self.auto_open_folder = data.get("auto_open_folder", False)
            self.last_storage = data.get("last_storage", None)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        self.video_dir.mkdir(parents=True, exist_ok=True)

    def save_settings(self):
        data = {
            "camera_ip": self.camera_ip,
            "video_dir": str(self.video_dir),
            "naming_scheme": self.naming_scheme.value,
            "naming_prefix": self.naming_prefix,
            "naming_separator": self.naming_separator,
            "date_subfolders": self.date_subfolders,
            "auto_open_folder": self.auto_open_folder,
            "last_storage": self.last_storage,
        }
        try:
            with open(self._settings_path(), "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def _history_path(self) -> Path:
        return self._settings_path().parent / "history.json"

    def log_session(self, action: str, file_count: int, auto_deleted: int = 0, notes: str = ""):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "files": file_count,
            "auto_deleted": auto_deleted,
            "camera_ip": self.camera_ip,
            "save_folder": str(self.video_dir),
            "notes": notes,
        }
        history = []
        try:
            with open(self._history_path()) as f:
                history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        history.append(entry)
        # Keep last 200 entries
        history = history[-200:]
        try:
            with open(self._history_path(), "w") as f:
                json.dump(history, f, indent=2)
        except OSError:
            pass

    def get_history(self) -> list:
        try:
            with open(self._history_path()) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    # --- Camera Presets ---

    def _profiles_path(self) -> Path:
        return self._settings_path().parent / "profiles.json"

    def load_profiles(self) -> Dict[str, dict]:
        try:
            with open(self._profiles_path()) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_profile(self, name: str):
        profiles = self.load_profiles()
        profiles[name] = {
            "camera_ip": self.camera_ip,
            "video_dir": str(self.video_dir),
            "naming_scheme": self.naming_scheme.value,
            "naming_prefix": self.naming_prefix,
            "naming_separator": self.naming_separator,
        }
        try:
            with open(self._profiles_path(), "w") as f:
                json.dump(profiles, f, indent=2)
        except OSError:
            pass

    def apply_profile(self, name: str) -> bool:
        profiles = self.load_profiles()
        if name not in profiles:
            return False
        p = profiles[name]
        self.camera_ip = p.get("camera_ip", self.camera_ip)
        d = p.get("video_dir")
        if d:
            self.video_dir = Path(d)
            self.video_dir.mkdir(parents=True, exist_ok=True)
        s = p.get("naming_scheme", "")
        for ns in NamingScheme:
            if ns.value == s:
                self.naming_scheme = ns
                break
        self.naming_prefix = p.get("naming_prefix", self.naming_prefix)
        sep = p.get("naming_separator", "_")
        self.naming_separator = " " if sep == " " else "_"
        self.save_settings()
        return True

    def delete_profile(self, name: str):
        profiles = self.load_profiles()
        profiles.pop(name, None)
        try:
            with open(self._profiles_path(), "w") as f:
                json.dump(profiles, f, indent=2)
        except OSError:
            pass

    # --- Storage info ---

    def get_storage_info(self) -> Optional[Dict[str, int]]:
        """Get camera storage usage via df command over telnet. Returns dict with used/free/total bytes."""
        output = self._run_fresh_command("df /mnt/mmc", read_delay=2.0)
        self.log(f"[STORAGE] df output: {output[:300]}")
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/mnt/mmc" in line:
                try:
                    # df output in 1K blocks: filesystem, total, used, free, ...
                    total_kb = int(parts[1])
                    used_kb = int(parts[2])
                    free_kb = int(parts[3])
                    return {
                        "total": total_kb * 1024,
                        "used": used_kb * 1024,
                        "free": free_kb * 1024,
                    }
                except (ValueError, IndexError):
                    pass
        return None

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

    def _run_fresh_command(self, command: str, read_delay: float = 2.0,
                           idle_timeout: float = 0.5, retries: int = 1) -> str:
        """Run one command on a fresh connection.

        The read loop ends on an idle gap, so a stall on a marginal WiFi link can
        cut a long listing short with no error. The shell prompt marks a complete
        reply — if it's missing we retry with a longer patience rather than
        silently returning half a directory.
        """
        for attempt in range(retries + 1):
            result = self._run_fresh_command_once(
                command, read_delay, idle_timeout * (attempt + 1))
            # busybox ash prints "/ # " when the command has finished.
            if not result or result.rstrip().endswith("#"):
                return result
            self.log(f"[CMD] '{command}' looks truncated (no prompt, {len(result)} "
                     f"chars) — retry {attempt + 1}/{retries}")
        return result

    def _run_fresh_command_once(self, command: str, read_delay: float,
                                idle_timeout: float) -> str:
        sock = self._open_tcp(timeout=3.0)
        if not sock:
            self.log(f"[CMD] Failed to open TCP for: {command}")
            return ""
        try:
            time.sleep(1.0)
            self._drain(sock)
            sock.sendall((command + "\n").encode("utf-8"))
            time.sleep(read_delay)
            sock.settimeout(idle_timeout)
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

    def _discover_media_dirs(self) -> List[str]:
        """Enumerate the camera's actual DCIM/*SYCAM folders right now. The
        camera rolls forward to a new folder (102, 103, ...) once one fills
        up, so a fixed folder pair goes stale — this always reflects reality."""
        output = self._run_fresh_command("ls /mnt/mmc/DCIM/", read_delay=2.0)
        clean = re.sub(r'\x1b\[[\d;]*m', '', output)
        return sorted(set(re.findall(r'\d{3}SYCAM', clean)))

    def list_files_via_telnet(self) -> List[CameraFile]:
        files = []
        for directory in self._discover_media_dirs():
            output = self._run_fresh_command(f"ls -la /mnt/mmc/DCIM/{directory}/", read_delay=3.0)
            self.log(f"[TELNET {directory}] ({len(output)} chars): {output[:500]}")
            # A folder can hold either type (or, after a rollover, a mix) — match
            # by each file's own extension rather than assuming by folder number.
            files.extend(self._parse_ls_la(output, directory, "mp4", True))
            files.extend(self._parse_ls_la(output, directory, "jpg", False))
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

        for subdir in self._discover_media_dirs():
            files.extend(self._fetch_http_listing(subdir, "mp4", True))
            files.extend(self._fetch_http_listing(subdir, "jpg", False))

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

    def _build_local_size_map(self) -> Dict[int, list]:
        """Map size -> [(local_name, mtime), ...].

        Size alone is not a safe identity test: two different clips can share a
        byte count, and the loser is silently marked "Synced" and can then be
        deleted off the camera without ever having been downloaded. We keep every
        candidate at a given size and disambiguate with mtime, which download_file
        stamps from the camera's own timestamp via os.utime().
        """
        size_map: Dict[int, list] = {}
        try:
            dirs_to_scan = [self.video_dir]
            if self.date_subfolders:
                for d in self.video_dir.iterdir():
                    if d.is_dir():
                        dirs_to_scan.append(d)
            for scan_dir in dirs_to_scan:
                for entry in scan_dir.iterdir():
                    if entry.is_file() and entry.suffix.lower() in (".mp4", ".jpg"):
                        try:
                            st = entry.stat()
                        except OSError:
                            continue
                        if st.st_size > 0:
                            size_map.setdefault(st.st_size, []).append(
                                (entry.name, st.st_mtime))
        except OSError:
            pass
        return size_map

    def _match_local_copy(self, f: CameraFile, size_map: Dict[int, list]) -> Optional[str]:
        """Return the local filename already holding this camera file, or None.

        Requires size to match AND a corroborating signal (same name, or an mtime
        matching the camera's timestamp). Only when the size is unique locally do
        we accept size alone — which is the common case and keeps existing
        libraries from being re-downloaded wholesale."""
        if f.size_bytes <= 0:
            return None
        candidates = size_map.get(f.size_bytes)
        if not candidates:
            return None

        for name, _mtime in candidates:
            if name == f.name:
                return name

        if f.remote_timestamp:
            want = f.remote_timestamp.timestamp()
            for name, mtime in candidates:
                if abs(mtime - want) <= 2:
                    return name

        # No corroboration. A size shared by exactly one local file is still a
        # confident match (this is the overwhelmingly common case, and demanding
        # more would re-download whole libraries whose mtimes were never
        # stamped). Only a size shared by SEVERAL local files is genuinely
        # ambiguous — and that is precisely the case that could delete an
        # un-downloaded file off the camera, so refuse it.
        return candidates[0][0] if len(candidates) == 1 else None

    def _find_next_counter(self, date: Optional[datetime], ext: str) -> int:
        scheme = self.naming_scheme
        p = self.naming_prefix or "cam"
        sep = re.escape(self.naming_separator)
        max_counter = 0
        try:
            for entry in self.video_dir.iterdir():
                if not entry.name.endswith(f".{ext}"):
                    continue
                if scheme == NamingScheme.CUSTOM_SEQ:
                    # Match: {prefix}{sep}{NNN}.{ext}  (try both separators for compatibility)
                    m = re.search(r'^' + re.escape(p) + r'[_ ](\d{3})\.' + re.escape(ext) + r'$', entry.name)
                elif scheme == NamingScheme.SEQ_CUSTOM:
                    # Match: {NNN}{sep}{prefix}.{ext}
                    m = re.search(r'^(\d{3})[_ ]' + re.escape(p) + r'\.' + re.escape(ext) + r'$', entry.name)
                else:
                    # Date-based schemes: match by date string
                    if date:
                        date_str = f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
                        if date_str not in entry.name:
                            continue
                    m = re.search(r'[_ ](\d{3})\.' + re.escape(ext) + r'$', entry.name)
                if m:
                    max_counter = max(max_counter, int(m.group(1)))
        except OSError:
            pass
        return max_counter + 1

    def resolve_file_names(self, files: List[CameraFile]):
        local_sizes = self._build_local_size_map()
        date_counters: Dict[str, int] = {}
        # Names handed out during THIS pass. Checking the disk alone isn't enough:
        # two not-yet-downloaded camera files (e.g. the same SCKR number in two
        # rolled-over DCIM folders) would both find the path free and collide,
        # with the second silently overwriting the first.
        assigned: set = set()

        for f in files:
            existing = self._match_local_copy(f, local_sizes)
            if existing:
                f.is_downloaded = True
                f.selected = False
                f.local_name = existing
                assigned.add(existing)
                self.log(f"[RESOLVE] {f.name} -> already have {existing} (size={f.size_bytes})")
                continue

            f.is_downloaded = False
            f.selected = True

            if self.naming_scheme == NamingScheme.ORIGINAL:
                candidate = f.name
                n = 1
                base = Path(f.name).stem
                ext = "mp4" if f.is_video else "jpg"
                while (self.video_dir / candidate).exists() or candidate in assigned:
                    candidate = f"{base}_{n}.{ext}"
                    n += 1
                f.local_name = candidate
                assigned.add(candidate)
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
                f.local_name = self.naming_scheme.generate_name(
                    self.naming_prefix, date, ext, counter, self.naming_separator)
                # Counters are derived from disk state, so a name can still
                # collide with one assigned earlier in this same pass.
                while f.local_name in assigned:
                    counter = date_counters[date_key]
                    date_counters[date_key] = counter + 1
                    f.local_name = self.naming_scheme.generate_name(
                        self.naming_prefix, date, ext, counter, self.naming_separator)
                assigned.add(f.local_name)

            self.log(f"[RESOLVE] {f.name} -> {f.local_name} (new)")

    # --- Download ---

    def download_file(self, file: CameraFile, progress_cb: Callable, done_cb: Callable,
                      cancel_check: Optional[Callable] = None,
                      bytes_cb: Optional[Callable] = None):
        url = f"http://{self.camera_ip}:{self.http_port}/{file.http_path}"
        save_dir = self.video_dir
        if self.date_subfolders and file.remote_timestamp:
            d = file.remote_timestamp
            subfolder = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
            save_dir = self.video_dir / subfolder
            save_dir.mkdir(parents=True, exist_ok=True)
        dest = save_dir / file.local_name
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
                        if bytes_cb:
                            bytes_cb(downloaded)

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

            ok = dest.stat().st_size > 0
            # Without Content-Length we cannot prove the transfer wasn't cut
            # short, so the copy is unverified. Cross-check the size the camera
            # reported at listing time; failing that, report it unverified so
            # the caller declines to auto-delete the only good copy.
            verified = True
            if total <= 0:
                if file.size_bytes > 0:
                    verified = (downloaded == file.size_bytes)
                    if not verified:
                        self.log(f"[DOWNLOAD] {file.name} no Content-Length and size "
                                 f"mismatch: got {downloaded}, listing said {file.size_bytes}")
                else:
                    verified = False
                    self.log(f"[DOWNLOAD] {file.name} no Content-Length and no listed "
                             f"size — cannot verify completeness")
            done_cb(ok, verified)
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
        for directory in self._discover_media_dirs():
            r = self._run_fresh_command(f"rm -f /mnt/mmc/DCIM/{directory}/*", read_delay=2.0)
            self.log(f"[WIPE] {directory}: {r}")

    def cleanup(self):
        if self._persistent_sock:
            try:
                self._persistent_sock.close()
            except OSError:
                pass
            self._persistent_sock = None

    # --- Camera discovery ---

    def _is_valid_ip(self, ip: str) -> bool:
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False

    def local_subnet_prefix(self) -> str:
        """First three octets of this machine's LAN address, e.g. '192.168.1'."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return ".".join(local_ip.split(".")[:3])
        except OSError:
            return "192.168.1"

    def _local_subnet_hosts(self) -> List[str]:
        """Candidate host IPs on this machine's own /24, so discovery isn't tied
        to any one hardcoded network (fixes drift if you change routers)."""
        prefix = self.local_subnet_prefix()
        if not prefix:
            return []
        return [f"{prefix}.{i}" for i in range(1, 255)]

    @staticmethod
    def _ping(ip: str):
        try:
            if platform.system() == "Windows":
                cmd = ["ping", "-n", "1", "-w", "300", ip]
            else:
                cmd = ["ping", "-c", "1", ip]
            subprocess.run(cmd, capture_output=True, timeout=1)
        except Exception:
            pass

    def _refresh_arp_cache(self):
        """Actively probe every host on the subnet so the OS's ARP table is
        current before we read it — a cold/expired ARP entry (not the camera
        being gone) is the most common reason discovery used to fail."""
        hosts = self._local_subnet_hosts()
        if not hosts:
            return
        with ThreadPoolExecutor(max_workers=40) as pool:
            pool.map(self._ping, hosts)

    def _mac_sweep(self, status_cb: Callable) -> Optional[str]:
        status_cb("Scanning network...")
        self._refresh_arp_cache()
        try:
            if platform.system() == "Linux" and os.path.exists("/proc/net/arp"):
                with open("/proc/net/arp") as f:
                    for line in f:
                        if self.CAMERA_MAC_PREFIX in line.lower():
                            return line.split()[0]
            arp_cmd = ["arp", "-a"]
            if platform.system() == "Darwin":
                arp_cmd = ["/usr/sbin/arp", "-a"]
            result = subprocess.run(arp_cmd, capture_output=True, text=True, timeout=5)
            mac_sep = "-" if platform.system() == "Windows" else ":"
            mac_prefix = self.CAMERA_MAC_PREFIX.replace(":", mac_sep)
            for line in result.stdout.splitlines():
                if mac_prefix in line.lower():
                    m = re.search(r'\(([\d.]+)\)', line)
                    if m:
                        return m.group(1)
        except Exception as e:
            self.log(f"[SWEEP] error: {e}")
        return None

    def discover(self, status_cb: Callable) -> Tuple[Optional[str], str]:
        """Single entry point for both manual Connect and the background
        watcher: try the last-known/manual IP first, then fall back to an
        active MAC-based subnet sweep. No more hardcoded IP-range guessing."""
        if self._is_valid_ip(self.camera_ip):
            status_cb("Checking last known address...")
            if self.is_reachable():
                return self.camera_ip, "last known address"

        ip = self._mac_sweep(status_cb)
        if ip:
            return ip, "MAC address"
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
        self._heartbeat_running = False
        self._watch_running = False
        self._watch_miss_count = 0
        self._original_warning_shown = False  # one-per-session popup
        self.auto_delete_var = ctk.BooleanVar(value=False)

        self.title("HumDrop")
        self.geometry("720x800")
        self.minsize(600, 650)
        ctk.set_appearance_mode("system")

        self._build_ui()
        self._restore_storage_display()
        self._show_disconnected()
        self._start_watch()
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
        """Draw the bird icon matching the app icon (generate_icons.py)."""
        bg = self._resolve(("#f0f0f0", "#1e1e1e"))
        c = tk.Canvas(parent, width=size, height=size, highlightthickness=0, bg=bg)

        s = size
        pad = 2
        r = s * 0.18

        # Rounded rectangle background — deep teal base
        pts = [
            pad + r, pad, s - pad - r, pad, s - pad, pad,
            s - pad, pad + r, s - pad, s - pad - r, s - pad, s - pad,
            s - pad - r, s - pad, pad + r, s - pad, pad, s - pad,
            pad, s - pad - r, pad, pad + r, pad, pad,
        ]
        c.create_polygon(pts, smooth=True, fill=TEAL_DARK, outline="")

        # Lighter teal overlay on upper 65% for gradient feel
        top_pts = [
            pad + r, pad, s - pad - r, pad, s - pad, pad,
            s - pad, pad + r, s - pad, s * 0.65,
            pad, s * 0.65, pad, pad + r, pad, pad,
        ]
        c.create_polygon(top_pts, smooth=True, fill=TEAL, outline="")
        # Blend zone between teal and teal_dark
        blend_pts = [
            pad, s * 0.45, s - pad, s * 0.45,
            s - pad, s * 0.65, pad, s * 0.65,
        ]
        c.create_polygon(blend_pts, fill=TEAL, outline="")

        # Subtle white border
        c.create_polygon(pts, smooth=True, fill="", outline="white", width=1)

        # Branch — gentle curve via multiple points
        branch_y = s * 0.52
        bw_line = max(2, s * 0.028)
        branch_color = "#503C28"
        branch_pts = []
        for i in range(6):
            t = i / 5.0
            bx_pt = s * 0.12 + t * s * 0.76
            by_pt = branch_y + math.sin(t * math.pi) * s * 0.03
            branch_pts.extend([bx_pt, by_pt])
        c.create_line(*branch_pts, fill=branch_color, width=bw_line,
                      smooth=True, capstyle="round")
        # Small twig going up-right
        tw_x = s * 0.12 + (13 / 19.0) * s * 0.76
        tw_y = branch_y + math.sin((13 / 19.0) * math.pi) * s * 0.03
        c.create_line(tw_x, tw_y, tw_x + s * 0.06, tw_y - s * 0.07,
                      fill=branch_color, width=max(1, bw_line // 2), capstyle="round")

        # Bird body — wider ellipse matching icon proportions
        bx = s * 0.48
        by = s * 0.38
        body_w = s * 0.13
        body_h = s * 0.10
        c.create_oval(bx - body_w, by - body_h, bx + body_w, by + body_h,
                      fill="white", outline="")

        # Head — offset right like icon
        hr = s * 0.068
        hx = bx + body_w * 0.7
        hy = by - body_h * 0.5
        c.create_oval(hx - hr, hy - hr, hx + hr, hy + hr, fill="white", outline="")

        # Eye
        er = max(1.5, s * 0.014)
        ex = hx + hr * 0.3
        ey = hy - hr * 0.15
        c.create_oval(ex - er, ey - er, ex + er, ey + er, fill="#2a1510", outline="")
        # Eye highlight
        hlr = max(1, er * 0.5)
        c.create_oval(ex - hlr, ey - hlr - 1, ex, ey - 1, fill="white", outline="")

        # Beak (orange triangle, pointing right)
        beak_len = s * 0.05
        c.create_polygon(
            hx + hr, hy - hr * 0.15,
            hx + hr + beak_len, hy,
            hx + hr, hy + hr * 0.2,
            fill=ORANGE, outline=""
        )

        # Tail feathers — 5-point splayed shape matching icon
        c.create_polygon(
            bx - body_w, by - body_h * 0.3,
            bx - body_w - s * 0.07, by - body_h * 0.8,
            bx - body_w - s * 0.05, by - body_h * 0.1,
            bx - body_w - s * 0.08, by + body_h * 0.2,
            bx - body_w + s * 0.01, by + body_h * 0.3,
            fill="white", outline=""
        )

        # Wing detail line (teal accent, matching icon)
        c.create_line(
            bx - body_w * 0.2, by - body_h * 0.3,
            bx + body_w * 0.1, by,
            bx - body_w * 0.3, by + body_h * 0.5,
            fill=TEAL_DARK, width=max(1, s * 0.008), smooth=True
        )

        # Legs on branch
        leg_w = max(1, s * 0.008)
        foot_y = branch_y - max(1, int(bw_line / 2))
        foot_x1 = bx + body_w * 0.1
        foot_x2 = bx + body_w * 0.4
        foot_top = by + body_h - s * 0.01
        c.create_line(foot_x1, foot_top, foot_x1, foot_y,
                      fill="#505050", width=leg_w, capstyle="round")
        c.create_line(foot_x2, foot_top, foot_x2, foot_y,
                      fill="#505050", width=leg_w, capstyle="round")

        # Download arrow icon below branch
        arrow_cx = s * 0.50
        arrow_top = s * 0.62
        arrow_bottom = s * 0.82
        shaft_w = max(2, s * 0.035)
        head_w = s * 0.10
        head_h = s * 0.06

        # Arrow shaft (rectangle)
        half_sw = shaft_w / 2
        c.create_rectangle(arrow_cx - half_sw, arrow_top,
                           arrow_cx + half_sw, arrow_bottom - head_h,
                           fill="white", outline="")
        # Arrowhead triangle
        c.create_polygon(
            arrow_cx - head_w / 2, arrow_bottom - head_h,
            arrow_cx + head_w / 2, arrow_bottom - head_h,
            arrow_cx, arrow_bottom,
            fill="white", outline=""
        )
        # Tray with edges underneath
        tray_y = s * 0.86
        tray_w = s * 0.22
        tray_thick = max(2, s * 0.02)
        tray_edge_h = s * 0.03
        c.create_line(arrow_cx - tray_w / 2, tray_y,
                      arrow_cx + tray_w / 2, tray_y,
                      fill="white", width=tray_thick, capstyle="round")
        # Left edge
        c.create_line(arrow_cx - tray_w / 2, tray_y,
                      arrow_cx - tray_w / 2, tray_y - tray_edge_h,
                      fill="white", width=tray_thick, capstyle="round")
        # Right edge
        c.create_line(arrow_cx + tray_w / 2, tray_y,
                      arrow_cx + tray_w / 2, tray_y - tray_edge_h,
                      fill="white", width=tray_thick, capstyle="round")

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
        self.grid_rowconfigure(3, weight=1)  # content area gets the stretch
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
        small_btns = ctk.CTkFrame(btn_frame, fg_color="transparent")
        small_btns.pack(pady=(4, 0))
        ctk.CTkButton(small_btns, text="History", width=48, height=26,
                      fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
                      font=ctk.CTkFont(size=13), command=self._show_history).pack(side="left", padx=(0, 4))
        ctk.CTkButton(small_btns, text="Presets", width=48, height=26,
                      fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
                      font=ctk.CTkFont(size=13), command=self._show_profiles).pack(side="left")

        # IP row
        ip_frame = ctk.CTkFrame(header, fg_color="transparent")
        ip_frame.grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(6, 0))

        ctk.CTkLabel(ip_frame, text="Camera IP", font=ctk.CTkFont(size=16),
                     text_color=TEXT_SEC).pack(side="left")

        self.ip_entry = ctk.CTkEntry(ip_frame, width=150, font=ctk.CTkFont(family="Courier", size=16))
        self.ip_entry.pack(side="left", padx=(8, 10))
        self.ip_entry.insert(0, self.camera.camera_ip)

        # Status dot + label
        self.status_dot = ctk.CTkLabel(ip_frame, text="\u25CF", font=ctk.CTkFont(size=18),
                                        text_color=RED, width=24)
        self.status_dot.pack(side="left", padx=(6, 0))

        self.status_label = ctk.CTkLabel(ip_frame, text="Disconnected", font=ctk.CTkFont(size=16),
                                          text_color=TEXT_SEC)
        self.status_label.pack(side="left", padx=(2, 0))

        # Folder row
        folder_frame = ctk.CTkFrame(header, fg_color="transparent")
        folder_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=20, pady=(6, 2))
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

        # --- Persistent storage bar (always visible, below header) ---
        self.storage_frame = ctk.CTkFrame(self, fg_color="transparent", height=24)
        self.storage_frame.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 0))
        self.storage_frame.grid_propagate(True)

        self.storage_label = ctk.CTkLabel(self.storage_frame, text="", font=ctk.CTkFont(size=14),
                                           text_color=TEXT_MUTED)
        self.storage_label.pack(side="left")

        # Visual storage bar (teal filled bar with empty/full markers)
        self.storage_canvas = tk.Canvas(self.storage_frame, width=160, height=16,
                                         highlightthickness=0)
        try:
            ctk_bg = ctk.ThemeManager.theme["CTk"]["fg_color"]
            mode = ctk.get_appearance_mode()
            self.storage_canvas.configure(bg=ctk_bg[1] if mode == "Dark" else ctk_bg[0])
        except Exception:
            self.storage_canvas.configure(bg="#2b2b2b" if ctk.get_appearance_mode() == "Dark" else "#f0f0f0")
        self.storage_canvas.pack(side="left", padx=(8, 0))

        self.storage_time_label = ctk.CTkLabel(self.storage_frame, text="", font=ctk.CTkFont(size=12),
                                                text_color=TEXT_MUTED)
        self.storage_time_label.pack(side="left", padx=(6, 0))

        # --- Main content area ---
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=3, column=0, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        # Instruction overlay
        self.instruction_frame = ctk.CTkFrame(self.content, fg_color="transparent")
        self.instruction_frame.grid(row=0, column=0, sticky="nsew")

        inner = ctk.CTkFrame(self.instruction_frame, fg_color="transparent")
        inner.place(relx=0.5, rely=0.45, anchor="center")

        bird_c = self._create_bird_canvas(inner, size=120)
        bird_c.pack(pady=(0, 16))
        self._bird_canvases.append({'canvas': bird_c, 'parent': inner, 'size': 120, 'pady': (0, 16)})

        ctk.CTkLabel(inner, text="To get started:\n\n"
                     "1.  Open the camera app on your phone\n"
                     "2.  Start the camera stream\n"
                     "3.  Make sure you're on the same Wi-Fi network\n"
                     '4.  HumDrop watches for it automatically — or tap\n'
                     '     "Connect" to try right now',
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

        self.selectall_btn = ctk.CTkButton(
            btn_row, text="Select All", width=95, height=32, font=ctk.CTkFont(size=15),
            fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
            command=self._select_all)
        self.selectall_btn.pack(side="left", padx=(0, 4))
        self.selectnew_btn = ctk.CTkButton(
            btn_row, text="Select New", width=95, height=32, font=ctk.CTkFont(size=15),
            fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
            command=self._select_new)
        self.selectnew_btn.pack(side="left")

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

        # Options checkboxes
        opts_frame = ctk.CTkFrame(f, fg_color="transparent")
        opts_frame.grid(row=6, column=0, sticky="ew", padx=24, pady=(6, 0))

        self.auto_delete_cb = ctk.CTkCheckBox(
            opts_frame, text="Auto-delete from camera",
            variable=self.auto_delete_var,
            font=ctk.CTkFont(size=16), text_color=TEXT_SEC,
            fg_color=TEAL, hover_color=TEAL_HOVER,
            border_color=BORDER, checkmark_color="white"
        )
        self.auto_delete_cb.pack(side="left", padx=(0, 16))

        self.date_subfolder_var = ctk.BooleanVar(value=self.camera.date_subfolders)
        self.date_subfolder_cb = ctk.CTkCheckBox(
            opts_frame, text="Date subfolders",
            variable=self.date_subfolder_var,
            font=ctk.CTkFont(size=16), text_color=TEXT_SEC,
            fg_color=TEAL, hover_color=TEAL_HOVER,
            border_color=BORDER, checkmark_color="white",
            command=self._toggle_date_subfolders
        )
        self.date_subfolder_cb.pack(side="left", padx=(0, 16))

        self.auto_open_var = ctk.BooleanVar(value=self.camera.auto_open_folder)
        self.auto_open_cb = ctk.CTkCheckBox(
            opts_frame, text="Open folder when done",
            variable=self.auto_open_var,
            font=ctk.CTkFont(size=16), text_color=TEXT_SEC,
            fg_color=TEAL, hover_color=TEAL_HOVER,
            border_color=BORDER, checkmark_color="white",
            command=self._toggle_auto_open
        )
        self.auto_open_cb.pack(side="left")

        # Separator
        sep = ctk.CTkFrame(f, height=1, fg_color=BORDER)
        sep.grid(row=7, column=0, sticky="ew", padx=20, pady=(12, 0))

        # Naming config
        naming_frame = ctk.CTkFrame(f, fg_color="transparent")
        naming_frame.grid(row=8, column=0, sticky="ew", padx=20, pady=(8, 0))
        naming_frame.grid_columnconfigure(1, weight=0)

        ctk.CTkLabel(naming_frame, text="Naming", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, sticky="w")

        scheme_values = [f"{s.display_name}  ({s.example(self.camera.naming_prefix, self.camera.naming_separator)})" for s in NamingScheme]
        self.scheme_var = ctk.StringVar(value=scheme_values[list(NamingScheme).index(self.camera.naming_scheme)])
        self.scheme_menu = ctk.CTkOptionMenu(
            naming_frame, values=scheme_values, variable=self.scheme_var,
            width=320, height=34, font=ctk.CTkFont(size=16),
            fg_color=BTN_SEC, text_color=TEXT_PRI,
            button_color=ORANGE, button_hover_color=ORANGE_HOVER,
            command=self._scheme_changed, dynamic_resizing=False
        )
        self.scheme_menu.grid(row=0, column=1, padx=(8, 0), sticky="w")

        # Separator radio buttons (underscore vs space) — same row, right of dropdown
        sep_frame = ctk.CTkFrame(naming_frame, fg_color="transparent")
        sep_frame.grid(row=0, column=2, padx=(12, 0), sticky="w")

        self.sep_var = ctk.StringVar(value=self.camera.naming_separator)

        ctk.CTkRadioButton(
            sep_frame, text="underscore", variable=self.sep_var, value="_",
            font=ctk.CTkFont(size=14), text_color=TEXT_SEC,
            fg_color=TEAL, hover_color=TEAL_HOVER,
            border_color=BORDER,
            command=self._separator_changed
        ).pack(side="left")

        ctk.CTkRadioButton(
            sep_frame, text="space", variable=self.sep_var, value=" ",
            font=ctk.CTkFont(size=14), text_color=TEXT_SEC,
            fg_color=TEAL, hover_color=TEAL_HOVER,
            border_color=BORDER,
            command=self._separator_changed
        ).pack(side="left", padx=(8, 0))

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

        # Warning for "Camera original" naming
        self.original_warning = ctk.CTkLabel(
            f, text="\u26A0  Heads up: The camera reuses names like SCKR1000 across sessions. "
                    "Duplicates get _1, _2 suffixes \u2014 your files stay safe, but you lose dates and organization.",
            font=ctk.CTkFont(size=13), text_color=ORANGE,
            wraplength=600, justify="left"
        )
        if self.camera.naming_scheme == NamingScheme.ORIGINAL:
            self.original_warning.grid(row=10, column=0, sticky="w", padx=24, pady=(4, 0))

        # Separator 2
        sep2 = ctk.CTkFrame(f, height=1, fg_color=BORDER)
        sep2.grid(row=11, column=0, sticky="ew", padx=20, pady=(12, 0))

        # Bottom buttons
        bottom_frame = ctk.CTkFrame(f, fg_color="transparent")
        bottom_frame.grid(row=12, column=0, sticky="ew", padx=20, pady=(10, 16))
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

    def _row_values_and_tags(self, i: int, f: "CameraFile"):
        check = "\u2611" if f.selected else "\u2610"
        save_as = f.local_name if f.is_renamed else "\u2014"
        type_str = "Video" if f.is_video else "Photo"
        status_str = "Synced" if f.is_downloaded else "New"

        tags = ["synced" if f.is_downloaded else "new"]
        if i % 2 == 1:
            tags.append("stripe")

        return (check, f.name, save_as, f.size_string, type_str, status_str), tuple(tags)

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for i, f in enumerate(self.files):
            values, tags = self._row_values_and_tags(i, f)
            self.tree.insert("", "end", iid=str(i), values=values, tags=tags)

    def _update_table_row(self, idx: int):
        """Update a single row in place \u2014 avoids rebuilding the whole table
        (O(n) widget ops) after every file in a large batch download, which
        made total download time scale as O(n^2) with the library size."""
        if not (0 <= idx < len(self.files)):
            return
        row_id = str(idx)
        if not self.tree.exists(row_id):
            return
        values, tags = self._row_values_and_tags(idx, self.files[idx])
        self.tree.item(row_id, values=values, tags=tags)

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
        # The running batch is snapshotted at launch, so toggling now would do
        # nothing except mislead — and the download thread overwrites `selected`.
        if self.is_downloading:
            return
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
            # Expand a bare last octet against THIS machine's subnet rather than
            # a hardcoded 192.168.1.x, matching how discovery sweeps.
            raw = f"{self.camera.local_subnet_prefix()}.{raw}"
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
            self._stop_heartbeat()
            self.camera.cleanup()
            self.is_connected = False
            self.files = []
            self._show_disconnected()
            self._update_status("Disconnected", connected=False)
            self.connect_btn.configure(text="Connect")
            self._start_watch()
            return

        self._sync_ip()
        # Stop the background watcher first: otherwise an already-scheduled
        # poll can fire during this (multi-second) attempt, find the camera
        # too, and race us into a second concurrent start_httpd() on the same
        # socket — which wedges the UI with Connect stuck disabled.
        self._stop_watch()
        self.connect_btn.configure(state="disabled")
        self._update_status("Connecting...", connected=None)
        self._start_connect_spinner()

        def do_connect():
            ip, method = self.camera.discover(
                lambda msg: self.after(0, lambda m=msg: self._update_status(m))
            )
            if not ip:
                self.after(0, lambda: self._connect_failed())
                return
            self.camera.camera_ip = ip
            self.camera.save_settings()
            self.after(0, lambda: self._apply_found_ip(ip))
            self.after(0, lambda: self._update_status("Starting camera server..."))
            self.camera.start_httpd()
            self.after(0, lambda: self._connect_success())

        threading.Thread(target=do_connect, daemon=True).start()

    def _apply_found_ip(self, ip: str):
        self.ip_entry.delete(0, "end")
        self.ip_entry.insert(0, ip)

    def _connect_failed(self):
        self._stop_connect_spinner()
        self.connect_btn.configure(state="normal")
        self._update_status("Watching for camera...", connected=False)
        messagebox.showwarning("Camera Not Found",
                               f"Could not reach the camera (last known address "
                               f"{self.camera.camera_ip}, and no device matching its "
                               f"MAC address was found on the network).\n\n"
                               "Make sure:\n1. The camera app is open and streaming\n"
                               "2. You're on the same Wi-Fi network\n"
                               "3. The camera is powered on\n\n"
                               "HumDrop will keep watching in the background and "
                               "connect automatically once it's reachable.")
        self._start_watch()

    def _connect_success(self):
        self._stop_connect_spinner()
        # Guard: if disconnect happened during the connect thread, bail out
        if not self.camera.is_reachable():
            self._update_status("Disconnected", connected=False)
            self.connect_btn.configure(state="normal", text="Reconnect")
            self._start_watch()
            return
        self.is_connected = True
        self._stop_watch()
        self.connect_btn.configure(state="normal", text="Disconnect")
        self._update_status("Connected", connected=True)
        self._show_connected()
        self._start_heartbeat()
        self._fetch_storage_info()
        self._refresh()

    def _refresh(self):
        if not self.is_connected:
            return
        # A refresh replaces self.files wholesale. Doing that under a running
        # download corrupts the batch's indices (or kills its thread outright),
        # so refuse — the naming controls and Cmd+R can both land here.
        if self.is_downloading:
            self.camera.log("[REFRESH] Ignored — download in progress")
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
            try:
                found = self.camera.list_files()
            except Exception as e:
                self.camera.log(f"[REFRESH] Error: {e}")
                found = None
            if found is None:
                # list_files returned None or raised — check if camera is gone
                if not self.camera.is_reachable():
                    self.after(0, lambda: self._handle_disconnect(
                        "Disconnected"))
                    return
                found = []
            self.after(0, lambda: self._refresh_done(found))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _refresh_done(self, found: List[CameraFile]):
        # Guard: if we disconnected while the refresh was in-flight, ignore this callback
        if not self.is_connected:
            return

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
        self._update_status("No files found" if not found else "Connected", connected=True)

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
        total_bytes = sum(f.size_bytes for _, f in selected)

        def download_seq():
            completed_count = 0
            auto_deleted_count = 0
            connection_lost = False
            session_start = time.time()
            cumulative_bytes = [0]  # mutable for nested access
            # Rolling (transfer-clock, bytes) window backing the displayed rate,
            # plus the timestamp of the last UI push so a fast transfer can't
            # flood Tk.
            samples = deque(maxlen=128)
            last_ui_push = [0.0]
            # Wall clock minus the gaps between files. Those gaps are real time
            # but move zero bytes (telnet delete round-trip, httpd handshake),
            # so charging them to the transfer rate is what made the MB/s number
            # read low and drift. The ETA still uses wall clock — it has to
            # predict wall clock — but the rate should be the rate.
            idle_total = [0.0]
            last_byte_at = [0.0]

            def progress_text(name, c, cur_bytes, now):
                """Build the status line on the worker thread.

                Rate comes from a short rolling window over transfer time, so it
                shows what the wire is doing now rather than a session average
                that every round-trip since the batch began has diluted.
                """
                # Seconds since the batch began, minus the between-file gaps.
                # Must be relative: it is also the divisor in the warm-up rate
                # below, and an absolute epoch there yields a rate of zero.
                clock = now - session_start - idle_total[0]
                samples.append((clock, cur_bytes))
                rate = 0.0
                t0, b0 = samples[0]
                for t, b in samples:
                    if clock - t <= _SPEED_WINDOW_SEC:
                        break
                    t0, b0 = t, b
                dt, db = clock - t0, cur_bytes - b0
                if dt >= 0.5 and db > 0:
                    rate = db / dt
                elif clock > 0.5 and cur_bytes > 0:
                    rate = cur_bytes / clock          # not enough window yet
                speed_str = f"{rate / 1_048_576:.1f} MB/s" if rate else "..."

                # Estimate the two costs separately: bytes still to move at the
                # measured wire rate, plus the per-file gap for every file not
                # yet started. The old estimate rolled both into one average
                # rate, so it had to climb file after file as gaps accumulated
                # — the number appeared to get worse the longer you waited.
                eta = "..."
                if rate > 0 and total_bytes > cur_bytes:
                    per_file_gap = idle_total[0] / c if c else 0.0
                    secs_left = int((total_bytes - cur_bytes) / rate
                                    + max(0, total - c - 1) * per_file_gap)
                    mins, secs = divmod(secs_left, 60)
                    eta = f"{mins}:{secs:02d}" if mins else f"{secs}s"
                return f"Downloading {name} ({c + 1}/{total}) — {speed_str}, ~{eta} left"

            def apply_ui(text, frac):
                self.progress_label.configure(text=text)
                self.progress_bar.set(frac)

            for completed, (idx, file) in enumerate(selected):
                # Check cancel flag before each file
                if self._download_cancel:
                    self.camera.log("[DOWNLOAD] Cancelled by user")
                    break

                dl_name = file.local_name
                file_bytes_so_far = [0]

                def push_ui(force=False, name=dl_name, c=completed,
                            fb=file_bytes_so_far, size=file.size_bytes):
                    """Marshal one finished string to the main thread, at most
                    every _UI_THROTTLE_SEC. Previously every 64 KB chunk queued
                    two `after` callbacks, so a multi-megabyte file could pile
                    hundreds of them onto the event loop; the main thread then
                    competed with the socket read for the GIL and the backlog
                    carried across files, making each download slower than the
                    last."""
                    now = time.time()
                    if not force and now - last_ui_push[0] < _UI_THROTTLE_SEC:
                        return
                    last_ui_push[0] = now
                    cur_bytes = cumulative_bytes[0] + fb[0]
                    text = progress_text(name, c, cur_bytes, now)
                    frac = (c + (fb[0] / size if size else 0)) / total
                    self.after(0, lambda t=text, v=frac: apply_ui(t, v))

                push_ui(force=True)

                first_byte = [True]

                def note_bytes():
                    """Charge the pre-transfer gap to idle, not to the wire.
                    On the first file there is no previous byte to measure from,
                    so fall back to the batch start — otherwise that one gap is
                    never discounted and skews the rate for the whole session."""
                    now = time.time()
                    if first_byte[0]:
                        first_byte[0] = False
                        idle_total[0] += now - (last_byte_at[0] or session_start)
                    last_byte_at[0] = now

                def prog(pct, fb=file_bytes_so_far, size=file.size_bytes):
                    fb[0] = int(pct * size) if size else 0
                    note_bytes()
                    push_ui()

                def on_bytes(b, fb=file_bytes_so_far):
                    fb[0] = b
                    note_bytes()
                    push_ui()

                done_event = threading.Event()
                success_flag = [False]
                verified_flag = [True]

                def done(ok, verified=True, flag=success_flag,
                         vflag=verified_flag, ev=done_event):
                    flag[0] = ok
                    vflag[0] = verified
                    ev.set()

                self.camera.download_file(file, prog, done,
                                          cancel_check=lambda: self._download_cancel,
                                          bytes_cb=on_bytes)
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
                    cumulative_bytes[0] += file.size_bytes or 0
                    # self.files can be replaced by a refresh mid-batch; only
                    # touch the row if it still holds this exact file.
                    if idx < len(self.files) and self.files[idx] is file:
                        self.files[idx].is_downloaded = True
                        self.files[idx].selected = False
                        self.after(0, lambda i=idx: self._update_table_row(i))

                    # Per-file auto-delete: remove from camera right after download.
                    # Never delete the camera's only copy on the strength of a
                    # transfer we couldn't prove was complete.
                    if self.auto_delete_var.get():
                        if not verified_flag[0]:
                            self.camera.log(
                                f"[AUTO-DELETE] SKIPPED {file.name} — download could "
                                f"not be verified complete; keeping camera copy")
                        else:
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
            self._handle_disconnect("Disconnected")
            self._send_notification("HumDrop", f"Connection lost after downloading {total} files.")
            self._log_session("download", total, notes=f"Connection lost, auto-deleted {auto_deleted}")
            return
        # Success path — notify, open folder, log session
        self._send_notification("HumDrop", f"Downloaded {total} files" + (
            f", deleted {auto_deleted} from camera" if auto_deleted else ""))
        self._log_session("download", total, auto_deleted=auto_deleted)
        if self.auto_open_var.get() and total > 0:
            self._auto_open_save_folder()
        if auto_deleted > 0:
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
                try:
                    found = self.camera.list_files()
                except Exception:
                    found = None
                if found is None and not self.camera.is_reachable():
                    self.after(0, lambda: self._handle_disconnect("Disconnected"))
                    return
                self.after(0, lambda: self._download_auto_delete_refresh_done(
                    found or [], total, auto_deleted))

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
        if not self.is_connected:
            return
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

    # Selection and naming controls have no effect on an already-started batch
    # (it is snapshotted at launch, and the download thread writes `selected`
    # itself), so leaving them live during a download is misleading.
    def _set_secondary_controls(self, state: str):
        for attr in ("selectall_btn", "selectnew_btn", "scheme_menu", "prefix_entry"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.configure(state=state)
                except Exception:
                    pass

    def _re_enable_buttons(self):
        """Centralized helper to re-enable all action buttons."""
        self.download_btn.configure(state="normal")
        self.refresh_btn.configure(state="normal")
        self.clean_btn.configure(state="normal")
        self.wipe_btn.configure(state="normal")
        self.delete_btn.configure(state="normal")
        self.auto_delete_cb.configure(state="normal")
        self._set_secondary_controls("normal")

    def _disable_buttons(self):
        """Centralized helper to disable all action buttons during operations."""
        self.download_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.clean_btn.configure(state="disabled")
        self.wipe_btn.configure(state="disabled")
        self.delete_btn.configure(state="disabled")
        self.auto_delete_cb.configure(state="disabled")
        self._set_secondary_controls("disabled")

    def _handle_disconnect(self, message: str = "Connection lost"):
        """Centralized disconnect handler — dramatic UI reset to disconnected state."""
        self.is_connected = False
        self.is_downloading = False
        self._download_cancel = True
        self.camera.cleanup()
        self._stop_heartbeat()

        # Stop any progress animation
        try:
            self.progress_bar.stop()
        except Exception:
            pass
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(0)
        self.progress_bar.grid_forget()
        self.progress_label.grid_forget()

        # Full visual reset — back to disconnected home screen
        self._show_disconnected()
        self._update_status(message, connected=False)
        self.connect_btn.configure(text="Reconnect", state="normal")

        # Disable all action buttons since nothing works without connection
        self._disable_buttons()

        # Clear file state (keep storage info visible as last-known data)
        self.files = []
        self._refresh_table()
        self._update_summary()

        # Notify user
        self._send_notification("HumDrop", "Connection lost")

        # Resume background watching so it reconnects on its own once the
        # camera's reachable again, instead of waiting for another manual click.
        self._start_watch()

    def _start_heartbeat(self):
        """Start a periodic check that the camera is still reachable."""
        self._heartbeat_running = True
        self._heartbeat_check()

    def _stop_heartbeat(self):
        self._heartbeat_running = False

    def _heartbeat_check(self):
        if not self._heartbeat_running or not self.is_connected:
            return
        # Don't check during active operations
        if self.is_downloading:
            self.after(5000, self._heartbeat_check)
            return

        def check():
            reachable = self.camera.is_reachable()
            if not reachable and self._heartbeat_running and self.is_connected:
                self.camera.log("[HEARTBEAT] Camera unreachable")
                self.after(0, lambda: self._handle_disconnect(
                    "Disconnected"))
            elif self._heartbeat_running:
                self.after(5000, self._heartbeat_check)

        threading.Thread(target=check, daemon=True).start()

    # --- Background camera watcher ---
    # Polls quietly while disconnected so the camera (which sleeps between
    # motion-triggered clips and drifts to a new DHCP IP on wake) connects on
    # its own the moment it's reachable, with no button-mashing or popups.

    _WATCH_MIN_INTERVAL_MS = 20_000
    _WATCH_MAX_INTERVAL_MS = 60_000
    _WATCH_BACKOFF_STEP_MS = 10_000

    def _start_watch(self):
        if self._watch_running:
            return
        self._watch_running = True
        self._watch_miss_count = 0
        self._watch_check()

    def _stop_watch(self):
        self._watch_running = False

    def _watch_check(self):
        if not self._watch_running or self.is_connected:
            return
        self._update_status("Watching for camera...", connected=False)

        def check():
            ip, method = self.camera.discover(lambda msg: None)
            if not (self._watch_running and not self.is_connected):
                return  # state changed while we were scanning
            if ip:
                self.after(0, lambda: self._auto_connect(ip, method))
            else:
                self._watch_miss_count += 1
                interval = min(
                    self._WATCH_MAX_INTERVAL_MS,
                    self._WATCH_MIN_INTERVAL_MS + self._watch_miss_count * self._WATCH_BACKOFF_STEP_MS,
                )
                self.after(interval, self._watch_check)

        threading.Thread(target=check, daemon=True).start()

    def _auto_connect(self, ip: str, method: str):
        if not (self._watch_running and not self.is_connected):
            return
        self.camera.camera_ip = ip
        self._apply_found_ip(ip)
        self.camera.save_settings()
        self._watch_miss_count = 0
        self._update_status(f"Found camera ({method}) — connecting...", connected=None)
        self.connect_btn.configure(state="disabled")

        def do_connect():
            self.after(0, lambda: self._update_status("Starting camera server..."))
            self.camera.start_httpd()
            self.after(0, lambda: self._connect_success())

        threading.Thread(target=do_connect, daemon=True).start()

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
            deleted = 0
            for i, f in enumerate(files_to_delete):
                def update_ui(name=f.name, n=i):
                    self.progress_label.configure(
                        text=f"{status_prefix} {name} ({n + 1}/{total})...")
                    self.progress_bar.set((n + 1) / total)
                self.after(0, update_ui)
                try:
                    self.camera.delete_file(f)
                    deleted += 1
                except Exception as e:
                    self.camera.log(f"[DELETE] Error deleting {f.name}: {e}")
                    if not self.camera.is_reachable():
                        self.after(0, lambda d=deleted: self._handle_disconnect("Disconnected"))
                        return
            self.after(0, lambda: self._delete_done(deleted))

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
            try:
                found = self.camera.list_files()
            except Exception:
                found = None
            if found is None and not self.camera.is_reachable():
                self.after(0, lambda: self._handle_disconnect("Disconnected"))
                return
            self.after(0, lambda: self._delete_refresh_done(found or [], count))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _delete_refresh_done(self, found: list, deleted_count: int):
        """Post-delete refresh complete: update UI and re-enable."""
        if not self.is_connected:
            return
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0)
        self.progress_label.configure(text=f"Done! Deleted {deleted_count} files from camera.")
        self._update_status("Connected", connected=True)

        self.files = found
        self._refresh_table()
        self._update_summary()
        self._re_enable_buttons()
        self._fetch_storage_info()
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
            try:
                self.camera.wipe_all()
            except Exception as e:
                self.camera.log(f"[WIPE] Error: {e}")
                if not self.camera.is_reachable():
                    self.after(0, lambda: self._handle_disconnect("Disconnected"))
                    return
            self.after(0, self._wipe_done)

        threading.Thread(target=do_wipe, daemon=True).start()

    def _wipe_done(self):
        self.progress_label.configure(text="Wipe complete. Refreshing\u2026")
        self._update_status("Refreshing...")

        def do_refresh():
            try:
                found = self.camera.list_files()
            except Exception:
                found = None
            if found is None and not self.camera.is_reachable():
                self.after(0, lambda: self._handle_disconnect("Disconnected"))
                return
            self.after(0, lambda: self._wipe_refresh_done(found or []))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _wipe_refresh_done(self, found: list):
        if not self.is_connected:
            return
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
        if self.is_downloading:
            return
        for f in self.files:
            f.selected = True
        self._refresh_table()
        self._update_summary()

    def _select_new(self):
        if self.is_downloading:
            return
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

    def _restore_storage_display(self):
        """Restore last-known storage info from saved settings on app launch."""
        s = self.camera.last_storage
        if s and s.get("total"):
            self._apply_storage_display(s["used"], s["free"], s["total"], s["timestamp"])

    def _fmt_storage_bytes(self, b):
        if b >= 1_073_741_824:
            return f"{b / 1_073_741_824:.1f} GB"
        return f"{b / 1_048_576:.0f} MB"

    def _fmt_storage_time(self, t=None):
        t = t or datetime.now()
        return f"{t.hour % 12 or 12}:{t.minute:02d} {'PM' if t.hour >= 12 else 'AM'} {t.month}/{t.day}/{str(t.year)[2:]}"

    def _apply_storage_display(self, used_b, free_b, total_b, timestamp_str):
        """Update storage label, bar, and timestamp from raw byte values."""
        used = self._fmt_storage_bytes(used_b)
        free = self._fmt_storage_bytes(free_b)
        total = self._fmt_storage_bytes(total_b)
        pct = used_b / total_b * 100 if total_b else 0
        self.storage_label.configure(
            text=f"Storage: {used} used / {free} free / {total} total ({pct:.0f}%)")
        self._draw_storage_bar(pct)
        self.storage_time_label.configure(text=f"as of {timestamp_str}")

    def _fetch_storage_info(self):
        """Fetch camera storage usage in background and update the label + bar."""
        def do_fetch():
            info = self.camera.get_storage_info()
            if info and self.is_connected:
                t = datetime.now()
                now = self._fmt_storage_time(t)
                # Persist to settings so it survives restart
                self.camera.last_storage = {
                    "used": info["used"], "free": info["free"],
                    "total": info["total"], "timestamp": now,
                }
                self.camera.save_settings()
                def update_ui():
                    self._apply_storage_display(info["used"], info["free"], info["total"], now)
                self.after(0, update_ui)
        threading.Thread(target=do_fetch, daemon=True).start()

    def _draw_storage_bar(self, pct: float):
        """Draw a teal storage bar with empty/full indicators."""
        c = self.storage_canvas
        c.delete("all")
        w, h = 160, 16
        bar_x, bar_w = 20, 120  # leave room for icons on each side
        bar_y, bar_h = 2, 12
        radius = 4

        # "Empty" icon on left — small empty rectangle
        c.create_rectangle(4, 4, 14, 12, outline=TEAL, width=1)

        # "Full" icon on right — small filled rectangle
        c.create_rectangle(w - 14, 4, w - 4, 12, outline=TEAL, fill=TEAL, width=1)

        # Bar background (rounded rect outline)
        c.create_rectangle(bar_x, bar_y, bar_x + bar_w, bar_y + bar_h,
                           outline=TEAL, width=1, fill="")

        # Filled portion
        fill_w = max(0, min(bar_w, int(bar_w * pct / 100)))
        if fill_w > 0:
            # Pick color: teal normally, orange if > 80%, red if > 95%
            fill_color = TEAL
            if pct > 95:
                fill_color = RED
            elif pct > 80:
                fill_color = ORANGE
            c.create_rectangle(bar_x + 1, bar_y + 1,
                               bar_x + fill_w - 1, bar_y + bar_h - 1,
                               fill=fill_color, outline="")

    def _toggle_date_subfolders(self):
        self.camera.date_subfolders = self.date_subfolder_var.get()
        self.camera.save_settings()

    def _toggle_auto_open(self):
        self.camera.auto_open_folder = self.auto_open_var.get()
        self.camera.save_settings()

    def _auto_open_save_folder(self):
        """Open the save folder in the system file manager."""
        self._open_folder()

    def _send_notification(self, title: str, message: str):
        """Send a system notification (macOS/Windows/Linux)."""
        try:
            system = platform.system()
            if system == "Darwin":
                subprocess.run([
                    "osascript", "-e",
                    f'display notification "{message}" with title "{title}"'
                ], capture_output=True, timeout=5)
            elif system == "Windows":
                # Use PowerShell toast notification
                ps = (f'[Windows.UI.Notifications.ToastNotificationManager, '
                      f'Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; '
                      f'$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0); '
                      f'$xml.GetElementsByTagName("text")[0].AppendChild($xml.CreateTextNode("{title}: {message}")) > $null; '
                      f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("HumDrop").Show('
                      f'[Windows.UI.Notifications.ToastNotification]::new($xml))')
                subprocess.run(["powershell", "-Command", ps], capture_output=True, timeout=5)
            else:
                if shutil.which("notify-send"):
                    subprocess.run(["notify-send", title, message], capture_output=True, timeout=5)
        except Exception:
            pass  # Notifications are best-effort

    def _scheme_changed(self, value: str):
        schemes = list(NamingScheme)
        values = [f"{s.display_name}  ({s.example(self.camera.naming_prefix, self.camera.naming_separator)})" for s in schemes]
        try:
            idx = values.index(value)
            self.camera.naming_scheme = schemes[idx]
            self.camera.save_settings()
        except ValueError:
            pass

        is_original = self.camera.naming_scheme == NamingScheme.ORIGINAL
        state = "disabled" if is_original else "normal"
        self.prefix_entry.configure(state=state)

        # Show/hide "Camera original" warning
        if is_original:
            self.original_warning.grid(row=10, column=0, sticky="w", padx=24, pady=(4, 0))
            # One-per-session popup the first time they select it
            if not self._original_warning_shown:
                self._original_warning_shown = True
                messagebox.showwarning(
                    "Camera Original Naming",
                    "The camera reuses filenames like SCKR1000.mp4 each session.\n\n"
                    "HumDrop won't overwrite existing files \u2014 duplicates get _1, _2 "
                    "suffixes \u2014 but you'll lose dates and organization over time.\n\n"
                    "Consider using a custom naming scheme instead.")
        else:
            self.original_warning.grid_forget()

        self._update_example()

        if self.files and self.is_connected:
            self._refresh()

    def _separator_changed(self):
        self.camera.naming_separator = self.sep_var.get()
        self.camera.save_settings()
        self._update_scheme_menu()
        self._update_example()
        if self.files and self.is_connected:
            self._refresh()

    def _prefix_changed(self):
        raw = self.prefix_entry.get().strip()
        # Only strip characters that are truly illegal in filenames
        raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', raw)
        if not raw:
            raw = "cam"
        self.camera.naming_prefix = raw
        self.camera.save_settings()
        self._update_scheme_menu()
        self._update_example()
        if self.files and self.is_connected:
            self._refresh()

    def _update_scheme_menu(self):
        values = [f"{s.display_name}  ({s.example(self.camera.naming_prefix, self.camera.naming_separator)})" for s in NamingScheme]
        self.scheme_menu.configure(values=values)
        idx = list(NamingScheme).index(self.camera.naming_scheme)
        self.scheme_var.set(values[idx])

    def _update_example(self):
        ex = self.camera.naming_scheme.example(self.camera.naming_prefix, self.camera.naming_separator)
        self.example_label.configure(text=f"e.g. {ex}")

    def _show_profiles(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("Camera and Settings Presets")
        dlg.geometry("400x360")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="Camera and Settings Presets", font=ctk.CTkFont(size=18, weight="bold")).pack(
            padx=16, pady=(12, 4))
        ctk.CTkLabel(dlg, text="Save and switch between settings configurations",
                     font=ctk.CTkFont(size=14), text_color=TEXT_SEC).pack(padx=16)

        profiles = self.camera.load_profiles()

        # Profile list
        list_frame = ctk.CTkFrame(dlg, fg_color=BG_CARD, corner_radius=8)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(8, 0))

        profile_var = ctk.StringVar()

        def refresh_list():
            nonlocal profiles
            profiles = self.camera.load_profiles()
            for w in list_frame.winfo_children():
                w.destroy()
            if not profiles:
                ctk.CTkLabel(list_frame, text="No saved profiles yet.",
                             font=ctk.CTkFont(size=14), text_color=TEXT_MUTED).pack(pady=16)
            else:
                for name, cfg in profiles.items():
                    row = ctk.CTkFrame(list_frame, fg_color="transparent")
                    row.pack(fill="x", padx=8, pady=2)
                    rb = ctk.CTkRadioButton(row, text=f"{name}  ({cfg.get('camera_ip', '?')})",
                                            variable=profile_var, value=name,
                                            font=ctk.CTkFont(size=15), text_color=TEXT_PRI,
                                            fg_color=TEAL, hover_color=TEAL_HOVER,
                                            border_color=BORDER)
                    rb.pack(side="left")
                    ctk.CTkButton(row, text="X", width=28, height=28,
                                  fg_color="transparent", hover_color=("#ffe0de", "#3a1515"),
                                  text_color=RED, font=ctk.CTkFont(size=14),
                                  command=lambda n=name: (self.camera.delete_profile(n), refresh_list())
                                  ).pack(side="right")

        refresh_list()

        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(pady=(8, 12))

        def save_current():
            name = ctk.CTkInputDialog(text="Preset name:", title="Save Preset").get_input()
            if name and name.strip():
                self.camera.save_profile(name.strip())
                refresh_list()

        def load_selected():
            name = profile_var.get()
            if name and self.camera.apply_profile(name):
                # Update UI to reflect new settings
                self.ip_entry.delete(0, "end")
                self.ip_entry.insert(0, self.camera.camera_ip)
                self.folder_btn.configure(text=self._short_path(self.camera.video_dir))
                self.prefix_entry.delete(0, "end")
                self.prefix_entry.insert(0, self.camera.naming_prefix)
                self.sep_var.set(self.camera.naming_separator)
                self._update_scheme_menu()
                self._update_example()
                dlg.destroy()
                messagebox.showinfo("Preset Loaded", f"Switched to preset: {name}")

        ctk.CTkButton(btn_frame, text="Save Current", width=120, height=34,
                      fg_color=TEAL, hover_color=TEAL_HOVER, text_color="white",
                      font=ctk.CTkFont(size=15), command=save_current).pack(side="left", padx=(0, 4))
        ctk.CTkButton(btn_frame, text="Load Selected", width=120, height=34,
                      fg_color=ORANGE, hover_color=ORANGE_HOVER, text_color="white",
                      font=ctk.CTkFont(size=15), command=load_selected).pack(side="left", padx=(0, 4))
        ctk.CTkButton(btn_frame, text="Close", width=80, height=34,
                      fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
                      font=ctk.CTkFont(size=15), command=dlg.destroy).pack(side="left")

    def _log_session(self, action: str, file_count: int, auto_deleted: int = 0, notes: str = ""):
        self.camera.log_session(action, file_count, auto_deleted=auto_deleted, notes=notes)

    def _show_history(self):
        history = self.camera.get_history()
        dlg = ctk.CTkToplevel(self)
        dlg.title("Sync History")
        dlg.geometry("520x420")
        dlg.resizable(True, True)
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="Sync History", font=ctk.CTkFont(size=18, weight="bold")).pack(
            padx=16, pady=(12, 4))

        # Scrollable text area
        text_frame = ctk.CTkFrame(dlg, fg_color=BG_CARD, corner_radius=8)
        text_frame.pack(fill="both", expand=True, padx=16, pady=(4, 8))

        text_box = tk.Text(text_frame, wrap="word", bg=self._resolve(BG_CARD),
                           fg=self._resolve(TEXT_PRI), font=("Courier", 13),
                           relief="flat", bd=0, highlightthickness=0)
        scrollbar = ctk.CTkScrollbar(text_frame, command=text_box.yview)
        text_box.configure(yscrollcommand=scrollbar.set)
        text_box.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=4)

        if not history:
            text_box.insert("1.0", "No sync history yet.\n\nHistory is recorded after each download.")
        else:
            for entry in reversed(history):
                ts = entry.get("timestamp", "?")[:19].replace("T", " ")
                action = entry.get("action", "?")
                count = entry.get("files", 0)
                deleted = entry.get("auto_deleted", 0)
                ip = entry.get("camera_ip", "")
                notes = entry.get("notes", "")
                line = f"{ts}  {action}: {count} files"
                if deleted:
                    line += f", {deleted} auto-deleted"
                if ip:
                    line += f"  [{ip}]"
                if notes:
                    line += f"  ({notes})"
                text_box.insert("end", line + "\n")
        text_box.configure(state="disabled")

        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(pady=(0, 12))

        def export_csv():
            path = filedialog.asksaveasfilename(
                defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                title="Export History", initialfile="humdrop_history.csv")
            if path:
                with open(path, "w") as f:
                    f.write("timestamp,action,files,auto_deleted,camera_ip,save_folder,notes\n")
                    for e in history:
                        row = [str(e.get(k, "")) for k in
                               ["timestamp", "action", "files", "auto_deleted",
                                "camera_ip", "save_folder", "notes"]]
                        f.write(",".join(row) + "\n")
                messagebox.showinfo("Exported", f"History exported to:\n{path}")

        ctk.CTkButton(btn_frame, text="Export CSV", width=120, height=34,
                      fg_color=TEAL, hover_color=TEAL_HOVER, text_color="white",
                      font=ctk.CTkFont(size=15), command=export_csv).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_frame, text="Close", width=100, height=34,
                      fg_color=BTN_SEC, hover_color=BTN_SEC_HOVER, text_color=TEXT_PRI,
                      font=ctk.CTkFont(size=15), command=dlg.destroy).pack(side="left")

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
        ctk.CTkLabel(banner_inner, text="v0.10  \u2022  Camera Sync",
                     font=ctk.CTkFont(size=15),
                     text_color=TEAL_HOVER).pack()

        # Body
        body = ctk.CTkFrame(about, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(16, 0))

        ctk.CTkLabel(body, text="By Kenneth Russell DeGraff",
                     font=ctk.CTkFont(size=16, weight="bold")).pack()
        ctk.CTkLabel(body, text="Sync videos and photos from WiFi bird/trail cameras\n"
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
        self._stop_heartbeat()
        self._stop_watch()
        # Signal an in-flight download to stop and give it a moment to clean up
        # its partial file. Download threads are daemons and aren't joined at
        # interpreter exit, so without this a half-written file is left behind.
        if self.is_downloading:
            self._download_cancel = True
            deadline = time.time() + 3.0
            while self.is_downloading and time.time() < deadline:
                time.sleep(0.05)
        self.camera.cleanup()
        self.destroy()


# ============================================================
# MARK: - Main
# ============================================================

if __name__ == "__main__":
    app = HumDropApp()
    app.mainloop()
