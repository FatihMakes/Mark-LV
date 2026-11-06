# ULTRON gesture mouse

The gesture mouse is an opt-in, local webcam feature provided by
`plugins/gesture_mouse.py`.

## Install

From the project folder, run:

```powershell
python -m pip install -r requirements.txt
```

On first use, the plugin downloads the MediaPipe Hand Landmarker model into
`models/hand_landmarker.task`. The model is used locally.

## Start and stop

Start JARVIS and say **"start gesture mouse"**. The webcam opens only after
that explicit request. Say **"stop gesture mouse"** to release the camera and
stop pointer control. You can also ask whether the gesture mouse is running.

## Current gestures

- **Open palm:** move the pointer with your index fingertip.
- **Thumb-index pinch:** press and hold the left mouse button; release the
  pinch to release the button. This supports clicking and dragging.
- **Closed fist:** pause pointer movement and release any held mouse button.

Keep your hand visible to the camera. Camera frames are processed locally and
are not sent to Gemini. The feature needs a working webcam, MediaPipe Tasks,
OpenCV, and PyAutoGUI. If the camera cannot open or the model cannot download,
the plugin reports the error in the activity log.

## Safety

The pointer is controlled only while the feature is running. Use the spoken
stop command to end it. PyAutoGUI's corner failsafe remains enabled; moving the
pointer to the top-left screen corner also triggers its failsafe.
