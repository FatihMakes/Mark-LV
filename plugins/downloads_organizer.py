"""
JARVIS Downloads and File Organizer Plugin.
Categorizes downloads, detects large & duplicate files, provides reversible organization with undo.
"""

from __future__ import annotations

import os
import pathlib
import threading
from typing import Any, Dict, List, Optional

from memory.config_manager import get_plugin_config, get_plugin_setting
from plugins._file_operations import FileOrganizerEngine
from plugins._plugin_common import (
    HudVisualGenerator,
    format_bytes,
    safe_player_content,
    safe_player_image,
    safe_player_log,
)

PLUGIN = {
    "name": "downloads_organizer",
    "description": (
        "Organize, sort, scan, and categorize downloads. "
        "Triggers: 'organize my downloads', 'preview downloads organization', "
        "'clean downloads', 'undo downloads organization', 'scan downloads'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'scan', 'preview', 'apply', 'undo', or 'status'",
            },
            "folder_path": {
                "type": "STRING",
                "description": "Optional custom folder path to organize (defaults to user Downloads)",
            },
        },
        "required": ["action"],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "downloads_organizer",
    "title": "DOWNLOADS & FILE ORGANIZER",
    "fields": [
        {
            "key": "downloads_path",
            "label": "Downloads Directory Path",
            "type": "text",
            "default": str(pathlib.Path.home() / "Downloads"),
            "placeholder": "C:\\Users\\Username\\Downloads",
        },
        {
            "key": "auto_organize_subfolders",
            "label": "Auto-create category subfolders",
            "type": "toggle",
            "default": True,
        },
    ],
}


def _get_target_folder(custom_path: str = "") -> str:
    if custom_path and os.path.exists(custom_path):
        return custom_path
    configured = get_plugin_setting("downloads_organizer", "downloads_path")
    if configured and os.path.exists(configured):
        return configured
    return str(pathlib.Path.home() / "Downloads")


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "scan").lower().strip()
    custom_folder = str(parameters.get("folder_path") or "").strip()
    target_folder = _get_target_folder(custom_folder)

    engine = FileOrganizerEngine()

    try:
        # 1. Action: Undo Last Batch
        if action in ("undo", "revert"):
            restored, msg = engine.undo_last_batch()
            safe_player_log(player, f"Organizer Undo: {msg}")
            safe_player_content(player, "ORGANIZER UNDO", msg)
            return f"Sir, I have undone the previous organization batch: {restored} files restored."

        # 2. Action: Preview Organization
        if action in ("preview", "plan"):
            plan = engine.preview_organization(target_folder)
            if not plan:
                return f"Sir, your Downloads folder contains no unorganized files requiring relocation."

            # Categorize breakdown
            cat_counts: Dict[str, int] = {}
            for item in plan:
                cat_counts[item["category"]] = cat_counts.get(item["category"], 0) + 1

            summary_rows = [["Filename", "Target Folder", "Size", "Conflict"]]
            for item in plan[:7]:
                summary_rows.append([
                    item["filename"][:24],
                    item["category"],
                    item["size_str"],
                    "Conflict!" if item["conflict"] else "Ready"
                ])

            metrics = [
                ("UNSORTED", f"{len(plan)} Files", "amber"),
                ("CATEGORIES", f"{len(cat_counts)} Folders", "cyan"),
                ("CONFLICTS", f"{sum(1 for p in plan if p['conflict'])}", "red" if any(p['conflict'] for p in plan) else "green"),
            ]

            card_path = HudVisualGenerator.create_card_image(
                title="DOWNLOADS ORGANIZER - PREVIEW",
                subtitle=target_folder,
                metrics=metrics,
                table_rows=summary_rows,
                footer="Say 'organize my downloads' to apply safely",
                output_filename="downloads_preview_card.png"
            )
            safe_player_image(player, card_path, "DOWNLOADS ORGANIZER")
            safe_player_content(player, "ORGANIZATION PREVIEW", f"Planned moves for {len(plan)} files:\n\n" + "\n".join([f"• {p['filename']} → {p['category']}" for p in plan[:10]]))
            return f"Sir, preview ready for {len(plan)} files. Say 'apply' or 'organize' to execute safely."

        # 3. Action: Apply Organization
        if action in ("apply", "organize", "execute"):
            plan = engine.preview_organization(target_folder)
            if not plan:
                return "Sir, no files currently require reorganization."

            success, skipped, batch_id = engine.apply_organization(plan)
            safe_player_log(player, f"Organized {success} files into categories (Skipped: {skipped}). Batch: {batch_id}")
            safe_player_content(player, "ORGANIZATION COMPLETE", f"Successfully sorted {success} files.\nSkipped: {skipped}\nBatch ID: {batch_id}\n\nYou can say 'undo' at any time to restore files.")
            return f"Sir, I have organized {success} downloads into their respective category folders."

        # 4. Action: Scan / Status (Default)
        scan = engine.scan_downloads_folder(target_folder)
        if "error" in scan:
            return f"Sir, cannot scan folder: {scan['error']}"

        metrics = [
            ("TOTAL FILES", f"{scan['total_files']}", "cyan"),
            ("TOTAL SIZE", scan["total_size_str"], "amber"),
            ("CATEGORIES", f"{len(scan['categories'])}", "green"),
        ]

        table_rows = [["Category", "File Count"]]
        for c, count in scan["categories"].items():
            table_rows.append([c, f"{count} files"])

        card_path = HudVisualGenerator.create_card_image(
            title="DOWNLOADS GUARDIAN",
            subtitle=target_folder,
            metrics=metrics,
            table_rows=table_rows,
            footer=f"{scan['total_files']} files awaiting review",
            output_filename="downloads_status_card.png"
        )
        safe_player_image(player, card_path, "DOWNLOADS STATUS")
        safe_player_content(player, "DOWNLOADS STATUS", f"Target: {target_folder}\nTotal: {scan['total_files']} files ({scan['total_size_str']})\n\nCategories:\n" + "\n".join([f"• {k}: {v}" for k, v in scan["categories"].items()]))
        return f"Sir, your Downloads folder contains {scan['total_files']} files totaling {scan['total_size_str']}."

    except Exception as exc:
        return f"Sir, Downloads Organizer encountered an issue: {exc}"
