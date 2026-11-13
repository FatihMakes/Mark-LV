"""
Comprehensive test suite for the 8 Advanced Daily-Use Plugins.
Tests plugin discovery, signatures, schema validations, execution with player=None,
mocked player HUD methods, and graceful degradation.
"""

from __future__ import annotations

import os
import sys
import pathlib
import pytest
from unittest.mock import MagicMock

# Project root in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.plugin_loader import discover_plugins

PLUGINS_DIR = pathlib.Path(__file__).resolve().parent.parent

EXPECTED_PLUGINS = [
    "jarvis_command_center",
    "email_intelligence",
    "calendar_companion",
    "storage_guardian",
    "downloads_organizer",
    "smart_file_finder",
    "network_intelligence",
    "map_travel_intelligence",
]


def test_all_eight_plugins_discovered():
    """Verify that all 8 plugins are discovered and valid according to core.plugin_loader."""
    registry = discover_plugins(PLUGINS_DIR, core_tool_names=set())
    ui_list = registry.list_for_ui()
    valid_names = [item["name"] for item in ui_list if item["valid"]]

    for p in EXPECTED_PLUGINS:
        assert p in valid_names, f"Plugin {p} was not discovered or is marked invalid!"


def test_helper_files_not_discovered_as_plugins():
    """Verify that helper modules prefixed with '_' are not registered as tools."""
    registry = discover_plugins(PLUGINS_DIR, core_tool_names=set())
    ui_list = registry.list_for_ui()
    plugin_names = [item["name"] for item in ui_list]

    for name in plugin_names:
        assert not name.startswith("_"), f"Helper file registered as plugin: {name}"


@pytest.mark.parametrize("plugin_name", EXPECTED_PLUGINS)
def test_plugin_execution_with_none_player(plugin_name):
    """Verify each plugin's run() function executes safely without crashing when player=None."""
    mod = __import__(f"plugins.{plugin_name}", fromlist=["run", "PLUGIN"])
    assert hasattr(mod, "PLUGIN")
    assert hasattr(mod, "run")
    assert callable(mod.run)

    # Execute with default/status action
    res = mod.run({"action": "status"}, player=None)
    assert isinstance(res, str)
    assert len(res) > 0


def test_network_intelligence_functional():
    """Verify Network Intelligence diagnostics and public IP retrieval."""
    from plugins import network_intelligence
    mock_player = MagicMock()
    res = network_intelligence.run({"action": "diagnostics"}, player=mock_player)
    assert isinstance(res, str)
    assert "internet" in res.lower() or "network" in res.lower() or "offline" in res.lower()
    mock_player.show_image.assert_called()


def test_storage_guardian_functional():
    """Verify Storage Guardian drive reporting."""
    from plugins import storage_guardian
    mock_player = MagicMock()
    res = storage_guardian.run({"action": "drives"}, player=mock_player)
    assert isinstance(res, str)
    assert "drive" in res.lower() or "storage" in res.lower()
    mock_player.show_image.assert_called()


def test_downloads_organizer_functional():
    """Verify Downloads Organizer scanning and previewing."""
    from plugins import downloads_organizer
    mock_player = MagicMock()
    res = downloads_organizer.run({"action": "scan"}, player=mock_player)
    assert isinstance(res, str)
    assert "download" in res.lower() or "files" in res.lower()
    mock_player.show_image.assert_called()


def test_smart_file_finder_functional():
    """Verify Smart File Finder app discovery and search."""
    from plugins import smart_file_finder
    mock_player = MagicMock()
    res = smart_file_finder.run({"action": "list_apps"}, player=mock_player)
    assert isinstance(res, str)
    assert "installed" in res.lower() or "application" in res.lower()


def test_email_and_calendar_unconfigured_graceful():
    """Verify Email Intelligence & Calendar Companion handle unconfigured credentials gracefully."""
    from plugins import email_intelligence, calendar_companion
    mock_player = MagicMock()

    res_email = email_intelligence.run({"action": "unread"}, player=mock_player)
    assert isinstance(res_email, str)
    assert "gmail" in res_email.lower() or "connected" in res_email.lower() or "inbox" in res_email.lower()

    res_cal = calendar_companion.run({"action": "agenda"}, player=mock_player)
    assert isinstance(res_cal, str)
    assert "calendar" in res_cal.lower() or "connected" in res_cal.lower() or "events" in res_cal.lower()


def test_command_center_aggregates_all():
    """Verify Command Center aggregates data from sibling plugins without throwing."""
    from plugins import jarvis_command_center
    mock_player = MagicMock()
    res = jarvis_command_center.run({"action": "briefing"}, player=mock_player)
    assert isinstance(res, str)
    assert len(res) > 20
    mock_player.show_image.assert_called()
    mock_player.show_content.assert_called()


def test_map_travel_intelligence_functional():
    """Verify Map and Travel Intelligence loads and generates preview card."""
    from plugins import map_travel_intelligence
    mock_player = MagicMock()
    res = map_travel_intelligence.run({"action": "open"}, player=mock_player)
    assert isinstance(res, str)
    mock_player.show_image.assert_called()
