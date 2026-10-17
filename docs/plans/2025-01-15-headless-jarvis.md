# Mark-LV LaCasa — Headless JARVIS Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.
> **Project Repo:** https://github.com/qartex/Mark-LV-LaCasa
> **Base:** Mark-LV by FatihMakes (MIT licensed, personal/non-commercial use)

**Goal:** Transform Mark-LV from a desktop GUI app into a headless voice assistant running on Ubuntu server (no PyQt6, no display, pure audio + plugins).

**Architecture:**
```
[ESP32/Alexa/Dot] → HTTP/MQTT → [Mark-LV Headless on Ubuntu] → [HA REST API]
                                                         ↓
                                                    [Cannanas MCP]
```

**Tech Stack:** Python 3.11+, sounddevice, openwakeword, Gemini Live API, requests, fastapi (dashboard)

**Current State:**
- ✅ Repo geklont: `/root/Mark-LV-LaCasa`
- ✅ Headless UI shim: `ui_headless.py` (PyQt6-free JarvisUI)
- ✅ Main.py angepasst: imports headless UI, no threading/mainloop
- ⏳ Dependencies installiert (im Hintergrund, pip install läuft)
- ❌ Gemini API Key fehlt (config/api_keys.json nicht erstellt)
- ❌ HA Plugin muss getestet werden
- ❌ Cannanas Plugin muss getestet werden
- ❌ Telegram Bot Integration fehlt
- ❌ ESP32/Dot Audio-Anbindung fehlt

---

## Task 1: Dependencies installieren und Umgebungscheck

**Objective:** Alle Python-Dependencies installieren und testen ob der headless Start funktioniert.

**Files:**
- Modify: `/root/Mark-LV-LaCasa/requirements.txt` (check)
- Create: `/root/Mark-LV-LaCasa/config/api_keys.json` (placeholder)
- Test: `python3 -c "from ui_headless import JarvisUI; ui = JarvisUI(); print('OK')"`

**Step 1: Install dependencies**
```bash
cd /root/Mark-LV-LaCasa
pip3 install -r requirements.txt
```

**Step 2: Create placeholder config**
```json
{
  "gemini_api_key": "",
  "assistant_name": "JARVIS",
  "user_name": "",
  "plugin_config": {
    "home_assistant": {
      "ha_url": "http://192.168.1.88:8123",
      "ha_token": ""
    },
    "cannabis_status": {
      "mcp_path": "/home/lacasa15/services/cannanas-mcp/"
    }
  }
}
```

**Step 3: Verify headless UI imports**
```bash
python3 -c "from ui_headless import JarvisUI; ui = JarvisUI(); print(f'Headless OK, ready={ui._ready}')"
```
Expected: `[JARVIS] Headless UI initialised — assistant ready.` + `Headless OK, ready=False`

**Step 4: Check audio devices available**
```bash
python3 -c "import sounddevice as sd; print('Input devices:'); [print(f'  {i}: {d}') for i, d in enumerate(sd.query_devices()) if d['max_input_channels'] > 0]"
```

**Commit:**
```bash
git add -A
git commit -m "chore: add placeholder config and verify headless startup"
git push origin main
```

---

## Task 2: Gemini API Key und HA Token konfigurieren

**Objective:** API Keys aus sicherer Quelle laden und Test-Connection prüfen.

**Files:**
- Read: `/root/Mark-LV-LaCasa/config/api_keys.json`
- Read: `/root/.hermes/vault/` (für gespeicherte Tokens)

**Step 1: Prüfe ob Gemini API Key vorhanden ist**
```bash
cat /root/.hermes/vault/* 2>/dev/null | grep -iE "gemini|google|api.*key" | head -5
```

**Step 2: Prüfe ob HA Long-Lived Token vorhanden ist**
```bash
cat /tmp/ha_tokens.json 2>/dev/null || grep -r "ha_token\|long_lived" ~/.hermes/ 2>/dev/null | head -10
```

**Step 3: Falls nicht vorhanden — Benutzer fragen**
- Gemini API Key: https://aistudio.google.com/app/apikey
- HA Long-Lived Token: HA → Profil → Long-Lived Access Token

**Step 4: Config aktualisieren**
```bash
# Wenn Keys im Vault gefunden:
python3 -c "
import json
from pathlib import Path
config = Path('/root/Mark-LV-LaCasa/config/api_keys.json').read_text()
data = json.loads(config)
# Update mit tatsächlichen Keys
data['gemini_api_key'] = '<FROM_VAULT>'
data['plugin_config']['home_assistant']['ha_token'] = '<FROM_VAULT>'
Path('/root/Mark-LV-LaCasa/config/api_keys.json').write_text(json.dumps(data, indent=4))
print('Config updated')
"
```

**Step 5: Connection-Test**
```bash
curl -s -H "Authorization: Bearer <HA_TOKEN>" http://192.168.1.88:8123/api/ | python3 -m json.tool | head -20
```
Expected: `{"message": "Welcome to Home Assistant", "version": "..."}`

**Commit:**
```bash
git add config/api_keys.json
git commit -m "feat: configure Gemini API key and HA token"
git push origin main
```

---

## Task 3: HA Plugin testen und feintunen

**Objective:** Das Home Assistant Plugin funktioniert und kann Geräte abfragen/steuern.

**Files:**
- Read: `/root/Mark-LV-LaCasa/plugins/home_assistant.py`
- Test: Direkter Aufruf der Plugin-Funktionen

**Step 1: Plugin-Imports prüfen**
```bash
cd /root/Mark-LV-LaCasa
python3 -c "
from plugins.home_assistant import get_states, get_state, call_service, PLUGIN
print(f'Plugin loaded: {PLUGIN[\"name\"]}')
print(f'Description: {PLUGIN[\"description\"][:100]}...')
"
```

**Step 2: HA Entity-List abrufen**
```bash
python3 -c "
import sys
sys.path.insert(0, '.')
from plugins.home_assistant import get_states
states = get_states()
print(f'Total entities: {len(states)}')
for s in states[:10]:
    print(f'  {s[\"entity_id\"]}: {s[\"state\"]}')
"
```

**Step 3: Temperatursensoren abfragen**
```bash
python3 -c "
import sys
sys.path.insert(0, '.')
from plugins.home_assistant import get_states
temps = [s for s in get_states() if 'temp' in s['entity_id'].lower()]
for t in temps:
    print(f'{t[\"entity_id\"]}: {t[\"state\"]}°C')
"
```

**Step 4: Lights an/aus testen**
```bash
python3 -c "
import sys
sys.path.insert(0, '.')
from plugins.home_assistant import call_service
# Nur testen, kein reales Device ändern!
result = call_service('light', 'turn_on', 'light.test_entity')
print(result)
"
```
Expected: 404 oder Error — das ist okay, wir testen nur die Verbindung.

**Step 5: Plugin-Beschreibung verbessern (falls nötig)**
Wenn die deutsche Beschreibung nicht gut vom Gemini-Modell verstanden wird, englische Version testen.

**Commit:**
```bash
git add plugins/home_assistant.py
git commit -m "test: verify HA plugin connectivity"
git push origin main
```

---

## Task 4: Cannanas Plugin testen und feintunen

**Objective:** Das Cannanas Plugin kann Pflanzen-Daten abrufen.

**Files:**
- Read: `/root/Mark-LV-LaCasa/plugins/cannabis_status.py`
- Check: Cannanas MCP Server erreichbar

**Step 1: Cannanas MCP Server Status prüfen**
```bash
systemctl status cannanas-mcp 2>/dev/null || systemctl status cannanas 2>/dev/null
ps aux | grep cannanas | grep -v grep
```

**Step 2: MCP Tools direkt testen**
```bash
cd /home/lacasa15/services/cannanas-mcp
source venv/bin/activate
python3 -c "
import subprocess, json
result = subprocess.run(
    ['python3', '-m', 'cannanas_mcp'],
    input='test',
    capture_output=True,
    text=True,
    timeout=5
)
print('Process running:', result.returncode)
" 2>/dev/null || echo "MCP Server läuft (STDIO mode, kann nicht direkt getestet werden)"
```

**Step 3: Plugin-Structure prüfen**
```bash
cd /root/Mark-LV-LaCasa
python3 -c "
from plugins.cannabis_status import PLUGIN, _run_cannabis_cli
print(f'Plugin: {PLUGIN[\"name\"]}')
print(f'Tools: find_plants, list_strains, get_overview')
"
```

**Step 4: Cannanas-MCP Pfad validieren**
```bash
ls -la /home/lacasa15/services/cannanas-mcp/cannanas_mcp.py
test -f /home/lacasa15/services/cannanas-mcp/data/history.json && echo "Cannanas data exists"
```

**Step 5: Plugin-Logik anpassen**
Da Cannanas über MCP/STDIO läuft und nicht als CLI:
- Option A: Plugin ruft Cannanas REST API auf (falls vorhanden)
- Option B: Plugin nutzt Hermes MCP-Tools via subprocess
- Option C: Plugin fragt HA Entities ab (wenn Cannanas in HA integriert)

**Empfohlener Ansatz:** Prüfe ob Cannanas HTTP-API vorhanden ist:
```bash
curl -s http://localhost:8000/api/ 2>/dev/null || echo "No REST API"
```

**Commit:**
```bash
git add plugins/cannabis_status.py
git commit -m "feat: adapt cannabis_status plugin for MCP integration"
git push origin main
```

---

## Task 5: Audio-Setup und STT/TTS testen

**Objective:** Mikrofon und Lautsprecher funktionieren, Gemini Live API Stream funktioniert.

**Files:**
- Check: Audio-Geräte
- Test: Minimaler Gemini Live Stream (ohne GUI)

**Step 1: Audio-Geräte auflisten**
```bash
python3 -c "
import sounddevice as sd
print('=== INPUT DEVICES ===')
for i, d in enumerate(sd.query_devices()):
    if d['max_input_channels'] > 0:
        print(f'{i}: {d[\"name\"]} ({d[\"max_input_channels\"]}ch)')
print()
print('=== OUTPUT DEVICES ===')
for i, d in enumerate(sd.query_devices()):
    if d['max_output_channels'] > 0:
        print(f'{i}: {d[\"name\"]} ({d[\"max_output_channels\"]}ch)')
"
```

**Step 2: Wake Word Engine installieren (optional)**
```bash
# openwakeword für "Hey Jarvis"
pip3 install openwakeword
# Test
python3 -c "
try:
    import openwakeword
    print('openwakeword OK')
except ImportError:
    print('openwakeword not installed — use --with-wake-word flag')
"
```

**Step 3: Gemini Live API Connection testen**
```bash
python3 -c "
import sys
sys.path.insert(0, '.')
from memory.config_manager import load_api_keys, get_gemini_key
from google import genai
key = get_gemini_key()
if not key or len(key) < 10:
    print('ERROR: No valid Gemini API key')
    sys.exit(1)
client = genai.Client(api_key=key)
print(f'Gemini connected, model: {client.models.list()[0].name}')
"
```

**Step 4: Headless Main.py Syntax-Check**
```bash
cd /root/Mark-LV-LaCasa
python3 -m py_compile main.py && echo "main.py syntax OK"
python3 -m py_compile ui_headless.py && echo "ui_headless.py syntax OK"
```

**Commit:**
```bash
git add -A
git commit -m "feat: add audio device detection and Gemini connection test"
git push origin main
```

---

## Task 6: Telegram Bot Integration

**Objective:** Status-Updates an Telegram-Kanal senden.

**Files:**
- Create: `/root/Mark-LV-LaCasa/plugins/telegram_bot.py`
- Modify: `config/api_keys.json` (Telegram config)

**Step 1: Telegram Bot erstellen**
- Zu @BotFather auf Telegram gehen
- Bot-Namen wählen (z.B. "LaCasa Jarvis")
- Token erhalten

**Step 2: Plugin erstellen**
```python
# plugins/telegram_bot.py
PLUGIN = {
    "name": "telegram_bot",
    "description": "Sende Status-Updates an den LaCasa Telegram Kanal.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "message": {"type": "STRING", "description": "Nachricht zum Senden"},
            "channel": {"type": "STRING", "description": "Telegram Chat ID oder @channelname"}
        },
        "required": ["message"]
    }
}

import os
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")

def run(parameters: dict, player=None, session_memory=None) -> str:
    message = parameters.get("message", "")
    channel = parameters.get("channel", TELEGRAM_CHANNEL)
    
    if not TELEGRAM_BOT_TOKEN:
        return "Telegram Bot nicht konfiguriert"
    if not channel:
        return "Keine Telegram Channel-ID"
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    result = requests.post(url, json={"chat_id": channel, "text": message}, timeout=10)
    
    if result.status_code == 200 and result.json().get("ok"):
        return f"✅ Nachricht gesendet an {channel}"
    return f"❌ Senden fehlgeschlagen: {result.text}"
```

**Step 3: Config aktualisieren**
```json
{
  "telegram_bot_token": "<TOKEN_FROM_BOTFATHER>",
  "telegram_channel": "<CHANNEL_ID_OR_USERNAME>"
}
```

**Step 4: Test**
```bash
python3 -c "
import sys
sys.path.insert(0, '.')
from plugins.telegram_bot import run
result = run({'message': 'Test from JARVIS', 'channel': '@test_channel'})
print(result)
"
```

**Commit:**
```bash
git add plugins/telegram_bot.py config/api_keys.json
git commit -m "feat: add telegram bot plugin for status updates"
git push origin main
```

---

## Task 7: Service-Datei und Auto-Start

**Objective:** JARVIS startet automatisch beim Systemboot und läuft als Daemon.

**Files:**
- Create: `/etc/systemd/system/jarvis.service`
- Create: `/root/Mark-LV-LaCasa/scripts/start-jarvis.sh`

**Step 1: Start-Skript erstellen**
```bash
#!/bin/bash
# /root/Mark-LV-LaCasa/scripts/start-jarvis.sh
cd /root/Mark-LV-LaCasa
export PYTHONUNBUFFERED=1
exec python3 main.py >> /var/log/jarvis.log 2>&1
```

**Step 2: Systemd Service**
```ini
[Unit]
Description=Mark-LV Headless JARVIS Assistant
After=network.target home-assistant.service
Wants=home-assistant.service

[Service]
Type=simple
User=lacasa15
Group=lacasa15
WorkingDirectory=/root/Mark-LV-LaCasa
ExecStart=/usr/bin/python3 /root/Mark-LV-LaCasa/main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**Step 3: Installieren**
```bash
sudo cp scripts/start-jarvis.sh /usr/local/bin/jarvis-start.sh
sudo cp jarvis.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable jarvis
sudo systemctl start jarvis
sudo systemctl status jarvis
```

**Step 4: Logs prüfen**
```bash
journalctl -u jarvis -f
```

**Commit:**
```bash
git add scripts/ jarvis.service
git commit -m "feat: add systemd service for headless JARVIS daemon"
git push origin main
```

---

## Task 8: Dokumentation und README

**Objective:** Complete setup guide for future reference.

**Files:**
- Modify: `/root/Mark-LV-LaCasa/README.md`
- Create: `/root/Mark-LV-LaCasa/docs/headless-setup.md`

**Step 1: README aktualisieren**
Füge Sektion hinzu:
- Headless deployment
- Prerequisites (Python, Gemini API, HA Token)
- Installation steps
- Plugin configuration
- Troubleshooting

**Step 2: Setup-Dokumentation**
```markdown
# Headless JARVIS Setup

## Prerequisites
- Ubuntu Server (headless)
- Python 3.11+
- Gemini API Key
- Home Assistant Long-Lived Token
- [Optional] Telegram Bot Token

## Installation
1. Clone repo
2. `pip install -r requirements.txt`
3. Configure `config/api_keys.json`
4. `sudo systemctl enable jarvis`
5. `sudo systemctl start jarvis`

## Troubleshooting
- No audio: Check `sounddevice` device list
- HA connection: Verify token and URL
- Cannanas: Check MCP server status
```

**Commit:**
```bash
git add README.md docs/
git commit -m "docs: add headless setup documentation"
git push origin main
```

---

## Summary & Next Steps

**Completed in this session:**
- ✅ Repo geklont
- ✅ Headless UI shim created
- ✅ Main.py adapted (no GUI)
- ✅ HA + Cannanas plugin templates
- ✅ Git commits to GitHub

**Pending (need user input):**
- ❌ Gemini API Key (from aistudio.google.com)
- ❌ HA Long-Lived Token (from HA dashboard)
- ❌ Telegram Bot Token (from @BotFather)
- ❌ ESP32/Dot hardware (future)

**Run the following to proceed:**
```bash
# 1. Get Gemini API Key
# 2. Get HA Long-Lived Token
# 3. Create Telegram Bot
# 4. Run: hermes chat --resume <this-session>
```
