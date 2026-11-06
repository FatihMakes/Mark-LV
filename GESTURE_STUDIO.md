# Gesture Studio — feature branch

This branch adds an opt-in webcam hand-control engine and a voice-callable plugin.

## Install the optional hand-tracking dependency

From the repository folder in PowerShell:

\`\`\`powershell
python -m pip install mediapipe
\`\`\`

If MediaPipe does not have a compatible wheel for your Python version, use a supported 64-bit Python version listed by MediaPipe, then reinstall the project requirements.

## Start it

Launch MARK LVI, then say or type:

- "Start gesture control"
- "Stop gesture control"
- "Check gesture control status"

The camera starts only after the explicit start command. A GESTURE log entry reports state.

## Current gesture map

- Point with index finger: move the mouse pointer.
- Pinch thumb + index: hold mouse button; move to drag; release pinch to drop.
- Raise index + middle fingers: move hand up/down to scroll.
- Open palm: toggle media play/pause.
- Thumbs up/down (other fingers folded): volume up/down.
- Horizontal swipe: previous/next browser tab.

## Safety and limits

- The camera is not started automatically.
- No gesture deletes files or confirms shutdown/restart.
- Pinch-and-drag acts on the currently focused desktop window; use it carefully.
- Webcam and lighting quality affect tracking.
- This is an initial control engine, not a claim of tested hardware compatibility. Verify camera index and gestures on the target PC.
- The app's main Qt window dashboard controls are a follow-up integration step; this plugin is already discoverable by MARK LVI's plugin loader.
