# Abnahme-Checkliste — LaCasa-JARVIS (Montag)

> Alle Automatisierung ist fertig und deployed. Diese Liste führt durch,
> was **nur physisch vor Ort** getestet werden kann. Jeder Punkt hat ein
> Kommando und ein erwartbares Ergebnis.

## 0. Vorbereitung in lacasa (≈3 Minuten)

```sh
# am Server (SSH oder Termius von unterwegs, oder direkt am Server)
ssh lacasa@192.168.1.180   # oder über Tailscale
systemctl --user status jarvis raspotify --no-pager | head -6
# Erwartet: beides "active"; JARVIS meldet "JARVIS online."
```

## 1. Spotify-Empfänger einmalig koppeln (≈2 Minuten)

1. iPhone ins **gleiche WLAN** wie der Server.
2. Spotify-App → 📱 (Geräte) → **„raspotify (lacasa-server)"** antippen.
3. In der App kurz einen Titel starten (bestätigt die Kopplung).

```sh
# Verifikation aus dem JARVIS-Konto (auf dem Server):
python3 - <<'PY'
from plugins.spotify_control import run
print(run({'action': 'current'}))
PY
# Erwartet: kein „nicht erreichbar"-Fehler mehr — Wiedergabe ist sichtbar.
```

## 2. Sprachtest Spotify + Ducking (≈5 Minuten)

Am iPhone Safari → `https://lacasa-server.tail7a9c2.ts.net` (Microphonzugriff erlauben):

| Sprachbefehl | Erwartet |
|---|---|
| „Jarvis, spiele Bohemian Rhapsody." | Lied läuft aus den Serverboxen |
| „Jarvis, wie ist die Temperatur in [Raum]?" | Antwort kommt, Spotify leiser (−18 dB), danach gleiche Lautstärke wie vor der Frage |
| „Jarvis, Lautstärke 20 Prozent" | Spotify folgt |
| „Jarvis, Pause." / „Weiter" | Wiedergabe steuert |
| „Jarvis, was läuft gerade?" | aktueller Titel; nur wenn Empfänger = LaCasa |
| **STOP** während Antwort | bricht ab; Mikrofon erst nach Ausklang wieder offen |

Hinweis: Spotify sollte beim Test laufen — „Was läuft gerade" wird bewusst
verweigert, wenn ein anderes Gerät aktiv ist.

## 3. iPhone-Dauerzugänge mit den QR-Codes (≈2 Minuten pro Gerät)

Auf dem Server die QR-Anzeige im SSH-Terminal starten (privater Kanal):
```sh
cd ~/Mark-LV-LaCasa
.venv/bin/python scripts/show_device_qr.py --label 'LaCasa iPhone 1'
```
Mit dem iPhone den QR scannen → Safari öffnet `/login#device=…`, das Fragment
verschwindet sofort; JARVIS-Seite öffnet sich. Ein Stunde Laterne — beim
nächsten Öffnen meldet sich das Gerät automatisch wieder an.

```sh
.venv/bin/python -m core.headless device-list --socket /run/user/1000/jarvis/control.sock
# Erwartet: 4 aktive Geräte mit Label LaCasa iPhone 1–4; keine Geheimnisse in der Ausgabe
```

Widerrufstest (ausdrücklich, optional): ein Gerät `device-revoke DEVICE_ID` →
dessen offener Tab fällt beim nächsten Request auf die Login-Seite zurück.
Ersatz: `device-create` unter gleichem Label neu erzeugen (siehe Quickstart).

## 4. Cannanas-Sprachbefehle (≈5 Minuten)

Voraussetzung: `/reload-mcp` im Hermes-Gateway (der SSH-Bus zur Adapter-Datei
wurde umgeschaltet) UND JARVIS-Dienst gestartet mit Credential-Datei (erledigt).

| Sprachbefehl | Erwartet |
|---|---|
| „Jarvis, wie viele Pflanzen stehen in [Raum]?" | Anzahl (oder „mindestens N", wenn nicht abschließend geprüft) |
| „Jarvis, zeig mir Brain Damage Nummer 3." | Pflanzdaten |
| „Jarvis, welche Batches wurden zuletzt angelegt?" | begrenzte Liste + Verweis „weitere auf Anfrage" |
| „Jarvis, welche Mutterpflanzen oder Stecklinge sind verfügbar?" | Vorschau aus dem Inventar |
| Schreibvorgang (z. B. Diary-Eintrag) | Erst die **Vorschau**, nur „Ja"/„Nein"-Frage bestätigt; STOP bricht ohne Schreiben ab |

## 5. Neustart-/Widerrufsnachweise (freiwillig, je ≈1 Minute)

```sh
systemctl --user restart jarvis; sleep 6; systemctl --user is-active jarvis
# Erwartet: active; offener iPhone-Tab meldet sich automatisch wieder an.
# Cannanas-Ausstehende Vorschau ist dabei fällig → frische Vorschau nötig.
```

## 6. Abschluss

- Ergebnispunkte notieren; alles Grüne in `docs/server-voice-quickstart.md`
  und `docs/spotify-server.md` abgleichen.
- Nach der Abnahme: **Cannanas-API-Schlüssel tauschen** (alter stand einmal
  in einem Chat): neuen Schlüssel erzeugen, mit mir austauschen, ich lege
  ihn Server-seitig in die 0600-Datei und erzeugen danach einen
  Bestätigungslauf mit `GET /v1/auth/test`.