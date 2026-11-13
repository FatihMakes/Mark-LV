"""
Shared foundation utilities for the MARK LVII Plugin Suite.
Provides safe user-data directories, HUD image generation via Pillow,
logging shims, formatting helpers, and cross-plugin discovery.
"""

from __future__ import annotations

import os
import sys
import json
import time
import socket
import pathlib
import datetime
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# Runtime data lives outside the project directory in OS user-data directory
# e.g., %APPDATA%/JARVIS_MarkLVII/plugins_data or ~/.config/jarvis_marklvii/plugins_data
def get_plugins_data_dir() -> pathlib.Path:
    """Return an isolated per-user application directory for plugin databases and caches."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        data_dir = pathlib.Path(base) / "JARVIS_MarkLVII" / "plugins_data"
    elif sys.platform == "darwin":
        data_dir = pathlib.Path.home() / "Library" / "Application Support" / "JARVIS_MarkLVII" / "plugins_data"
    else:
        data_dir = pathlib.Path.home() / ".config" / "jarvis_marklvii" / "plugins_data"
    
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def safe_player_log(player: Any, text: str) -> None:
    """Safely log text via player.write_log without raising."""
    if not player:
        return
    try:
        if hasattr(player, "write_log"):
            player.write_log(f"JARVIS: {text}")
    except Exception:
        pass


def safe_player_content(player: Any, title: str, text: str) -> None:
    """Safely display content in the bottom panel via player.show_content."""
    if not player:
        return
    try:
        if hasattr(player, "show_content"):
            player.show_content(str(title)[:48], str(text)[:4000])
    except Exception:
        pass


def safe_player_image(player: Any, image_path: str, title: str = "") -> None:
    """Safely display an image in the HUD via player.show_image."""
    if not player or not image_path or not os.path.exists(image_path):
        return
    try:
        if hasattr(player, "show_image"):
            player.show_image(str(image_path), str(title)[:48])
    except Exception:
        pass


def format_bytes(size_bytes: int | float) -> str:
    """Format bytes to human-readable string (KB, MB, GB, TB)."""
    try:
        val = float(size_bytes)
    except (ValueError, TypeError):
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if val < 1024.0 or unit == "PB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} GB"


# ── HUD Visual Card Generator ────────────────────────────────────────────────
class HudVisualGenerator:
    """Generates futuristic HUD dashboard images using Pillow for player.show_image."""

    WIDTH = 760
    HEIGHT = 440
    BG_COLOR = (4, 10, 16)
    CARD_BG = (9, 20, 30)
    BORDER_COLOR = (0, 180, 216)
    BORDER_DIM = (0, 70, 90)
    PRI_CYAN = (0, 229, 255)
    SEC_AMBER = (255, 183, 3)
    GREEN = (0, 230, 118)
    RED = (255, 82, 82)
    TEXT_MAIN = (220, 240, 250)
    TEXT_DIM = (120, 150, 170)

    @classmethod
    def create_card_image(
        cls,
        title: str,
        subtitle: str,
        metrics: List[Tuple[str, str, str]],  # (Label, Value, StatusColor)
        table_rows: Optional[List[List[str]]] = None,
        footer: str = "",
        output_filename: str = "hud_card.png"
    ) -> str:
        """Render a high-resolution dark futuristic HUD telemetry card."""
        img = Image.new("RGBA", (cls.WIDTH, cls.HEIGHT), cls.BG_COLOR)
        draw = ImageDraw.Draw(img)

        # Outer border & header frame
        draw.rectangle([10, 10, cls.WIDTH - 10, cls.HEIGHT - 10], outline=cls.BORDER_DIM, width=1)
        draw.rectangle([14, 14, cls.WIDTH - 14, 58], fill=cls.CARD_BG, outline=cls.BORDER_COLOR, width=1)

        # Corner accents
        accent_len = 12
        for (x, y) in [(10, 10), (cls.WIDTH - 10, 10), (10, cls.HEIGHT - 10), (cls.WIDTH - 10, cls.HEIGHT - 10)]:
            draw.line([(x, y), (x + (accent_len if x == 10 else -accent_len), y)], fill=cls.PRI_CYAN, width=2)
            draw.line([(x, y), (x, y + (accent_len if y == 10 else -accent_len))], fill=cls.PRI_CYAN, width=2)

        # Title & Subtitle text
        draw.text((28, 22), f"◈  {title.upper()}", fill=cls.PRI_CYAN)
        if subtitle:
            draw.text((360, 26), subtitle, fill=cls.TEXT_DIM)

        # Metric Tiles (Top section: up to 4 tiles)
        tile_y = 70
        tile_h = 75
        num_metrics = min(len(metrics), 4)
        if num_metrics > 0:
            tile_w = (cls.WIDTH - 28 - (num_metrics - 1) * 8) // num_metrics
            for i, (label, val, col_name) in enumerate(metrics[:num_metrics]):
                tx = 14 + i * (tile_w + 8)
                draw.rectangle([tx, tile_y, tx + tile_w, tile_y + tile_h], fill=cls.CARD_BG, outline=cls.BORDER_DIM, width=1)
                draw.text((tx + 12, tile_y + 10), label.upper(), fill=cls.TEXT_DIM)
                color = cls.GREEN if col_name == "green" else (cls.RED if col_name == "red" else (cls.SEC_AMBER if col_name == "amber" else cls.PRI_CYAN))
                draw.text((tx + 12, tile_y + 32), str(val), fill=color)

        # Data Rows / Table (Middle section)
        table_y = tile_y + tile_h + 16
        if table_rows:
            draw.rectangle([14, table_y, cls.WIDTH - 14, cls.HEIGHT - 44], fill=cls.CARD_BG, outline=cls.BORDER_DIM, width=1)
            row_y = table_y + 12
            for row in table_rows[:8]:
                row_str = "    │    ".join(str(cell) for cell in row)
                draw.text((26, row_y), row_str, fill=cls.TEXT_MAIN)
                row_y += 24
                if row_y > cls.HEIGHT - 60:
                    break

        # Footer
        if footer:
            draw.text((20, cls.HEIGHT - 32), f"MARK LVII SYSTEM // {footer.upper()}", fill=cls.TEXT_DIM)
            draw.text((cls.WIDTH - 160, cls.HEIGHT - 32), time.strftime("%Y-%m-%d %H:%M"), fill=cls.PRI_CYAN)

        # Save to user data folder
        out_path = get_plugins_data_dir() / output_filename
        img.save(str(out_path), "PNG")
        return str(out_path)
