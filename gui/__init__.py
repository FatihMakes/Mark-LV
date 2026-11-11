"""The HUD, split out of ui.py in Mark LVII.

ui.py is still the entry point and still exports every name it always did,
so main.py, plugins and anything written against Mark LVI keep working.
"""
import os

# Before any Qt import: Qt's video backend prints signed stream URLs otherwise.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")
