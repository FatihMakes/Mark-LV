# LaCasa JARVIS: iPhone und Server-Lautsprecher

## Zugang und Bedienung

JARVIS läuft ohne Desktop auf **lacasa-server**, getrennt von Home Assistant und Cannanas. Das iPhone dient als Mikrofon und Fernbedienung, die Antwort kommt zunächst aus den **am Server angeschlossenen Lautsprechern**, nicht aus dem iPhone.

1. Auf dem iPhone **Tailscale verbinden**.
2. Einen aktuellen privaten Kopplungslink in **Safari** öffnen. Die zugehörige Adresse lautet `https://lacasa-server.tail7a9c2.ts.net`. Keine Zertifikatswarnung akzeptieren: korrekt eingerichtet ist HTTPS ohne Warnung gültig.
3. Mikrofontaste antippen und Safari den Mikrofonzugriff erlauben. Einen kurzen Befehl sprechen, zum Beispiel **„Wie ist die Temperatur?“**. Die Mikrofontaste kann die Aufnahme auch ausdrücklich beenden.
4. Während JARVIS antwortet, wird das Mikrofon geschlossen. Die **STOP**-Taste bricht die Antwort ab. Erst nach dem hörbaren Ausklang ist eine neue Aufnahme möglich; dafür erneut die Mikrofontaste antippen.
5. Ohne Mikrofonfreigabe kann zum Testen zunächst ein **Textbefehl** über dieselbe Seite gesendet werden.

Es gibt in diesem Modus kein dauerhaftes Mithören und kein notwendiges lokales Servermikrofon. „Hey Jarvis“ ist kein nötiger Aktivierungsschritt. Das iPhone nicht unmittelbar vor die Serverlautsprecher legen.

## Dauerhafte iPhone-Zugänge

Für die vier iPhones gibt es vier getrennte, dauerhaft gespeicherte Gerätezugänge. Der QR-Code öffnet eine private HTTPS-Adresse, deren Zugangsdaten ausschließlich im URL-Fragment stehen. Safari entfernt das Fragment vor dem ersten Netzwerkzugriff aus der sichtbaren URL und bewahrt den Gerätezugang lokal auf. Der Server tauscht ihn gegen einen frischen Bearer und Sitzungsschlüssel mit fester Laufzeit von **einer Stunde**. Nach Ablauf oder Dienstneustart meldet sich das iPhone mit seinem bestehenden Gerätezugang erneut an; ein einzelner Widerruf betrifft nur dieses Gerät.

Die Erstbereitstellung erfolgt als Benutzer `lacasa` auf **lacasa-server** über den privaten Unix-Socket:

```sh
cd /home/lacasa/Mark-LV-LaCasa
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 1' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 2' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 3' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 4' --socket /run/user/1000/jarvis/control.sock
```

Die Befehle geben nur sichere Metadaten und die Pfade zu den privaten `.png`- und `.url`-Artefakten aus. **Die Artefakte enthalten Zugangsdaten:** nur lokal aufbewahren, weder in Git noch in Chat, Logs oder Dokumentation kopieren. Der Credential-Store liegt in `config/device_credentials.json` (Modus `0600`), die Artefakte in `config/device_qr/` (Verzeichnis `0700`, Dateien `0600`).

Geräte lassen sich ausschließlich lokal verwalten:

```sh
.venv/bin/python -m core.headless device-list --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-revoke DEVICE_ID --socket /run/user/1000/jarvis/control.sock
```

Bei Verlust zuerst die sichere Geräte-ID mit `device-revoke` widerrufen und danach mit `device-create` unter demselben Label einen neuen Zugang erzeugen. Es gibt keine Geräteverwaltung über HTTP oder die Telefonoberfläche.

Für Notfälle bleibt ein separater, **zehn Minuten gültiger** temporärer Kopplungslink verfügbar:

```sh
.venv/bin/python -m core.headless pair --socket /run/user/1000/jarvis/control.sock
```

Dieser Operatorbefehl darf den temporären Link ausgeben. Er rotiert nur frühere temporäre Kopplungen und lässt alle dauerhaften Gerätezugänge und deren Sitzungen unberührt.

Nicht `uvicorn dashboard.server:app` starten: Es gibt dort keine solche Modulvariable, und ein isoliertes Dashboard würde weder Gemini noch Audio betreiben. Keine GUI-Schaltfläche, Chrome-Flags oder selbstsignierten Zertifikate nötig.

## Spotify

In Spotify unter **„Mit einem Gerät verbinden“ → „raspotify (lacasa-server)“** wählen. Der Empfänger benötigt Spotify Premium. Die erste Kopplung erfolgt üblicherweise im gleichen LAN/WLAN, mit iOS-Zugriff auf das lokale Netzwerk. Tailscale allein transportiert keine mDNS-Gerätesuche.

Der Empfänger ist inzwischen am tatsächlichen Zielserver mit einem Konto gekoppelt; reale Titel wurden geladen. Die Musik kommt über den analogen Serverausgang. Die Lautstärke lässt sich weiter in Spotify einstellen. Ein Konto- oder Musikpasswort gehört weder in den Chat noch in die Git-Konfiguration.

### Gemeinsamer Audioweg

```text
Spotify/librespot -> lacasa_spotify -> Monitor-Loopback -> analoger Ausgang
JARVIS ------------------------------------------------> analoger Ausgang
```

Der analoge Pulse-Ausgang heißt auf diesem Rechner:

```text
alsa_output.pci-0000_00_1f.3.analog-stereo
```

Die unabhängige Spotify-Sink-Lautstärke ist ausschließlich die vorübergehende Ducking-Stufe; der Master und JARVIS werden dabei nicht abgesenkt. Die automatische Integration ist ausgerollt: Bei einer echten Ansage wurden beide Kanalwerte von `65536` auf `32846` (akustisch −18 dB) abgesenkt und danach exakt wiederhergestellt. Auch STOP und der unabhängige Wiederherstellungsdienst nach dem SIGKILL eines separaten Test-Owners wurden auf dem Zielhost geprüft. Normales Ducking pausiert keine Titel und startet keine Dienste neu; der Spotify-Prozess blieb dabei derselbe. Beim abschließenden automatisierten Ansagetest war allerdings kein Spotify-Audiostream aktiv. Der gemeinsame Hörtest mit erneut gestarteter Musik und deinem iPhone bleibt deshalb offen.

## Dienste prüfen

Die aktiven Anwendungen und Audio-Dienste sind **Benutzerdienste von `lacasa`**, nicht der alte systemweite Paketdienst:

```sh
systemctl --user status jarvis raspotify spotify-ducking-guard pipewire pipewire-pulse wireplumber --no-pager
journalctl --user -u jarvis -n 40 --no-pager
journalctl --user -u raspotify -n 20 --no-pager
pactl list short sinks
tailscale serve status
```

`systemctl is-active raspotify` ohne `--user` zeigt für den alten deaktivierten Paketdienst bewusst `inactive`. Dies ist kein Nachweis, dass der neue Empfänger gestoppt wäre. Keine zweite Raspotify-Instanz parallel starten.

`loginctl show-user lacasa -p Linger` sollte `Linger=yes` melden. Die Benutzersitzung benötigt dadurch keine offene SSH-Verbindung oder einen Desktop. Der richtige systemd-Start führt `.venv/bin/python main.py` im Deployment-Verzeichnis aus. Vorhandene Keys werden aus der lokalen ignorierten Konfiguration gelesen; Dateimodus 0600 beibehalten.

### Startfehler und lokale Reparatur

Die aktualisierte Unit verhindert automatische Neustarts nur bei **Exit 78 (`EX_CONFIG`)**: fehlende/ungültige lokale Gemini-Zugangsdaten, ein beschädigter oder unsicher berechtigter `config/device_credentials.json` oder ein unsicherer QR-Pfad müssen zuerst lokal repariert werden; danach `systemctl --user restart jarvis`. Niemals den Store durch eine leere Datei ersetzen oder Berechtigungsfehler durch Lockerung der Dateimodi umgehen. Ein HTTP-Bindefehler oder fehlgeschlagener Unix-Kontrollkanal beendet den Dienst dagegen mit einem normalen Fehlerstatus; `Restart=on-failure` darf ihn erneut starten. Modellverbindungen beginnen erst, wenn beide lokalen Eingänge bereit sind. Der unabhängige Spotify-Ducking-Guard bleibt davon unberührt. Nach einem Unit-Update ist vor dem nächsten geplanten JARVIS-Start ein `systemctl --user daemon-reload` erforderlich; Spotify oder die gemeinsame Audiositzung dafür nicht neu starten.

### Zugriff prüfen

```sh
curl -s -o /dev/null -w 'HTTPS %{http_code}, TLS-Prüfung %{ssl_verify_result}\n' \
  https://lacasa-server.tail7a9c2.ts.net/
curl -s -o /dev/null -w 'Nicht angemeldeter Status %{http_code}\n' \
  https://lacasa-server.tail7a9c2.ts.net/api/status
```

Erwartet: Seite **200**, TLS-Prüfung **0**, nicht angemeldeter Status **401**. Das Backend bindet nur an `127.0.0.1:8000`; Tailscale Serve stellt es privat und mit vertrauenswürdigem HTTPS bereit. Kein öffentliches Funnel und kein zusätzliches WAN-Portforwarding erforderlich.

## Sichere Funktionsgrenzen dieses Meilensteins

- Home Assistant ist **lesend** verfügbar: echte Zustände, Geräteübersicht und Temperaturen in °C. Schalten und gefährliche Desktop-/Shell-/Shutdown-Aktionen sind in diesem ersten Servermodus gesperrt.
- Cannanas-Sprachaktionen (Lesen + zweistufiges Schreiben) sind jetzt Bestandteil dieser Sprachstrecke; Details im Abschnitt „Cannanas" unten. Sie werden erst nach der Operator-Abnahme (Adapter-Installation, eigener Schlüssel, Live-Abnahme) am Zielserver freigeschaltet. Keine geratenen API-Endpunkte oder CLI-Pakete als Ersatz installieren.
- Telegram, iPhone-Audioausgabe und Raumsatelliten sind nicht Bestandteil dieser ersten Abnahme.
- Konfigurations- und Zugangsdaten liegen nur lokal; kein HA-, Gemini- oder Cannanas-Schlüssel wird hier dokumentiert.

## Tatsächlich geprüfte Ebenen

Am Zielserver geprüft: echter Gemini-Live-Verbindungsaufbau, private HTTPS-Kopplung, Ablehnung unautorisierter Requests, Textbefehl mit gesprochener Modellantwort, Zugriff auf den expliziten analogen Pulse-Ausgang und Phone-Audio-Protokoll mit einer ausdrücklich generierten Test-WAV. Eine echte HA-Sensorabfrage lieferte denselben Wert wie die direkte Quelle. Die vollständige Suite bestand nach der Ducking-Integration auf dem Zielserver mit 71 Tests einschließlich Node-Browser-Lifecycle-Tests und echten Signal-/Guard-Kindprozessen. Diese Prozesse verwenden für einzelne Unit-Tests Mixer-Adapter; zusätzlich wurde der echte `pactl`-Adapter auf dem Zielhost geprüft.

Das sind **keine** Nachweise für Safari-Mikrofonrechte auf deinem konkreten iPhone oder die akustische Verständlichkeit im Raum. Der abschließende Nutzertest ist: Musik abspielen, JARVIS über das iPhone nach einer Temperatur fragen, leise weiterlaufende Musik während der gesprochenen Antwort hören, Rückkehr zum bisherigen Pegel prüfen und einmal mit STOP abbrechen.

## Rollback-Hinweis

Vor der einmaligen System-zu-Benutzerdienst-Migration wurden Paketkonfiguration und Spotify-Zustand privat unter `~/.local/state/lacasa-audio-backup/` gesichert. Die Datei `latest` verweist auf die betreffende Sicherung. Kein automatisches Rollback ausführen, während der neue Empfänger funktioniert; sonst entsteht eine konkurrierende Audio-/Connect-Instanz oder laufende Musik wird unterbrochen.

## Spotify voice control (Task 5)

App name: any personal name
Website: blank
Redirect URI: https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback
API/SDK: Web API
Credential requested from user: Client ID only (never client secret/password)
Start: .venv/bin/python -m core.headless spotify-auth-start --client-id '<CLIENT_ID>'
Status: .venv/bin/python -m core.headless spotify-auth-status

The authorization URL is opened by the operator over trusted HTTPS. Consent must be repeated after six calendar months/revocation/invalid_grant. The status command never displays tokens. Spotify Premium is required for playback mutating actions; reads (search, playlists, library, get_state) work on Free.

Seven German voice-action examples:
- „Spiele Bohemian Rhapsody." (play, track)
- „Spiele das Album A Night at the Opera." (play, album)
- „Spiele Queen." (play, artist)
- „Pause die Wiedergabe." / „Setze die Wiedergabe fort."
- „Nächster Titel." / „Vorheriger Titel."
- „Stelle die Lautstärke auf 50 Prozent."
- „Was läuft gerade?" (current)

Exact receiver: raspotify (lacasa-server). All playback is directed exclusively at this receiver; the iPhone or any other device is never controlled.

Safe troubleshooting:
- If `spotify-auth-status` returns `authorized: false`, re-run `spotify-auth-start`.
- Consent must be re-granted after six calendar months, revocation, or `invalid_grant`; a single 401 during playback is handled by one automatic token refresh and one retry, an expired reauthorization needs a new manual `spotify-auth-start`.
- Network errors appear as "Spotify API ist zurzeit nicht erreichbar" in the assistant output.

These steps are explicitly deferred to the separate durable-device plan; current transient `pair` instructions stay valid.

## Cannanas im Sprachbefehl

Cannanas ist über die gesprochene Steuerung erreichbar. Die Antworten sind kurz und gebunden: Listen mit **höchstens fünf namentlich genannten** Einträgen, strukturell höchstens 25 Einträge pro Antwort. Bei nicht abgeprüften Mengen sagt JARVIS bewusst „mindestens“. Die komplette Architektur und die Operator-Schritte stehen in `docs/cannanas-jarvis-integration.md`; die Installation des gepinnten Adapters steht in `deploy/cannanas/README-DEPLOY.md`.

### Lesen (direkt, ohne Bestätigung)

Beispiele auf dem iPhone oder am Serverlautsprecher:

- „Wie viele Pflanzen stehen im Wachraum?“
- „Welche Pflanzen stehen in der Blüte?“
- „Welche Batches wurden zuletzt angelegt?“
- „Welches Vermehrungsmaterial ist verfügbar?“

### Schreiben (immer zweistufig)

Jeder Schreibvorgang braucht zwei getrennte Äußerungen und läuft **über den Serverhost, nie über das Sprachmodell**:

1. **Vorbereiten:** „Lege eine Pflanze mit dem Namen X im Wachraum an.“ JARVIS liest die **komplette Vorschau** jedes zu schreibenden Feldes vor und fragt dann direkt: „Bitte bestätigen Sie mit einem klaren „ja“, oder sagen Sie „abbrechen“." In diesem Schritt wurde noch nichts geschrieben.
2. **Bestätigen:** Erst eine **spätere**, abgeschlossene Äußerung mit exakt einem der Bestätigungsworte löst das Speichern aus. Das Modell kann nicht selbst bestätigen; Bestätigungsnummern oder Kennungen werden ihm nie gezeigt.

Die Bestätigungs-Grammatik ist geschlossen und deterministisch — nur diese Formen zählen:

- **Zustimmen:** „ja“, „ok“, „okay“, „bestätigen“, „bestätige“, „bitte bestätigen“, „ja bitte“, „speichern“, „speichere“
- **Abbrechen:** „nein“, „abbrechen“, „brich ab“, „abbruch“, „stop“, „stopp“, „stoppe“, „stoppen“

Jedes andere Gesagte (inklusive sinngemäßer Zustimmung) ist **keine** Bestätigung; die Vorschau bleibt dann fünf Minuten lang bestehen. Alles Weitere bleibt im Modellpfad.

### Was zuverlässig geschützt ist

- Pro Gerät gilt nur **ein** ausstehender Vorgang; eine neue Vorbereitung verwirft die vorherige Vorschau. Jede Vorschau läuft nach **fünf Minuten** ab; danach ist erneut vorzubereiten.
- **STOP** oder eine unterbrochene Aufnahme bestätigt nie etwas: STOP verwirft die offene Preview, ohne etwas zu schreiben.
- Nur das Gerät, dessen Mikrofontaste die Vorbereitung ausgelöst hat, kann seine eigene Preview bestätigen; unterbrochene, verschränkte oder widerrufene Geräte haben keinerlei Bestätigungsmöglichkeit.
- Nach einem Neustart von JARVIS **oder** des Adapters ist jede ausstehende Preview veraltet — es ist dann erneut vorzubereiten.
- Schlägt die Schreibprüfung oder die Adapter-Verbindung fehl, bleiben Spotify, Home Assistant und JARVIS-Gespräche vollständig aktiv; die Meldung ist eine kurze deutsche Störungszeile statt einer ausführlichen Diagnose.

Die zugehörigen Zugangsdaten stehen ausschließlich lokal (Template: `deploy/cannanas/cannanas-adapter.credentials.example`); nie in Git, Chat oder Dokumentation. Keine Klubkennung oder Bestätigungsnummer steht in den Modellergebnissen, Logs im Dashboard oder Antworten.
