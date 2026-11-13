"""
JARVIS Storage Guardian Plugin.
Monitors drive space, alerts on low storage thresholds, analyzes folder consumption and temporary cache.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Dict, List, Optional

from memory.config_manager import get_plugin_config, get_plugin_setting
from plugins._plugin_common import (
    HudVisualGenerator,
    format_bytes,
    safe_player_content,
    safe_player_image,
    safe_player_log,
)
from plugins._storage_utils import StorageUtils

PLUGIN = {
    "name": "storage_guardian",
    "description": (
        "Monitor disk storage, drive usage, analyze large folders and cleanable temp files. "
        "Triggers: 'check my storage', 'check disk space', 'what is taking space on C drive', "
        "'show drive capacity', 'find large folders'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'drives', 'analyze_folder', 'temp_files', or 'status'",
            },
            "target_path": {
                "type": "STRING",
                "description": "Optional directory path to analyze folder breakdown",
            },
        },
        "required": ["action"],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "storage_guardian",
    "title": "STORAGE GUARDIAN",
    "fields": [
        {
            "key": "warn_threshold_pct",
            "label": "Storage Warning Threshold (%)",
            "type": "choice",
            "options": ["70", "80", "85", "90"],
            "default": "85",
        },
        {
            "key": "primary_drive",
            "label": "Primary Monitored Drive",
            "type": "text",
            "default": "C:\\" if os.name == "nt" else "/",
            "placeholder": "C:\\ or /",
        },
    ],
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "drives").lower().strip()
    target_path = str(parameters.get("target_path") or "").strip()

    try:
        # 1. Action: Temporary Files Check
        if action in ("temp_files", "temp", "cache"):
            temp_info = StorageUtils.find_temporary_files()
            metrics = [
                ("TEMP LOCATION", temp_info.get("temp_dir", "TEMP")[:14], "cyan"),
                ("FILES", f"{temp_info['count']:,}", "amber"),
                ("RECOVERABLE", temp_info["size_str"], "green"),
            ]
            card_path = HudVisualGenerator.create_card_image(
                title="STORAGE GUARDIAN - TEMP FILES",
                subtitle="Recoverable Cache & Temporary Data",
                metrics=metrics,
                table_rows=[["Cache Type", "Location", "Space"] , ["User Temp", temp_info.get("temp_dir", "")[:35], temp_info["size_str"]]],
                footer="Storage analysis complete",
                output_filename="storage_temp_card.png"
            )
            safe_player_image(player, card_path, "STORAGE GUARDIAN")
            safe_player_content(player, "TEMP FILE ANALYSIS", f"Temporary folder: {temp_info['temp_dir']}\nFile Count: {temp_info['count']}\nEstimated Recoverable Space: {temp_info['size_str']}")
            return f"Sir, I identified {temp_info['size_str']} of temporary cache in your temp directory."

        # 2. Action: Deep Folder Breakdown
        if action in ("analyze_folder", "analyze", "folders"):
            folder_to_scan = target_path or str(pathlib.Path.home())
            safe_player_log(player, f"Analyzing folder breakdown: {folder_to_scan}...")
            analysis = StorageUtils.analyze_folder_breakdown(folder_to_scan, top_n=6)
            if "error" in analysis:
                return f"Sir, cannot analyze directory: {analysis['error']}"

            metrics = [
                ("TARGET", folder_to_scan[:16], "cyan"),
                ("TOTAL USAGE", analysis["total_size_str"], "amber"),
                ("SUB-FOLDERS", f"{len(analysis['top_folders'])}", "green"),
            ]

            table_rows = [["Subfolder / File", "Disk Usage"]]
            for f in analysis["top_folders"]:
                table_rows.append([f["folder"][:26], f["size_str"]])

            card_path = HudVisualGenerator.create_card_image(
                title="FOLDER STORAGE BREAKDOWN",
                subtitle=folder_to_scan,
                metrics=metrics,
                table_rows=table_rows,
                footer="Top consuming directory entries",
                output_filename="folder_breakdown_card.png"
            )
            safe_player_image(player, card_path, "FOLDER BREAKDOWN")
            details = "\n".join([f"• {f['folder']}: {f['size_str']} ({f['file_count']} files)" for f in analysis["top_folders"]])
            safe_player_content(player, "DIRECTORY ANALYSIS", f"Target: {folder_to_scan}\nTotal: {analysis['total_size_str']}\n\nTop Folders:\n{details}")
            return f"Sir, the top subfolder in {folder_to_scan} is {analysis['top_folders'][0]['folder'] if analysis['top_folders'] else 'None'} ({analysis['top_folders'][0]['size_str'] if analysis['top_folders'] else '0 B'})."

        # 3. Action: All Drives Overview (Default)
        drives = StorageUtils.get_drives_overview()
        if not drives:
            return "Sir, unable to query mounted disk drives."

        warn_pct = float(get_plugin_setting("storage_guardian", "warn_threshold_pct", default="85"))
        metrics = []
        table_rows = [["Drive", "Used / Total", "Free", "Usage %", "Status"]]

        has_alert = False
        for d in drives[:4]:
            col = "red" if d["percent"] >= warn_pct else ("amber" if d["percent"] >= 75 else "green")
            metrics.append((d["mountpoint"], f"{d['percent']}% Used", col))
            if d["percent"] >= warn_pct:
                has_alert = True
            table_rows.append([
                d["mountpoint"],
                f"{d['used_str']} / {d['total_str']}",
                d["free_str"],
                f"{d['percent']}%",
                d["status"]
            ])

        card_path = HudVisualGenerator.create_card_image(
            title="STORAGE GUARDIAN - DRIVES",
            subtitle=f"{len(drives)} Active Partitions Mounted",
            metrics=metrics,
            table_rows=table_rows,
            footer="All drive systems nominal" if not has_alert else f"WARNING: Storage exceeded {warn_pct}% threshold",
            output_filename="storage_drives_card.png"
        )

        safe_player_image(player, card_path, "STORAGE GUARDIAN")
        safe_player_content(
            player,
            "STORAGE STATUS",
            "\n".join([f"• {d['mountpoint']} [{d['fstype']}]: {d['free_str']} free of {d['total_str']} ({d['percent']}% used - {d['status']})" for d in drives])
        )

        primary = drives[0]
        alert_msg = f" Warning: {primary['mountpoint']} has reached {primary['percent']}% capacity." if primary["percent"] >= warn_pct else ""
        return f"Sir, primary drive {primary['mountpoint']} has {primary['free_str']} available out of {primary['total_str']} ({primary['percent']}% used).{alert_msg}"

    except Exception as exc:
        return f"Sir, Storage Guardian encountered an issue: {exc}"
