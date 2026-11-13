"""
Reversible file organization, classification, and undo journal for Downloads and File Organizer.
Enforces strict preview-first safety, never silently overwrites, and logs every move in SQLite.
"""

from __future__ import annotations

import os
import sys
import time
import shutil
import hashlib
import sqlite3
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from plugins._plugin_common import get_plugins_data_dir, format_bytes

JOURNAL_DB = get_plugins_data_dir() / "file_operations_journal.db"

# Mapping extensions to target folder subdirectories
CATEGORY_FOLDERS = {
    "Documents": ["pdf", "doc", "docx", "txt", "rtf", "odt", "md", "xlsx", "csv", "pptx", "epub"],
    "Images": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico", "psd", "ai", "raw"],
    "Videos": ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v"],
    "Audio": ["mp3", "wav", "flac", "aac", "ogg", "m4a", "wma"],
    "Archives": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"],
    "Installers": ["exe", "msi", "dmg", "pkg", "deb", "rpm", "apk"],
    "Code": ["py", "js", "ts", "html", "css", "json", "c", "cpp", "h", "dart", "java", "rs", "go", "sql"],
}


class FileOrganizerEngine:
    """Safely categorizes, previews, moves, and undoes file organization batches."""

    def __init__(self) -> None:
        self.db_path = str(JOURNAL_DB)
        self._init_journal()

    def _init_journal(self) -> None:
        """Create operations journal table."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    dest_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    timestamp REAL NOT NULL,
                    undone INTEGER DEFAULT 0
                )
            """)
            conn.commit()

    @staticmethod
    def get_category_for_filename(filename: str) -> str:
        """Determine folder category by extension."""
        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        for cat, ext_list in CATEGORY_FOLDERS.items():
            if ext in ext_list:
                return cat
        return "Other"

    def scan_downloads_folder(self, folder_path: str) -> Dict[str, Any]:
        """Scan a folder and return statistics on unsorted files, categories, and duplicates."""
        fpath = pathlib.Path(folder_path).resolve()
        if not fpath.exists() or not fpath.is_dir():
            return {"error": f"Directory not found: {folder_path}"}

        files_list = []
        total_size = 0
        categories: Dict[str, int] = {}
        large_files = []
        now = time.time()

        for item in fpath.iterdir():
            # Only consider top-level root files (do not mess with subdirectories)
            if item.is_file() and not item.name.startswith((".", "~$")):
                try:
                    stat = item.stat()
                    size = stat.st_size
                    total_size += size
                    cat = self.get_category_for_filename(item.name)
                    categories[cat] = categories.get(cat, 0) + 1

                    f_info = {
                        "name": item.name,
                        "path": str(item),
                        "size": size,
                        "size_str": format_bytes(size),
                        "category": cat,
                        "mtime": stat.st_mtime,
                        "age_days": round((now - stat.st_mtime) / 86400, 1)
                    }
                    files_list.append(f_info)

                    if size > 100 * 1024 * 1024:  # > 100 MB
                        large_files.append(f_info)
                except (OSError, PermissionError):
                    continue

        return {
            "folder": str(fpath),
            "total_files": len(files_list),
            "total_size": total_size,
            "total_size_str": format_bytes(total_size),
            "categories": categories,
            "large_files": sorted(large_files, key=lambda x: x["size"], reverse=True)[:10],
            "files": files_list
        }

    def preview_organization(self, source_folder: str, target_base: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Preview planned moves without touching any file on disk.
        Returns list of {source, destination, category, filename, size_str, conflict}.
        """
        src = pathlib.Path(source_folder).resolve()
        base = pathlib.Path(target_base).resolve() if target_base else src
        preview: List[Dict[str, Any]] = []

        if not src.exists():
            return preview

        for item in src.iterdir():
            if item.is_file() and not item.name.startswith((".", "~$")):
                cat = self.get_category_for_filename(item.name)
                dest_dir = base / cat
                dest_file = dest_dir / item.name
                conflict = dest_file.exists()
                preview.append({
                    "source": str(item),
                    "destination": str(dest_file),
                    "category": cat,
                    "filename": item.name,
                    "size": item.stat().st_size if item.exists() else 0,
                    "size_str": format_bytes(item.stat().st_size if item.exists() else 0),
                    "conflict": conflict
                })
        return preview

    def apply_organization(self, preview_plan: List[Dict[str, Any]]) -> Tuple[int, int, str]:
        """
        Execute planned organization. Never overwrites existing destination files.
        Logs each move in the journal.
        Returns (success_count, skipped_count, batch_id).
        """
        batch_id = f"BATCH_{int(time.time())}"
        success = 0
        skipped = 0
        journal_entries = []

        for p in preview_plan:
            src = pathlib.Path(p["source"])
            dest = pathlib.Path(p["destination"])

            if not src.exists():
                skipped += 1
                continue

            # Conflict protection
            if dest.exists():
                skipped += 1
                continue

            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                success += 1
                journal_entries.append((
                    batch_id,
                    str(src),
                    str(dest),
                    p["filename"],
                    p["size"],
                    time.time()
                ))
            except Exception:
                skipped += 1

        if journal_entries:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany("""
                    INSERT INTO journal (batch_id, source_path, dest_path, filename, size_bytes, timestamp, undone)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                """, journal_entries)
                conn.commit()

        return success, skipped, batch_id

    def undo_last_batch(self) -> Tuple[int, str]:
        """Undo the most recent file organization batch, moving files back to their original paths."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT batch_id FROM journal WHERE undone = 0 ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                return 0, "No reversible organization batches found in journal."

            batch_id = row["batch_id"]
            cur.execute("SELECT id, source_path, dest_path FROM journal WHERE batch_id = ? AND undone = 0", (batch_id,))
            entries = cur.fetchall()

            restored = 0
            for e in entries:
                src_orig = pathlib.Path(e["source_path"])
                curr_dest = pathlib.Path(e["dest_path"])

                if curr_dest.exists() and not src_orig.exists():
                    try:
                        src_orig.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(curr_dest), str(src_orig))
                        restored += 1
                        conn.execute("UPDATE journal SET undone = 1 WHERE id = ?", (e["id"],))
                    except Exception:
                        pass
            conn.commit()
            return restored, f"Restored {restored} files from batch {batch_id}."
