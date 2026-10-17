# Spotify auf lacasa-server (headless)

## Geprüfter Stand

- Zielhost: `lacasa-server`, Tailscale `100.68.76.27`, LAN `192.168.1.180`.
- Home Assistant ist ein anderer Host: `lacasa17`, `100.74.152.40:8123`.
- Installiert: Raspotify 0.48.3 / librespot 0.8.0, aus dem offiziellen amd64-Release. Download gegen den von GitHub gelieferten SHA-256-Digest geprüft.
- Paketdienst `raspotify.service` ist aktiviert und läuft mit der Paketkonfiguration.
- Spotify-Gerätename aktuell: **raspotify (lacasa-server)**.
- ALSA-Karte: `PCH`, ALC1150 Analog, Gerät 0 (analoger Klinkenausgang).
- Der Master-Mixer wurde von stumm auf 60 % / unmuted gestellt. Das ist kein Nachweis für hörbare Wiedergabe und noch keine gesicherte Mixer-Persistenz nach Neustart.
- Einsekündige Stille über `default` sowie `plughw:CARD=PCH,DEV=0` wurde erfolgreich ausgegeben.
- Der lokale Spotify-Connect-Endpunkt meldet den erwarteten Gerätenamen und `statusString: OK`; noch kein Konto gekoppelt.

## Einmalig am iPhone koppeln

1. Spotify Premium verwenden (Voraussetzung von librespot/Raspotify).
2. Das iPhone für die erste automatische Erkennung ins gleiche LAN/WLAN wie den Server bringen. iOS muss Spotify Zugriff auf das lokale Netzwerk erlauben.
3. In Spotify einen Titel öffnen, dann **Mit einem Gerät verbinden**.
4. **raspotify (lacasa-server)** wählen und leise starten.
5. Boxen am analogen Ausgang müssen angeschlossen, eingeschaltet und passend eingestellt sein.

Der JARVIS-Headless-Stack betreibt Spotify ausschließlich über Raspotify/librespot mit dem exakten Gerätenamen `raspotify (lacasa-server)`. Alle Wiedergabe erfolgt direkt über den konfigurierten ALSA-Ausgang; es erfolgt keine Geräteübertragung auf andere Endgeräte.

Das Spotify-Konto ist persönlich und darf nicht für öffentliche oder gewerbliche Musikbeschallung ohne klare Rechte genutzt werden.

Die Konfiguration beschränkt sich auf die Datei `deploy/spotify/raspotify.conf` (nur `LIBRESPOT_NAME` wurde angepasst auf `raspotify (lacasa-server)`); alle weiteren Einstellungen bleiben unverändert.

JARVIS und Spotify laufen parallel über die analogen Lautsprecher des Servers. Während tatsächlicher JARVIS-Ausgabe wird der Musikzweig um 18 dB abgesenkt und nach exakter Wiederherstellung des vorherigen Pegels genau. Kein Track wird pausiert oder neu gestartet.

Nach Authorization via `spotify-auth-start` und erfolgreichem Konsent zeigt `spotify-auth-status` `authorized: true` mit den festen Ablaufdaten (sechs Monate ab erstem Consent). Refresh verlängert diese Frist nicht. `invalid_grant`, Widerruf oder Ablauf der Autorisierung invalidieren Tokens atomar und fordern einen neuen Autorisierungsvorgang.

Die Web API Zielgeräteauswahl ist exakt: nur `raspotify (lacasa-server)` wird gesteuert. Kein Tracking, kein Zwischenspeichern von Token-Werten, keine Weiterleitung an andere Geräte.

Spotify-Konten sind für persönliche/private Nutzung vorgesehen. Für öffentliche oder gewerbliche Musikbeschallung separat die Nutzungsrechte klären.

## Diagnose

```bash
systemctl is-enabled raspotify
systemctl is-active raspotify
journalctl -u raspotify -n 30 --no-pager
aplay -l
amixer -c PCH get Master
ss -ltn
```

Der Connect-Port der Paketkonfiguration wird dynamisch gewählt. Den aktuellen Port aus `ss -ltn` ermitteln und lokal abfragen:

```bash
curl --fail --silent --show-error 'http://127.0.0.1:PORT/?action=getInfo'
```

## Optionale Konfiguration (noch NICHT auf dem Server angewendet)

`deploy/spotify/raspotify.conf` ist eine geheimnisfreie Vorlage für den Namen **LaCasa Boxen**, 30 % Software-Startlautstärke, expliziten Analogausgang und festen Connect-Port. Aktuell läuft weiterhin die Paketkonfiguration. Das Anwenden benötigt administrative Rechte und startet den Dienst neu:

```bash
sudo cp -an /etc/raspotify/conf /etc/raspotify/conf.before-lacasa
sudo install -m 600 -o root -g root deploy/spotify/raspotify.conf /etc/raspotify/conf
sudo systemctl restart raspotify
```

Danach Namen und Status am Connect-Endpunkt erneut prüfen. Die Vorlage nutzt `plughw` exklusiv; gleichzeitige Musikwiedergabe und JARVIS-Audio benötigen einen getesteten gemeinsamen Mixer. Dies ist noch nicht eingerichtet.

## JARVIS ist getrennt zu betrachten

Ein laufender Spotify-Dienst bedeutet nicht, dass JARVIS läuft. Auf diesem Host wurden PortAudio, ALSA-Werkzeuge und eine Python-Umgebung `/home/lacasa/Mark-LV-LaCasa/.venv` eingerichtet. Der JARVIS-Import scheitert noch an der PulseAudio-Initialisierung durch `sounddevice`. Dashboard, Gemini-Live-Gespräch und iPhone-Mikrofon sind noch nicht end-to-end verifiziert. Telegram gehört ausdrücklich nicht zum Projektumfang.
