# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

APP_NAME = "antilagphotoshop"
REPORT_DIR_NAME = "diagnostics"
LOG_TAIL_BYTES = 100_000
EVENT_MESSAGE_LIMIT = 2_000


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def appdata_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME


def diagnostics_root() -> Path:
    return appdata_dir() / REPORT_DIR_NAME


def prepare_output_dir(requested: Optional[str]) -> Path:
    if requested:
        target = Path(requested).expanduser()
        if target.exists():
            raise FileExistsError(f"Output path already exists: {target}")
        target.mkdir(parents=True, exist_ok=False)
        return target

    root = diagnostics_root()
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"report-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        target = root / name
        try:
            target.mkdir(parents=False, exist_ok=False)
            return target
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not create a unique report path in {root}")


def available_ram_mb() -> Optional[int]:
    if os.name != "nt":
        return None
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    try:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys // (1024 * 1024))
    except Exception:
        return None
    return None


def disk_free_bytes(path_text: Optional[str]) -> Optional[int]:
    if not path_text:
        return None
    try:
        root = Path(path_text).anchor or str(Path(path_text))
        return int(shutil.disk_usage(root).free)
    except Exception:
        return None


def disk_snapshot(label: str, path_text: Optional[str], note: Optional[str] = None) -> Dict[str, Any]:
    free_bytes = disk_free_bytes(path_text)
    item = {
        "label": label,
        "path": path_text,
        "free_bytes": free_bytes,
        "free_mb": None if free_bytes is None else int(free_bytes // (1024 * 1024)),
    }
    if note:
        item["note"] = note
    return item


def read_configured_photoshop_path(base_dir: Path) -> Dict[str, Any]:
    config_file = base_dir / "photoshop_path.txt"
    configured_path = None
    if config_file.is_file():
        try:
            configured_path = config_file.read_text(encoding="utf-8-sig").strip() or None
        except (OSError, UnicodeError):
            configured_path = None
    return {
        "config_file": str(config_file),
        "configured_path": configured_path,
        "configured_path_exists": bool(configured_path and Path(configured_path).is_file()),
    }


def read_text_tail(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> Dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "status": "missing", "bytes_read": 0, "text": ""}
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
        return {
            "path": str(path),
            "status": "ok",
            "bytes_read": len(data),
            "truncated": size > len(data),
            "text": data.decode("utf-8", errors="replace"),
        }
    except OSError as exc:
        return {"path": str(path), "status": f"error: {exc}", "bytes_read": 0, "text": ""}


def collect_windows_application_events() -> Dict[str, Any]:
    if os.name != "nt":
        return {
            "status": "unsupported",
            "reason": "Windows Application event collection is available only on Windows.",
            "events": [],
        }
    script = rf"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$events = @(
  Get-WinEvent -FilterHashtable @{{LogName='Application'; Id=1000,1001,1002; StartTime=(Get-Date).AddDays(-7)}} -MaxEvents 200 -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Message -match 'Photoshop\.exe|Adobe Photoshop' }} |
  Select-Object TimeCreated, Id, ProviderName, LevelDisplayName,
    @{{Name='Message'; Expression={{
      $m = [string]$_.Message
      if ($m.Length -gt {EVENT_MESSAGE_LIMIT}) {{ $m.Substring(0, {EVENT_MESSAGE_LIMIT}) + '...[truncated]' }} else {{ $m }}
    }}}}
)
$events | ConvertTo-Json -Depth 3
""".strip()
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return {"status": "error", "reason": "PowerShell was not found.", "events": []}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "reason": "PowerShell timed out after 20 seconds.", "events": []}

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if not stdout:
        if result.returncode == 0:
            return {"status": "no_events", "events": []}
        return {
            "status": "error",
            "reason": stderr or f"PowerShell exited with code {result.returncode}.",
            "events": [],
        }
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "reason": "PowerShell returned non-JSON output.",
            "raw_output": stdout[:4_000],
            "events": [],
        }
    events = parsed if isinstance(parsed, list) else [parsed]
    return {
        "status": "ok" if events else "no_events",
        "event_ids": [1000, 1001, 1002],
        "days": 7,
        "max_events": 200,
        "events": events,
    }


def build_report() -> Dict[str, Any]:
    base_dir = app_dir()
    appdata = appdata_dir()
    configured = read_configured_photoshop_path(base_dir)
    temp_path = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    install_path = configured["configured_path"] or str(base_dir)
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "privacy_note": (
            "This report stays local. It can contain private file paths and Windows event messages. "
            "Review it before you publish it anywhere."
        ),
        "scope": [
            "Read-only diagnostics only",
            "No Photoshop launch or shutdown",
            "No registry, preference, plugin, or driver changes",
            "No deletion",
            "No network or upload",
        ],
        "system": {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "available_ram_mb": available_ram_mb(),
        },
        "paths": {
            "application_directory": str(base_dir),
            "appdata_directory": str(appdata),
            "temp_directory": temp_path,
        },
        "configured_photoshop": configured,
        "disk_free_space": {
            "system_drive": disk_snapshot("System drive", os.environ.get("SystemDrive", "C:\\")),
            "temp_drive": disk_snapshot("Temp drive", temp_path),
            "install_drive": disk_snapshot(
                "Configured Photoshop install drive",
                install_path,
                "Drive free space only. This does not identify Photoshop scratch disk settings.",
            ),
        },
        "logs": {
            "guard_log": read_text_tail(appdata / "antilagphotoshop.log"),
            "supervisor_log": read_text_tail(appdata / "supervisor.log"),
        },
        "windows_application_events": collect_windows_application_events(),
    }


def render_text(report: Dict[str, Any]) -> str:
    lines = [
        "AntiLagPhotoshop local diagnostics",
        f"Created UTC: {report['created_at_utc']}",
        "",
        f"Privacy note: {report['privacy_note']}",
        "",
        "Scope:",
    ]
    lines.extend(f"- {item}" for item in report["scope"])
    system = report["system"]
    lines.extend([
        "",
        "System:",
        f"- Platform: {system['platform']}",
        f"- Python: {system['python_version']}",
        f"- Python executable: {system['python_executable']}",
        f"- Available RAM MB: {system['available_ram_mb']}",
        "",
        "Configured Photoshop:",
        f"- Config file: {report['configured_photoshop']['config_file']}",
        f"- Configured path: {report['configured_photoshop']['configured_path']}",
        f"- Path exists: {report['configured_photoshop']['configured_path_exists']}",
        "",
        "Disk free space:",
    ])
    for item in report["disk_free_space"].values():
        line = f"- {item['label']}: {item['free_mb']} MB free from {item['path']}"
        if item.get("note"):
            line += f" ({item['note']})"
        lines.append(line)
    lines.append("")
    lines.append("Windows Application events for Photoshop:")
    events = report["windows_application_events"]
    if events["status"] == "ok":
        for event in events["events"]:
            lines.extend([
                f"- {event.get('TimeCreated')} | ID {event.get('Id')} | {event.get('ProviderName')} | {event.get('LevelDisplayName')}",
                f"  {str(event.get('Message', '')).replace(os.linesep, os.linesep + '  ')}",
            ])
    elif events["status"] == "no_events":
        lines.append("- No matching events found in the last 7 days.")
    else:
        lines.append(f"- {events['status']}: {events.get('reason')}")
    lines.extend(["", "Guard log tail:", report["logs"]["guard_log"]["text"], "", "Supervisor log tail:", report["logs"]["supervisor_log"]["text"], ""])
    return "\n".join(lines)


def write_report_bundle(report: Dict[str, Any], output_dir: Path) -> Tuple[Path, Path]:
    json_path = output_dir / "diagnostics.json"
    text_path = output_dir / "diagnostics.txt"
    if json_path.exists() or text_path.exists():
        raise FileExistsError(f"Report files already exist in {output_dir}")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    text_path.write_text(render_text(report), encoding="utf-8")
    return text_path, json_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect local read-only diagnostics for antilagphotoshop.")
    parser.add_argument("--output", help="Optional output directory path. It must not already exist.")
    args = parser.parse_args(argv)

    if sys.version_info < (3, 9):
        print("Python 3.9 or newer is required.", file=sys.stderr)
        return 2
    try:
        output_dir = prepare_output_dir(args.output)
        report = build_report()
        text_path, json_path = write_report_bundle(report, output_dir)
    except Exception as exc:
        print(f"Diagnostics failed: {exc}", file=sys.stderr)
        return 1

    print("Diagnostics created locally. Review the files before sharing them because paths and event messages can be sensitive.")
    print(f"Text report: {text_path}")
    print(f"JSON report: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
