Mark LVI/
├── main.py                   # Core loop — Gemini Live session, audio I/O, viseme extraction, tool dispatch
├── ui.py                     # PyQt6 HUD — avatar canvas, waveform, log panel, settings drawer, camera feed
├── setup.py                  # OS-aware installer (skips wrong-OS dependencies, checks your Python)
├── .gitignore                # Keeps your API key, TLS key and memories out of the repository
├── providers/                # OPTIONAL — delete it and the assistant runs on Gemini,
│                             #   exactly as it did before this folder existed.
│                             #   Never imported unless a provider is configured.
│   ├── base.py               # The contract: Engine / LLM / STT / TTS, caps, Live-shaped events
│   ├── registry.py           # Discovery and construction — imports one module, on demand
│   ├── schema.py             # Tool declarations: Gemini <-> OpenAI <-> Anthropic, and the budget
│   ├── audio.py              # Rates and formats — 16 kHz in, 24 kHz out, everything between
│   ├── llm_openai.py         # Every /v1/chat/completions endpoint: local servers and hosted APIs
│   ├── stt_whisper.py        # faster-whisper on the CPU, or a transcription API
│   ├── tts_piper.py          # Neural voice here, ~50 languages, follows the one you speak
│   ├── tts_system.py         # Windows SAPI / macOS say / Linux espeak-ng — nothing to download
│   ├── tts_elevenlabs.py     # One voice, every language, streamed
│   ├── engine_pipeline.py    # STT -> LLM -> TTS, wearing the Live session's interface
│   ├── _tests.py             # `python -m providers._tests` — no network, no key needed
│   └── voices/               # Piper voices, downloaded on demand (git-ignored)
├── plugins/                  # OPTIONAL skills — delete any file and it is simply gone
│   │                         #   next launch. This is where anything that is NOT
│   │                         #   about driving the computer lives; see actions/.
│   ├── _template.py          # Copy this to write a new skill — one file, drop in, done
│   │                         #   A file starting with _ is a shared driver, not a skill:
│   │                         #   the loader skips it, and the skills beside it import it.
│   ├── _whatsapp_core.py     # Shared WhatsApp driver — desktop app or Web, all three OSes
│   ├── whatsapp_call.py      # Place and end WhatsApp voice / video calls
│   ├── whatsapp_guard.py     # Answer, decline or auto-reply to INCOMING WhatsApp calls
│   ├── telegram_remote.py    # Drive JARVIS from anywhere over Telegram
│   ├── chat_takeover.py      # Reads an on-screen chat and replies in it
│   ├── screen_agent.py       # Looks at the screen and works it, step by step
│   ├── gmail_control.py      # Read, search, send and draft email
│   ├── calendar_control.py   # Read and create calendar events
│   ├── obsidian_vault.py     # Read and write notes in an Obsidian vault
│   ├── document_review.py    # Review a document and report what should change
│   ├── excel_writer.py       # Spreadsheet output
│   ├── code_helper.py        # Write and run small programs
│   ├── dev_agent.py          # Multi-file project work
│   ├── image_generator.py    # Generate images
│   ├── upload_video.py       # Upload the video on your Desktop to YouTube
│   ├── youtube_video.py      # Search, summarise and fetch transcripts
│   ├── printer_control.py    # 3D printers, over MQTT
│   ├── home_assistant.py     # Smart-home devices
│   ├── game_updater.py       # Update or install games through their launcher
│   ├── flight_finder.py      # Flight search
│   ├── shopping_list.py      # Shopping list
│   ├── pomodoro.py           # Pomodoro timer
│   ├── water_reminder.py     # Hydration reminders with intake tracking
│   ├── quiz.py               # Spoken quizzes
│   ├── spaced_repetition.py  # Flashcard review
│   ├── calorie_counter.py    # Food through the webcam
│   └── pushup_counter.py     # Counts pushups through the webcam
├── actions/                  # Bundled skills — each self-describes via a TOOL dict + handler
│                             #   Everything here drives the COMPUTER, which is what decides
│                             #   whether a new skill belongs in this folder at all.
│   ├── web_search.py         # Gemini + DDG parallel search (news, research, price, compare)
│   ├── screen_processor.py   # Screen & webcam capture for vision
│   ├── background_monitor.py # User-configured topic watching — daily DDG check
│   ├── proactive.py          # Proactive 2.0 — time/context/rotation-aware check-ins
│   ├── reminder.py           # OS-native scheduled notifications
│   ├── system_monitor.py     # CPU / RAM / GPU / temperature telemetry
│   ├── computer_settings.py  # Volume, brightness, WiFi, power (per-OS)
│   ├── computer_control.py   # Keyboard shortcuts, mouse, window management
│   ├── open_app.py           # Application launcher (per-OS name map)
│   ├── browser_control.py    # Web browser control
│   ├── file_controller.py    # File system operations
│   ├── file_processor.py     # Document reading and summarization
│   ├── send_message.py       # Messaging integration
│   ├── weather_report.py     # Live weather data
│   ├── video_player.py       # Plays video on the HUD, where the avatar normally is
│   └── desktop.py            # Desktop and taskbar control
├── memory/
│   ├── memory_manager.py     # Load/save long_term.json — sessions, monitors, identity
│   ├── config_manager.py     # api_keys.json access — key, OS, name, voice, colour, toggles
│   └── long_term.json        # Persistent store — created on first run
├── dashboard/                # Phone control — paired by QR code, one-time key
│   ├── server.py             # FastAPI + uvicorn; HTTP by default, HTTPS opt-in
│   └── static/               # The phone page and its login screen
├── core/
│   ├── outcome.py            # What a tool tells the model: done / unverified / failed /
│   │                         #   ask / started, plus the untrusted-content wrapper
│   ├── appidentity.py        # Which applications are running, which one the user named,
│   │                         #   and which processes are the assistant's own (never a target)
│   ├── lazyimport.py         # Heavy third-party imports paid only if they are used
│   ├── platform_support.py   # What THIS machine permits — Wayland, macOS permissions —
│   │                         #   reported to the user and to the model, never guessed at
│   ├── gemini.py             # One place for every one-shot Gemini call — model ladder, timeouts,
│   │                         #   cooldowns, and the configured provider as one more rung
│   ├── prompt.txt            # All prompt wording — {tokens} are filled from the live system at startup
│   ├── avatar.py             # Avatar renderer — lighting, pose, expression, mouth (QPainter)
│   ├── avatar_mesh.py        # Head geometry — loads the face, generates skull/neck/rigs
│   ├── face_model.obj        # The face itself (MediaPipe canonical model, Apache-2.0, 25 KB)
│   ├── viseme.py             # Transcript → mouth shapes, fused with the audio's timing
│   ├── echo.py               # Tells your voice from the assistant's own echo; self-calibrating
│   ├── hotkey.py             # Push-to-talk chord — global on Windows, windowed fallback elsewhere
│   ├── undo.py               # One shared undo stack — actions register how to reverse themselves
│   ├── confirm.py            # Irreversible-action gate — the token is issued by the UI, not the model
│   ├── audio_devices.py      # Microphone / speaker list — filtered, measured, resolved by name
│   ├── plugin_loader.py      # Plugin engine — discovery, validation, crash isolation
│   ├── action_loader.py      # Bundled-action engine — the built-in twin of plugin_loader
│   └── wake_word.py          # Local "Hey Jarvis" detector — own thread, offline, opt-in
└── config/
    ├── api_keys.json         # API key, name, voice, colour, toggles — created on first launch (git-ignored)
    └── certs/                # Self-signed TLS pair for the phone dashboard — generated locally (git-ignored)# Security Policy

## Supported Versions

Use this section to tell people about which versions of your project are
currently being supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 5.1.x   | :white_check_mark: |
| 5.0.x   | :x:                |
| 4.0.x   | :white_check_mark: |
| < 4.0   | :x:                |

## Reporting a Vulnerability

Use this section to tell people how to report a vulnerability.

Tell them where to go, how often they can expect to get an update on a
reported vulnerability, what to expect if the vulnerability is accepted or
declined, etc.
