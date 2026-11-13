"""
Disk and storage analysis engine for Storage Guardian.
Provides disk capacity overviews, folder size hierarchies, large file discovery,
and temporary file analysis safely without automatic deletions.
"""

from __future__ import annotations

import os
import sys
import psutil
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from plugins._plugin_common import format_bytes


class StorageUtils:
    """Safe disk partitions query and deep directory storage usage analyzer."""

    @classmethod
    def get_drives_overview(cls) -> List[Dict[str, Any]]:
        """Return status of all mounted disk partitions."""
        drives: List[Dict[str, Any]] = []
        partitions = psutil.disk_partitions(all=False)

        for part in partitions:
            try:
                usage = psutil.disk_usage(part.mountpoint)
                pct = usage.percent
                status = "CRITICAL" if pct >= 90 else ("WARNING" if pct >= 80 else "NOMINAL")
                drives.append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "opts": part.opts,
                    "total_bytes": usage.total,
                    "total_str": format_bytes(usage.total),
                    "used_bytes": usage.used,
                    "used_str": format_bytes(usage.used),
                    "free_bytes": usage.free,
                    "free_str": format_bytes(usage.free),
                    "percent": pct,
                    "status": status
                })
            except (PermissionError, OSError):
                continue
        return drives

    @classmethod
    def analyze_folder_breakdown(cls, target_dir: str, top_n: int = 10) -> Dict[str, Any]:
        """Analyze direct child folders and large files within target_dir."""
        p = pathlib.Path(target_dir).resolve()
        if not p.exists() or not p.is_dir():
            return {"error": f"Folder not found: {target_dir}"}

        folder_sizes: List[Dict[str, Any]] = []
        large_files: List[Dict[str, Any]] = []
        total_examined_size = 0

        try:
            for entry in p.iterdir():
                try:
                    if entry.is_dir() and not entry.name.startswith((".", "$")):
                        # Fast estimation of immediate subfolder size
                        f_size = 0
                        f_count = 0
                        for root, _, files in os.walk(str(entry)):
                            for f in files:
                                fp = os.path.join(root, f)
                                try:
                                    s = os.path.getsize(fp)
                                    f_size += s
                                    f_count += 1
                                    if s > 50 * 1024 * 1024:  # > 50 MB
                                        large_files.append({
                                            "name": f,
                                            "path": fp,
                                            "size": s,
                                            "size_str": format_bytes(s)
                                        })
                                except (OSError, PermissionError):
                                    pass
                        folder_sizes.append({
                            "folder": entry.name,
                            "path": str(entry),
                            "size": f_size,
                            "size_str": format_bytes(f_size),
                            "file_count": f_count
                        })
                        total_examined_size += f_size
                    elif entry.is_file() and not entry.name.startswith((".", "~$")):
                        sz = entry.stat().st_size
                        total_examined_size += sz
                        if sz > 10 * 1024 * 1024:
                            large_files.append({
                                "name": entry.name,
                                "path": str(entry),
                                "size": sz,
                                "size_str": format_bytes(sz)
                            })
                except (OSError, PermissionError):
                    continue
        except Exception as exc:
            return {"error": str(exc)}

        folder_sizes.sort(key=lambda x: x["size"], reverse=True)
        large_files.sort(key=lambda x: x["size"], reverse=True)

        return {
            "target": str(p),
            "total_size": total_examined_size,
            "total_size_str": format_bytes(total_examined_size),
            "top_folders": folder_sizes[:top_n],
            "top_large_files": large_files[:top_n]
        }

    @classmethod
    def find_temporary_files(cls) -> Dict[str, Any]:
        """Scan standard user TEMP directory for cleanup opportunities."""
        temp_dir = os.environ.get("TEMP") or "/tmp"
        p = pathlib.Path(temp_dir).resolve()
        if not p.exists():
            return {"count": 0, "size": 0, "size_str": "0 B"}

        count = 0
        total_bytes = 0
        try:
            for root, _, files in os.walk(str(p)):
                for f in files:
                    try:
                        sz = os.path.getsize(os.path.join(root, f))
                        total_bytes += sz
                        count += 1
                    except (OSError, PermissionError):
                        continue
        except Exception:
            pass

        return {
            "temp_dir": str(p),
            "count": count,
            "size": total_bytes,
            "size_str": format_bytes(total_bytes)
        }
