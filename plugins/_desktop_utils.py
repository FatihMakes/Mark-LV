"""
Desktop and application launcher utilities for Smart File Finder.
Detects installed applications on Windows, macOS, and Linux safely without arbitrary execution.
"""

from __future__ import annotations

import os
import sys
import glob
import pathlib
import subprocess
from typing import Dict, List, Optional, Tuple


class DesktopUtils:
    """Discovers installed applications and launches validated executables safely."""

    _app_cache: Optional[List[Dict[str, str]]] = None

    @classmethod
    def get_installed_applications(cls, force_refresh: bool = False) -> List[Dict[str, str]]:
        """Return list of installed applications with name and executable/shortcut path."""
        if cls._app_cache is not None and not force_refresh:
            return cls._app_cache

        apps: List[Dict[str, str]] = []

        if sys.platform == "win32":
            # Search Windows Start Menu shortcuts and system directories
            search_dirs = [
                os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
                os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs"),
            ]
            for s_dir in search_dirs:
                if not os.path.exists(s_dir):
                    continue
                for root, _, files in os.walk(s_dir):
                    for file in files:
                        if file.lower().endswith((".lnk", ".url")):
                            name = os.path.splitext(file)[0]
                            full_path = os.path.join(root, file)
                            # Avoid duplicates by name
                            if not any(a["name"].lower() == name.lower() for a in apps):
                                apps.append({
                                    "name": name,
                                    "path": full_path,
                                    "type": "shortcut",
                                    "category": "Application"
                                })

            # Common standard tools
            common_tools = [
                ("Notepad", "notepad.exe"),
                ("Calculator", "calc.exe"),
                ("Explorer", "explorer.exe"),
                ("Task Manager", "taskmgr.exe"),
                ("Command Prompt", "cmd.exe"),
                ("PowerShell", "powershell.exe"),
                ("Registry Editor", "regedit.exe"),
                ("Paint", "mspaint.exe"),
            ]
            for name, cmd in common_tools:
                if not any(a["name"].lower() == name.lower() for a in apps):
                    apps.append({
                        "name": name,
                        "path": cmd,
                        "type": "system",
                        "category": "System Tool"
                    })

        elif sys.platform == "darwin":
            # macOS /Applications
            app_paths = glob.glob("/Applications/*.app") + glob.glob(os.path.expanduser("~/Applications/*.app"))
            for p in app_paths:
                name = os.path.splitext(os.path.basename(p))[0]
                apps.append({"name": name, "path": p, "type": "app_bundle", "category": "Application"})
        else:
            # Linux /usr/share/applications/*.desktop
            desktop_files = glob.glob("/usr/share/applications/*.desktop") + glob.glob(os.path.expanduser("~/.local/share/applications/*.desktop"))
            for p in desktop_files:
                name = os.path.splitext(os.path.basename(p))[0]
                apps.append({"name": name, "path": p, "type": "desktop_file", "category": "Application"})

        cls._app_cache = sorted(apps, key=lambda a: a["name"].lower())
        return cls._app_cache

    @classmethod
    def find_application(cls, query: str) -> Optional[Dict[str, str]]:
        """Search for an application matching the query string."""
        q = query.lower().strip()
        all_apps = cls.get_installed_applications()

        # 1. Exact match
        for a in all_apps:
            if a["name"].lower() == q:
                return a

        # 2. Starts with
        for a in all_apps:
            if a["name"].lower().startswith(q):
                return a

        # 3. Substring match
        for a in all_apps:
            if q in a["name"].lower():
                return a

        return None

    @classmethod
    def launch_application(cls, app_entry: Dict[str, str]) -> Tuple[bool, str]:
        """Safely launch an application entry."""
        path = app_entry.get("path", "")
        name = app_entry.get("name", "Application")
        if not path:
            return False, "Invalid application path."

        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return True, f"Successfully launched {name}."
        except Exception as exc:
            return False, f"Failed to launch {name}: {exc}"
