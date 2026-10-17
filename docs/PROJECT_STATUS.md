# Mark-LV LaCasa Project Status

**Date:** 2026-10-15  
**Repo:** https://github.com/qartex/Mark-LV-LaCasa  
**Base:** Mark-LV by FatihMakes (MIT licensed, personal/non-commercial use)

## Completed

- [x] Repo geklont nach `/root/Mark-LV-LaCasa`
- [x] Headless UI shim (`ui_headless.py`) erstellt
- [x] `main.py` angepasst (kein PyQt6, kein threading/mainloop)
- [x] HA Plugin (`plugins/home_assistant.py`) mit Env-Variable-Fallback
- [x] Cannanas Plugin (`plugins/cannabis_status.py`) Template
- [x] Dependencies installiert (portaudio19-dev, openwakeword, etc.)
- [x] Gemini API Key konfiguriert
- [x] Git Commits auf GitHub gepusht

## In Arbeit / Blocker

- [ ] **HA Token Problem:** Der Token wird von HA mit 401 abgelehnt
  - Token Issuer ID: `34a73b4482ed43618eb8e461499b99f3`
  - Wird auf BEIDEN Endpunkten abgelehnt:
    - `https://homelacasa.qartex.com` (Cloudflare)
    - `http://100.74.152.40:8123` (Tailscale direkt)
  - Mögliche Ursachen:
    1. Token wurde auf einer ANDEREN HA-Instanz erstellt (User hat zwei Accounts!)
    2. Token ist für einen anderen Benutzer
    3. Cloudflare OAuth Protection blockiert alle API-Calls

## Offene Fragen

1. Auf welcher HA-Instanz wurde der Token erstellt?
2. Sind die beiden Accounts im Screenshot auf derselben oder verschiedenen HA-Installationen?
3. Welchen Token braucht das Projekt wirklich?

## Nächste Schritte

Sobald ein gültiger HA Token vorliegt:
1. HA Plugin testen (Temperatursensoren abfragen)
2. Cannanas Integration testen
3. Headless Main.py starten und verifizieren
4. Systemd Service erstellen für Auto-Start
5. Telegram Bot Integration (wenn gewünscht)

## Wichtige Pfade

- Mark-LV Repo: `/root/Mark-LV-LaCasa/`
- Cannanas MCP: `/home/lacasa15/services/cannanas-mcp/`
- HA Tailscale: `100.74.152.40` (lacasa17)
- HA Cloudflare: `https://homelacasa.qartex.com`

## Skills Die Dieses Projekt Betreffen

- `mark-lv` — Mark-LV Deployment mit HA und Cannanas
- `home-assistant-operational-excellence` — HA Management
- `subagent-driven-development` — Plan-execution mit Reviews
- `writing-plans` — Implementation plans
