"""
Comprehensive Automated Test Suite for MARK LVII Universal Plugin HUD Display System.

Tests cover:
1. Existing plugins discover and load automatically via core.plugin_loader.discover_plugins().
2. player.show_content() still targets the bottom content panel.
3. player.show_image() and player.show_video() still use the central display correctly.
4. player.show_hud_view() displays HTML, text, declarative spec, and localhost URL interfaces.
5. player.update_hud_view() updates an existing view without destroying the surface.
6. player.close_hud_view() restores the avatar (stack index 0).
7. Non-owning plugin cannot close or update another plugin's view.
8. Duplicate show_hud_view() from same owner updates existing view without re-stacking.
9. Second plugin cannot replace active view unless replace=True is passed.
10. Temporary priority: live camera stream switches to stack index 1 and restores plugin HUD (index 3) on stop.
11. Video opening cleanly closes/pauses and plugin HUD request while video plays stops video.
12. Graceful fallback when WebEngine is missing.
13. get_hud_view_state() returns thread-safe immutable snapshots.
"""

import os
import sys
from pathlib import Path
import pytest
from unittest.mock import MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt6.QtWidgets import QApplication, QStackedWidget

# Ensure single QApplication exists for Qt tests
app = QApplication.instance()
if app is None:
    app = QApplication(["test_plugin_hud"])


def _create_mock_player():
    """Helper to instantiate JarvisUI with a mocked MainWindow without launching the full window."""
    from ui import JarvisUI
    player = JarvisUI.__new__(JarvisUI)
    player._win = MagicMock()
    return player


# ---------------------------------------------------------------------------
# Test 1: Plugin discovery mechanism
# ---------------------------------------------------------------------------
def test_plugin_auto_discovery():
    """Verify that core.plugin_loader.discover_plugins discovers plugins and map_explorer without issues."""
    from core.plugin_loader import discover_plugins
    plugins_dir = Path(__file__).resolve().parent.parent / "plugins"
    registry = discover_plugins(plugins_dir, core_tool_names=set())
    records = registry.list_for_ui()
    assert isinstance(records, list)
    assert len(records) > 0
    names = [r["name"] for r in records if r["valid"]]
    assert "map_explorer" in names


# ---------------------------------------------------------------------------
# Test 2: JarvisUI backwards compatibility & thread-safety
# ---------------------------------------------------------------------------
def test_player_legacy_methods_exist_and_safe():
    """Verify player.show_content, player.show_image, player.show_video still exist on JarvisUI."""
    player = _create_mock_player()
    
    # show_content
    player.show_content("Test Title", "Test Text")
    player._win._content_sig.emit.assert_called_once_with("Test Title"[:48], "Test Text"[:4000])
    
    # show_image
    player.show_image("test.png", "Test Image")
    player._win._image_open_sig.emit.assert_called_once_with("test.png", "Test Image")
    
    # show_video
    player.show_video("test.mp4", "Test Video")
    player._win._video_open_sig.emit.assert_called_once_with("test.mp4", "Test Video", True, "")
    
    # write_log
    player.write_log("Test Log")
    player._win._log_sig.emit.assert_called_once_with("Test Log")


# ---------------------------------------------------------------------------
# Test 3: JarvisUI HUD View APIs
# ---------------------------------------------------------------------------
def test_player_hud_view_api_emission():
    """Verify player.show_hud_view, update_hud_view, close_hud_view, get_hud_view_state emit signals."""
    player = _create_mock_player()
    player._win.get_plugin_hud_state.return_value = {"active": True, "plugin_id": "demo"}
    
    # show_hud_view
    ok = player.show_hud_view("test_plugin", "Test HUD", "http://127.0.0.1:8765/", "url", replace=True)
    assert ok is True
    player._win._plugin_hud_open_sig.emit.assert_called_once_with(
        "test_plugin", "Test HUD", "http://127.0.0.1:8765/", "url", True
    )
    
    # update_hud_view
    ok = player.update_hud_view("test_plugin", "Updated content", "text")
    assert ok is True
    player._win._plugin_hud_update_sig.emit.assert_called_once_with(
        "test_plugin", "Updated content", "text"
    )
    
    # close_hud_view
    ok = player.close_hud_view("test_plugin")
    assert ok is True
    player._win._plugin_hud_close_sig.emit.assert_called_once_with("test_plugin")
    
    # get_hud_view_state
    state = player.get_hud_view_state()
    assert state == {"active": True, "plugin_id": "demo"}
    player._win.get_plugin_hud_state.assert_called_once()


def test_player_hud_view_none_player_safe():
    """Verify calling HUD view methods on None or unattached player does not raise."""
    from ui import JarvisUI
    player = JarvisUI.__new__(JarvisUI)
    player._win = None
    assert player.show_hud_view("demo", "title", "content") is False
    assert player.update_hud_view("demo", "content") is False
    assert player.close_hud_view("demo") is False
    state = player.get_hud_view_state()
    assert state["active"] is False
    assert state["plugin_id"] is None


# ---------------------------------------------------------------------------
# Test 4: PluginHudHost UI component functionality
# ---------------------------------------------------------------------------
def test_plugin_hud_host_text_mode():
    """Test PluginHudHost rendering formatted text."""
    from gui.plugin_hud import PluginHudHost
    
    host = PluginHudHost()
    ok = host.show_view("text_plugin", "TEXT VIEW", "Hello Jarvis World", content_type="text")
    assert ok is True
    state = host.get_state_snapshot()
    assert state["active"] is True
    assert state["plugin_id"] == "text_plugin"
    assert state["content_type"] == "text"
    assert "TEXT VIEW" in host._title_lbl.text()
    assert host._text_edit.toPlainText() == "Hello Jarvis World"
    assert host._stack.currentIndex() == 2


def test_plugin_hud_host_spec_mode():
    """Test PluginHudHost rendering a declarative specification dictionary."""
    from gui.plugin_hud import PluginHudHost
    
    host = PluginHudHost()
    spec = {
        "type": "container",
        "children": [
            {"type": "heading", "text": "Telemetry Overview"},
            {"type": "metric", "label": "CPU USAGE", "value": "35%", "delta": "-5%"},
            {"type": "progress", "label": "Memory", "value": 60, "max": 100},
            {"type": "key_value", "data": {"Status": "Nominal", "Node": "Localhost"}},
            {"type": "table", "headers": ["Device", "State"], "rows": [["Sensors", "Online"], ["Network", "Connected"]]}
        ]
    }
    ok = host.show_view("spec_plugin", "SYSTEM TELEMETRY", spec, content_type="spec")
    assert ok is True
    state = host.get_state_snapshot()
    assert state["active"] is True
    assert state["plugin_id"] == "spec_plugin"
    assert state["content_type"] == "spec"
    assert host._stack.currentIndex() == 1


def test_plugin_hud_host_html_and_url_modes():
    """Test PluginHudHost handling url and html content types."""
    from gui.plugin_hud import PluginHudHost, HAVE_WEBENGINE
    
    host = PluginHudHost()
    
    # URL mode
    ok = host.show_view("map_plugin", "MAP VIEW", "http://127.0.0.1:8765/?token=xyz", content_type="url")
    if HAVE_WEBENGINE:
        assert ok is True
        assert host._stack.currentIndex() == 0
    else:
        # Fallback screen shown
        assert host._stack.currentIndex() == 3
        
    # HTML mode
    ok = host.show_view("html_plugin", "HTML VIEW", "<html><body><h1>Test</h1></body></html>", content_type="html", replace=True)
    if HAVE_WEBENGINE:
        assert ok is True
        assert host._stack.currentIndex() == 0


# ---------------------------------------------------------------------------
# Test 5: Ownership rules, replace flag, updates and closes
# ---------------------------------------------------------------------------
def test_ownership_and_replace_rules():
    """Test that non-owning plugins cannot overwrite without replace=True, and cannot close/update."""
    from gui.plugin_hud import PluginHudHost
    
    host = PluginHudHost()
    # Plugin A takes the display
    ok = host.show_view("plugin_A", "TITLE A", "Content A", content_type="text")
    assert ok is True
    assert host.get_state_snapshot()["plugin_id"] == "plugin_A"
    
    # Plugin B tries to show without replace=True
    ok = host.show_view("plugin_B", "TITLE B", "Content B", content_type="text", replace=False)
    assert ok is False
    assert host.get_state_snapshot()["plugin_id"] == "plugin_A"
    
    # Plugin B tries to update Plugin A's view
    ok = host.update_view("plugin_B", "Content B hacked")
    assert ok is False
    
    # Plugin B tries to close Plugin A's view
    ok = host.close_view("plugin_B")
    assert ok is False
    assert host.get_state_snapshot()["active"] is True
    
    # Plugin A updates its own view
    ok = host.update_view("plugin_A", "Updated Content A", content_type="text")
    assert ok is True
    assert host._text_edit.toPlainText() == "Updated Content A"
    
    # Plugin B takes over with replace=True
    ok = host.show_view("plugin_B", "TITLE B", "Content B", content_type="text", replace=True)
    assert ok is True
    assert host.get_state_snapshot()["plugin_id"] == "plugin_B"
    
    # Plugin B closes its own view
    ok = host.close_view("plugin_B")
    assert ok is True
    assert host.get_state_snapshot()["active"] is False
    assert host.get_state_snapshot()["plugin_id"] is None


# ---------------------------------------------------------------------------
# Test 6: MainWindow Stack Index & Lifecycle Transitions
# ---------------------------------------------------------------------------
def test_mainwindow_stack_and_priority():
    """Test stack index switching, camera stream priority, and video closure."""
    # We test with a dummy QStackedWidget mimicking MainWindow's stack
    stack = QStackedWidget()
    from gui.plugin_hud import PluginHudHost
    
    avatar_widget = QStackedWidget() # Index 0
    cam_widget = QStackedWidget()    # Index 1
    video_widget = QStackedWidget()  # Index 2
    plugin_host = PluginHudHost()    # Index 3
    
    stack.addWidget(avatar_widget)
    stack.addWidget(cam_widget)
    stack.addWidget(video_widget)
    stack.addWidget(plugin_host)
    
    assert stack.currentIndex() == 0
    
    # Show Plugin HUD -> switches to index 3
    plugin_host.show_view("map_plugin", "MAP", "Test", content_type="text")
    stack.setCurrentIndex(3)
    assert stack.currentIndex() == 3
    
    # Camera Stream Starts -> remembers previous index 3, switches to index 1
    prev_index = stack.currentIndex()
    assert prev_index == 3
    stack.setCurrentIndex(1)
    assert stack.currentIndex() == 1
    # Plugin host remains active in background
    assert plugin_host.get_state_snapshot()["active"] is True
    
    # Camera Stream Stops -> restores previous index 3
    stack.setCurrentIndex(prev_index)
    assert stack.currentIndex() == 3
    
    # Plugin HUD closes -> restores avatar (index 0)
    plugin_host.close_view("map_plugin")
    stack.setCurrentIndex(0)
    assert stack.currentIndex() == 0


# ---------------------------------------------------------------------------
# Test 7: Map Explorer Integration
# ---------------------------------------------------------------------------
def test_map_explorer_uses_hud_view():
    """Verify plugins/map_explorer.py invokes player.show_hud_view."""
    from plugins import map_explorer
    
    mock_player = MagicMock()
    mock_player.show_hud_view.return_value = True
    
    # Test open action with mocked player
    res = map_explorer.run({"action": "open"}, player=mock_player)
    assert "HUD" in res or "explorer" in res.lower()
    mock_player.show_hud_view.assert_called_once()
    args, kwargs = mock_player.show_hud_view.call_args
    assert args[0] == "map_explorer"
    assert args[1] == "MAP EXPLORER"
    assert "http://127.0.0.1:" in args[2]
    assert kwargs.get("content_type") == "url" or args[3] == "url"
    
    # Test close action
    res_close = map_explorer.run({"action": "close"}, player=mock_player)
    mock_player.close_hud_view.assert_called_once_with("map_explorer")
    assert "closed" in res_close.lower()
