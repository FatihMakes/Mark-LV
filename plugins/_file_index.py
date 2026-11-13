"""
SQLite-backed incremental filesystem indexing and metadata search engine for Smart File Finder.
Indexes selected user directories safely, preventing path traversal and omitting system files.
"""

from __future__ import annotations

import os
import sys
import time
import sqlite3
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from plugins._plugin_common import get_plugins_data_dir, format_bytes

DB_FILE = get_plugins_data_dir() / "file_index.db"

# Excluded system & sensitive patterns
EXCLUDED_DIR_NAMES = {
    "$recycle.bin", "system volume information", "windows", "appdata",
    ".git", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".cache", "recovery", "program files", "program files (x86)"
}


class FileIndexEngine:
    """Provides fast indexed search over user documents, code, media, and projects."""

    _instance: Optional[FileIndexEngine] = None

    def __init__(self) -> None:
        self.db_path = str(DB_FILE)
        self._init_db()

    @classmethod
    def get_instance(cls) -> FileIndexEngine:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _init_db(self) -> None:
        """Create the SQLite index tables."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    category TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    indexed_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_filename ON files(filename)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON files(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ext ON files(extension)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_size ON files(size_bytes)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON files(mtime)")
            conn.commit()

    @staticmethod
    def categorize_extension(ext: str) -> str:
        """Return high-level category for a given file extension."""
        e = ext.lower().lstrip(".")
        if e in ("pdf", "doc", "docx", "txt", "rtf", "odt", "md", "xlsx", "csv", "pptx"):
            return "Documents"
        elif e in ("jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico"):
            return "Images"
        elif e in ("mp4", "mkv", "avi", "mov", "wmv", "flv", "webm"):
            return "Videos"
        elif e in ("mp3", "wav", "flac", "aac", "ogg", "m4a"):
            return "Audio"
        elif e in ("zip", "rar", "7z", "tar", "gz", "bz2"):
            return "Archives"
        elif e in ("py", "js", "ts", "html", "css", "json", "c", "cpp", "h", "dart", "java", "rs", "go", "sql"):
            return "Code"
        elif e in ("exe", "msi", "dmg", "pkg", "deb", "rpm", "apk"):
            return "Installers"
        return "Other"

    def index_directory(self, root_dir: str, max_depth: int = 5, limit: int = 15000) -> Tuple[int, int]:
        """
        Incrementally index files within root_dir up to max_depth.
        Returns (new_or_updated_count, total_indexed).
        """
        root_path = pathlib.Path(root_dir).resolve()
        if not root_path.exists() or not root_path.is_dir():
            return 0, 0

        indexed_count = 0
        batch: List[Tuple[str, str, str, str, int, float, float]] = []
        now = time.time()

        for dirpath, dirnames, filenames in os.walk(str(root_path)):
            # Filter out sensitive or system directories in-place
            dirnames[:] = [d for d in dirnames if d.lower() not in EXCLUDED_DIR_NAMES and not d.startswith(".")]

            # Check depth
            rel = os.path.relpath(dirpath, str(root_path))
            if rel != "." and len(pathlib.Path(rel).parts) >= max_depth:
                dirnames.clear()
                continue

            for fname in filenames:
                if fname.startswith("~$") or fname.startswith("."):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(fpath)
                    ext = os.path.splitext(fname)[1]
                    cat = self.categorize_extension(ext)
                    batch.append((
                        fpath,
                        fname,
                        ext.lower(),
                        cat,
                        stat.st_size,
                        stat.st_mtime,
                        now
                    ))
                    indexed_count += 1
                except (OSError, PermissionError):
                    continue

                if indexed_count >= limit:
                    break
            if indexed_count >= limit:
                break

        if batch:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO files (path, filename, extension, category, size_bytes, mtime, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, batch)
                conn.commit()

        total = self.get_total_indexed()
        return len(batch), total

    def search(
        self,
        query: str = "",
        category: str = "",
        extension: str = "",
        min_size_bytes: int = 0,
        sort_by: str = "mtime",  # 'mtime', 'size', 'name'
        limit: int = 25
    ) -> List[Dict[str, Any]]:
        """Search the indexed files matching parameters."""
        clauses = []
        params: List[Any] = []

        if query:
            clauses.append("filename LIKE ?")
            params.append(f"%{query}%")

        if category:
            clauses.append("category = ?")
            params.append(category)

        if extension:
            ext = extension.lower() if extension.startswith(".") else f".{extension.lower()}"
            clauses.append("extension = ?")
            params.append(ext)

        if min_size_bytes > 0:
            clauses.append("size_bytes >= ?")
            params.append(min_size_bytes)

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        order_col = "mtime DESC"
        if sort_by == "size":
            order_col = "size_bytes DESC"
        elif sort_by == "name":
            order_col = "filename ASC"

        sql = f"SELECT path, filename, extension, category, size_bytes, mtime FROM files {where_clause} ORDER BY {order_col} LIMIT {limit}"

        results = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(sql, params)
            for r in cur.fetchall():
                results.append({
                    "path": r["path"],
                    "filename": r["filename"],
                    "extension": r["extension"],
                    "category": r["category"],
                    "size_bytes": r["size_bytes"],
                    "size_str": format_bytes(r["size_bytes"]),
                    "mtime": r["mtime"],
                    "mtime_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"]))
                })
        return results

    def get_total_indexed(self) -> int:
        """Return total indexed file count."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM files")
            row = cur.fetchone()
            return row[0] if row else 0

    def get_category_counts(self) -> Dict[str, int]:
        """Return counts by category."""
        counts = {}
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT category, COUNT(*) FROM files GROUP BY category")
            for r in cur.fetchall():
                counts[r[0]] = r[1]
        return counts
