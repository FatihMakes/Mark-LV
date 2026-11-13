"""
JARVIS Smart File Finder & Application Launcher Plugin.
Fast indexed file search across documents, projects, code, and safe desktop app launching.
"""

from __future__ import annotations

import os
import pathlib
import threading
from typing import Any, Dict, List, Optional

from memory.config_manager import get_plugin_config, get_plugin_setting
from plugins._desktop_utils import DesktopUtils
from plugins._file_index import FileIndexEngine
from plugins._plugin_common import (
    HudVisualGenerator,
    format_bytes,
    safe_player_content,
    safe_player_image,
    safe_player_log,
)

PLUGIN = {
    "name": "smart_file_finder",
    "description": (
        "Search files, documents, projects, code, and launch installed applications. "
        "Triggers: 'find my file', 'search for documents', 'open VS Code', 'launch notepad', "
        "'index my files', 'find files larger than 1GB'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'search', 'launch_app', 'index_directory', 'list_apps', or 'status'",
            },
            "query": {
                "type": "STRING",
                "description": "Filename, keyword, or application name to search/launch",
            },
            "category": {
                "type": "STRING",
                "description": "Optional category filter: 'Documents', 'Images', 'Videos', 'Code', 'Archives'",
            },
            "extension": {
                "type": "STRING",
                "description": "Optional extension filter (e.g. 'py', 'pdf', 'docx')",
            },
            "min_size_mb": {
                "type": "NUMBER",
                "description": "Minimum file size in megabytes",
            },
        },
        "required": ["action"],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "smart_file_finder",
    "title": "SMART FILE FINDER & APP LAUNCHER",
    "fields": [
        {
            "key": "indexed_root",
            "label": "Primary Folder to Index",
            "type": "text",
            "default": str(pathlib.Path.home()),
            "placeholder": "C:\\Users\\Username or ~",
        },
        {
            "key": "max_index_depth",
            "label": "Max Indexing Folder Depth",
            "type": "choice",
            "options": ["3", "5", "8"],
            "default": "5",
        },
        {
            "key": "auto_index_on_start",
            "label": "Enable Background Incremental Indexing",
            "type": "toggle",
            "default": True,
        },
    ],
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "search").lower().strip()
    query = str(parameters.get("query") or "").strip()
    category = str(parameters.get("category") or "").strip()
    extension = str(parameters.get("extension") or "").strip()
    min_size_mb = float(parameters.get("min_size_mb") or 0.0)

    engine = FileIndexEngine.get_instance()

    try:
        # 1. Action: Launch Application
        if action in ("launch_app", "open_app", "launch"):
            if not query:
                return "Sir, please specify which application to open."
            app_match = DesktopUtils.find_application(query)
            if not app_match:
                return f"Sir, I could not locate an installed application matching '{query}'."

            ok, msg = DesktopUtils.launch_application(app_match)
            safe_player_log(player, f"App Launcher: {msg}")
            safe_player_content(player, "APP LAUNCHER", f"Application: {app_match['name']}\nPath: {app_match['path']}\nStatus: {msg}")
            return f"Launching {app_match['name']} now, sir."

        # 2. Action: Index Directory
        if action in ("index_directory", "index", "reindex"):
            target_dir = query or get_plugin_setting("smart_file_finder", "indexed_root", default=str(pathlib.Path.home()))
            depth = int(get_plugin_setting("smart_file_finder", "max_index_depth", default="5"))

            # Run re-indexing in background thread
            def _bg_index():
                added, total = engine.index_directory(target_dir, max_depth=depth)
                safe_player_log(player, f"Index complete: {added} files updated. Total index: {total} files.")

            threading.Thread(target=_bg_index, daemon=True, name="FileIndexWorker").start()
            return f"Started indexing {target_dir} in the background, sir."

        # 3. Action: List Installed Applications
        if action in ("list_apps", "apps"):
            apps = DesktopUtils.get_installed_applications()
            app_names = [a["name"] for a in apps[:20]]
            summary = "\n".join(f"• {name}" for name in app_names)
            safe_player_content(player, "INSTALLED APPLICATIONS", f"Found {len(apps)} applications:\n\n{summary}")
            return f"Found {len(apps)} installed applications on your system, sir."

        # 4. Action: Search Files (Default)
        min_bytes = int(min_size_mb * 1024 * 1024)
        results = engine.search(
            query=query,
            category=category,
            extension=extension,
            min_size_bytes=min_bytes,
            limit=15
        )

        if not results:
            # If database is empty, auto-trigger indexing of User directory
            total_indexed = engine.get_total_indexed()
            if total_indexed == 0:
                user_home = str(pathlib.Path.home())
                threading.Thread(target=lambda: engine.index_directory(user_home, max_depth=4), daemon=True).start()
                return f"No files indexed yet. I have started indexing your home directory in the background, sir."
            return f"Sir, no files were found matching '{query or category or extension}'."

        # Generate Visual HUD Card
        metrics = [
            ("RESULTS", f"{len(results)} Files", "cyan"),
            ("CATEGORY", category or "All", "green"),
            ("TOTAL INDEX", f"{engine.get_total_indexed():,}", "amber"),
        ]
        table_rows = [["Filename", "Category", "Size", "Modified"]]
        for r in results[:7]:
            table_rows.append([r["filename"][:24], r["category"], r["size_str"], r["mtime_str"][:10]])

        card_path = HudVisualGenerator.create_card_image(
            title="SMART FILE FINDER",
            subtitle=f"Query: '{query}'",
            metrics=metrics,
            table_rows=table_rows,
            footer=f"Found {len(results)} matching entries",
            output_filename="file_search_card.png"
        )

        safe_player_image(player, card_path, "SMART FILE FINDER")

        # Details in bottom panel
        details = "\n".join([f"• {r['filename']} ({r['size_str']}) - {r['path']}" for r in results[:8]])
        safe_player_content(player, f"SEARCH RESULTS ({len(results)})", details)
        safe_player_log(player, f"Found {len(results)} files matching '{query}'.")

        best = results[0]["filename"]
        return f"Sir, I found {len(results)} matching files. Top result: {best}."

    except Exception as exc:
        return f"Sir, File Finder encountered an issue: {exc}"
