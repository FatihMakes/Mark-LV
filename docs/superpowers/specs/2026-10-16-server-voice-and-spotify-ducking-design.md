# LaCasa JARVIS: iPhone-Eingabe, Server-Ausgabe und Spotify-Ducking

Status: Vom Gärtner freigegeben; Umsetzung beauftragt. Builder-Check abgeschlossen. Die bestätigte Bedienung lautet: Mikrofon während der Antwort geschlossen, Abbruch per Stopptaste. Implementierungsplan und tatsächliche Nachweise stehen unter `docs/superpowers/plans/2026-10-16-server-voice-and-spotify-ducking.md` und `docs/server-voice-quickstart.md`. Die Freigabe dieses Designs ist kein Ersatz für die dort getrennt ausgewiesenen technischen und akustischen Abnahmen.

## Zweck und vom Gärtner festgelegtes Verhalten

JARVIS läuft headless auf `lacasa-server`, ohne Bildschirm, Desktop oder Mark-LV-GUI. Zuerst dient das iPhone über die vorhandene Remote-Webseite als Mikrofon. Gesprochene Antworten kommen ausschließlich aus den am Server angeschlossenen Lautsprechern. Später werden Raumsatelliten als Ein- und Ausgabegeräte ergänzt; sie gehören nicht zum ersten Abnahmetest.

Spotify und JARVIS müssen gleichzeitig laufen. Während einer tatsächlich gesprochenen JARVIS-Antwort wird nur Spotify so weit abgesenkt, dass JARVIS klar verständlich ist. Nach Ende der hörbaren Antwort kehrt Spotify zum vorherigen Pegel zurück. Spotify darf dafür nicht pausiert, beendet oder neu gestartet werden. Ein Titelwechsel darf den Ablauf nicht stören. Telegram bleibt vollständig außerhalb des Projektumfangs.

## Bestandsaufnahme (technisch geprüft, keine Fertigmeldung)

- Repository: `https://github.com/qartex/Mark-LV-LaCasa`; lokale Arbeitskopie auf `lacasa15` unter `/root/Mark-LV-LaCasa`; Zielkopie auf dem anderen Rechner `lacasa-server` unter `/home/lacasa/Mark-LV-LaCasa`.
- `lacasa-server`: Tailscale `100.68.76.27`, LAN `192.168.1.180`, Ubuntu Server, Dienstbenutzer `lacasa`. ALSA-Karte `PCH`, ALC1150 Analog, Gerät 0. Die tatsächliche Verkabelung und hörbare Wiedergabe müssen vom Nutzer bestätigt werden.
- Home Assistant: separater Rechner `lacasa17`, `lacasa17.tail7a9c2.ts.net:8123`, Tailscale `100.74.152.40`; extern `https://homelacasa.qartex.com`.
- Cannanas-MCP liegt derzeit auf `lacasa15` unter `/home/lacasa15/services/cannanas-mcp/`, nicht nachweislich auf dem Zielserver. Nicht umbenennen, nicht durch ein erfundenes Python-Paket ersetzen. Eine spätere zusätzliche lokale Instanz auf `lacasa-server` ist erwünscht; bestehende Hermes-Nutzung bleibt unberührt.
- Raspotify 0.48.3 / librespot 0.8.0 ist auf dem Zielhost als Paketdienst aktiv und aktiviert. Name aktuell `raspotify (lacasa-server)`. Connect meldet `OK`, Konto noch nicht gekoppelt. Spotify Premium ist Voraussetzung dieses Empfängers.
- ALSA-Stille wurde erfolgreich über `default` und `plughw:CARD=PCH,DEV=0` ausgegeben. Das beweist den geöffneten Ausgang, nicht hörbare Musik oder Sprache.
- Zielvenv `/home/lacasa/Mark-LV-LaCasa/.venv` existiert. Der aktuelle JARVIS-Import scheitert noch an der PulseAudio-Initialisierung durch PortAudio/sounddevice.
- Die existierende Remote-Seite ist kein eigenständiger ASGI-Service: `DashboardServer` wird aus JARVIS gestartet; `uvicorn dashboard.server:app` ist kein gültiger Startweg. Der bisherige QR-Schlüssel wird nur über einen GUI-Callback erzeugt.
- `ui_headless.py` besitzt eine gefährliche Auto-Bestätigungsabsicht und eine inkompatible Bestätigungssignatur. Diese ist ausdrücklich kein akzeptables Server-Verhalten.
- `.gitignore` ist seit Commit `4b77e55` korrigiert: Zugangsdaten bleiben lokal. Vorherige Inline-Kommentare hinter Mustern verhinderten das Ignorieren. Alte Anleitungen und Planbehauptungen sind nicht als Ausführungsnachweis zu verwenden.

## Vorgeschlagene Architektur — Entscheidung des Erstellers, noch zu genehmigen

Die Python-Codebasis und Gemini Live bleiben erhalten. Kein Neuaufbau der Anwendung, kein Avatar und keine GUI-Abhängigkeiten im unterstützten Server-Startpfad.

Für gemischte lokale Audioausgabe wird eine gemeinsame benutzerspezifische PipeWire/WirePlumber-Audiositzung mit PulseAudio-Kompatibilität empfohlen. Spotify und JARVIS geben als derselbe unprivilegierte Dienstbenutzer an diese Sitzung aus. Das vermeidet konkurrierende exklusive Zugriffe auf `plughw` und gibt jedem Programm einen separat regelbaren Audiostream. Die endgültige Auswahl des analogen Sink-Namens erfolgt anhand echter Gerätedaten, nicht wechselnder numerischer Indizes.

Der bestehende Raspotify-Systemdienst wird erst nach gesicherter Konfiguration und vorbereitetem Ersatz einmalig auf die gemeinsame Audiositzung umgestellt. Während dieser Einrichtung kann es eine angekündigte einmalige Unterbrechung geben; im späteren normalen Ducking-Betrieb sind Dienstneustarts, Track-Pausen und Gerätewechsel ausgeschlossen. Es darf nie gleichzeitig ein alter und ein neuer Spotify-Empfänger mit konkurrierenden Namen/Ports aktiv sein. Vor einer Migration auf bereits gekoppelte Zugangsdaten sichern, ohne sie im Chat oder Git auszugeben. Audio-Backends der tatsächlich installierten librespot-Version vor der Auswahl prüfen.

Der Ducking-Controller verändert ausschließlich den identifizierten Spotify-Stream beziehungsweise eine dedizierte Spotify-Gain-Stufe. Er verändert weder den Master-Sink noch JARVIS' Ausgangspegel. Es darf keine Spotify-Web-API-Autorisierung nötig sein, nur um die lokale Musik abzusenken. JARVIS' eigene Lautstärke bleibt separat einstellbar.

## Erste Benutzerstrecke

1. JARVIS startet als Serverdienst ohne Bildschirm, globale Tastenkürzel oder lokales Mikrofon. Lokale Lautsprecherausgabe ist aktiv, lokale Aufnahme in dieser Betriebsart deaktiviert.
2. Ein expliziter Headless-Kopplungsbefehl erzeugt einen kurzlebigen Link und QR-Code für genau diesen Server. Keine Schlüssel, HA-Tokens oder Gemini-Keys im Git. Ein neuer Schlüssel ersetzt den alten; Gültigkeit und Widerruf werden getestet.
3. Zugriff nur über das private Netz/Tailscale und eine für Safari vertrauenswürdige HTTPS-Adresse. Bevorzugt Tailscale Serve mit Zertifikat für den tatsächlichen `lacasa-server`-Hostnamen. Vor der Einrichtung vorhandene Serve-Konfiguration prüfen und erhalten. Kein öffentliches Funnel, kein willkürlich geöffnetes WAN-Portforwarding, keine automatischen Firewalländerungen.
4. Die vorhandene schlichte Remote-Seite wird wiederverwendet, nicht durch eine Desktop-GUI ersetzt. Eine explizite Aufnahmetaste öffnet das iPhone-Mikrofon; es gibt kein Dauerlauschen. Vom Gärtner bestätigt: Während einer JARVIS-Antwort bleibt die Aufnahme gesperrt; Unterbrechung erfolgt ausschließlich über die Stopptaste. Der Server nimmt in dieser Phase keine weiteren Mikrofonframes zur Verarbeitung an. Wartende Aufnahmeframes werden verworfen, nicht nachträglich als neuer Befehl abgespielt. Stoppen/Trennen beendet die Aufnahme. Safari-Mikrofonrechte und Secure Context werden auf einem echten iPhone geprüft.
5. Mikrofon-Audio wird authentifiziert zum Server übertragen und an Gemini Live gegeben. Der Server spielt die Antwort auf den angeschlossenen Boxen ab. Auf dem iPhone erscheint der Text/Status, aber in diesem Meilenstein keine Audioantwort. Die Stopptaste bleibt während der Aufnahme-Sperre über den authentifizierten Steuerkanal verfügbar: Sie bricht die aktuelle Antwort ab, leert noch nicht abgespielte Antwortframes und verwirft weitere Frames dieser abgebrochenen Antwort. Erst nach dem verbleibenden hörbaren Ausklang wird die Aufnahmetaste wieder freigegeben. Dieses Halbduplex-Verhalten benötigt keine geräteübergreifende Echo-Unterdrückung; automatische Sprachunterbrechung ist ausdrücklich kein Abnahmekriterium.
6. Unauthentifizierte Mikrofon-, Steuer- und WebSocket-Anfragen sind abzuweisen. Es gibt nicht gleichzeitig mehrere aktive iPhone-Mikrofonquellen; eine zweite Quelle erhält einen klaren Besetztstatus. Keine Vermischung von Audiositzungen.

## Ducking: präzises Verhalten

### Beginn

Ducking beginnt unmittelbar vor dem ersten tatsächlich zur Ausgabe anstehenden JARVIS-Audioblock. Nicht beim Empfang eines Transkripts und nicht pauschal bei jedem Modellturn: reine Textantworten, Toolaufrufe und verworfene Audiodaten senken keine Musik ab.

Vor der Änderung wird der tatsächliche Spotify-Gain für alle Kanäle gelesen und pro Ausgabephase gespeichert. Kein angenommener Standardwert und kein Wiederherstellen auf 100 %. Ein stummgeschalteter Spotify-Stream bleibt stummgeschaltet.

Empfohlener Anfangswert des Erstellers: Spotify relativ zum Ausgangspegel ungefähr 18 dB absenken, mit kurzer weicher Rampe. Dieser Wert ist konfigurierbar. Verständlichkeit muss vor Ort geprüft werden; die Software kann Raumakustik und Boxenpegel nicht automatisch garantieren. Rechenwerte für lineare Lautstärkefaktoren werden bei der Implementierung berechnet und getestet, nicht als dB/Prozent gleichgesetzt.

### Während der Ausgabe

Spotify läuft zeitlich weiter. JARVIS bekommt unveränderten Pegel. Nur eindeutig als Spotify erkannte Streams werden geändert; bei unsicherer Zuordnung kein Eingriff in fremde Audiostreams.

Ducking wird pro Ausgabephase verwaltet: verschachtelte Ausgabeanforderungen und mehrere Audioblöcke dürfen den bereits abgesenkten Wert nicht erneut als Ausgangspegel speichern. Kurze Pausen innerhalb eines Satzes führen nicht zum hörbaren Auf- und Abpumpen. Ein neu entstehender Spotify-Stream bei laufender Sprachausgabe wird erkannt und abgesenkt; sein Originalpegel wird gesondert gesichert.

Die Spotify-App darf ihren eigentlichen Musikpegel weiterhin verändern. Die Ducking-Hüllkurve ist davon möglichst getrennt zu halten: eine während der Ansage absichtliche neue Nutzerlautstärke darf nachher nicht durch einen veralteten absoluten App-Wert überschrieben werden. Ohne Nutzeränderung muss die Musik zum exakt vorherigen Gain zurückkehren. Wie die unabhängige Gain-Stufe mit dem gewählten Audio-Backend abgebildet wird, ist vor dem Produktivumbau durch einen kleinen technischen Test zu belegen.

### Ende und Fehler

Wiederherstellung erst nach Ende der hörbaren Ausgabe: Audioqueue, Gerätelatenz und Ausklang berücksichtigen. `turn_complete` oder die Rückkehr von `stream.write()` allein bedeuten nicht, dass die Box fertig ist. Danach weich auf den gesicherten Ausgangspegel zurückfahren.

Abbruch, Benutzerunterbrechung, Gemini-Verbindungsfehler und geordnetes Beenden führen ebenfalls zur Freigabe nach dem tatsächlich verbleibenden Audioausklang. `try/finally` allein reicht bei hartem Prozessabsturz nicht: ein unabhängiger Wiederherstellungsmechanismus mit Ablauf/Heartbeat muss hängen gebliebenes Ducking erkennen und die gespeicherten Gains wiederherstellen, ohne bei lebender Sprachausgabe voreilig anzuheben. Dieser Zustand enthält keine Zugangsdaten und gehört nicht ins Git.

Wenn Spotify nicht läuft oder pausiert ist, spricht JARVIS normal. Der Controller startet Spotify nicht von sich aus. Fällt nur die Ducking-Steuerung aus, werden keine Dienste beendet oder automatisch neu gestartet. Es gibt eine erkennbare Fehlermeldung und einen degradierten Status; der kombinierte Abnahmetest gilt dann nicht als bestanden.

## Sicherheit und Begrenzungen

- Keine automatische Bestätigung gefährlicher Befehle. Die Headless-Schnittstelle muss dieselbe Signatur wie das echte Confirmation-Gate besitzen und geschützte Aktionen ablehnen, solange keine echte authentifizierte menschliche Zustimmung implementiert ist.
- Keine Freigabe von Shell-, Datei-, Kamera-, Shutdown- oder WLAN-Steuerung durch Sprachmodell-Defaults. Nur für diesen Meilenstein erforderliche Tools explizit zulassen.
- HA zunächst lesend testen. Geräteänderungen nur nach konkretem Auftrag; kein Test mit erfundenen Entity-IDs als Erfolgsnachweis.
- Cannanas-Zugriff als eigenen Meilenstein über einen echten MCP-Client planen. Datenänderungen benötigen weiterhin Vorschau und explizite Bestätigung; Sorten-HID bleibt automatisch. Keine erfundenen REST-Routen.
- Konfiguration mit echten Keys nur lokal und restriktiven Dateirechten. Keine Token-Erstellung oder Rotation als Routinevoraussetzung; vorhandene Credentials sicher aus Dateien lesen und nicht neu in Requests abtippen.
- Spotify-Nutzungsrechte für öffentliche/gewerbliche Beschallung sind separat zu klären. Diese technische Einrichtung gewährt keine solchen Rechte.

## Abnahme und Nachweise

Automatisiert mit kontrollierten Audio-/Mixer-Adaptern:

- Original-Gains einschließlich unterschiedlicher Kanalpegel werden einmal gespeichert und nach einer normalen Antwort identisch wiederhergestellt.
- Spotify-Gain wird abgesenkt, JARVIS-Gain und Master-Sink bleiben unangetastet.
- Keine Pause-, Stop-, Restart- oder Seek-Befehle an Spotify während des Duckings.
- Überlappende Ausgaben, Satzpausen, neu erzeugte Streams, Stummzustände, manuelle Lautstärkeänderung und fehlender Spotify-Stream sind abgedeckt.
- Abbruch, Exception und simulierter harter JARVIS-Ausfall stellen die Musik ohne bleibende Absenkung wieder her.
- Headless startet ohne Display und ohne lokales Mikrofon; fehlender Key beendet kontrolliert statt Zugriff auf GUI-private `_win`-Attribute.
- Authentifizierungs-, Kopplungsablauf- und Bestätigungsgate-Tests weisen unerlaubte Aktionen nachweislich ab.

Auf dem echten `lacasa-server`:

1. Audio-Sitzung und beide Dienste laufen nach Anmeldung sowie nach kontrolliertem Neustart ohne Desktop; analoge Ausgabe ist richtig zugeordnet.
2. Spotify ist tatsächlich gekoppelt und spielt einen Titel. Nicht allein `systemctl active` oder eine erfolgreiche Ausgabe von Stille als Musiktest werten.
3. JARVIS spricht eine harmlose Testansage. Musik spielt dabei leise weiter, Sprache ist verständlich, danach kehrt der vorherige Pegel zurück. Vorher-/Während-/Nachher-Gains und Service-Identität aufzeichnen; kein unbemerkter Dienstneustart.
4. Safari öffnet den echten vertrauenswürdigen HTTPS-Link; QR-Kopplung, Mikrofonaufnahme und eine HA-Abfrage funktionieren. Die gesprochene Antwort kommt aus den Server-Boxen.
5. Tests mit Unterbrechung, erneutem Verbinden und einem gezielt simulierten JARVIS-Abbruch belegen den Wiederherstellungspfad.

Hörtests und iPhone-Mikrofonrechte benötigen Rückmeldung des Gärtners. Bis dahin werden sie als ungetestet ausgewiesen, nicht als bestanden.

## Reihenfolge und Auslieferung

Nach Designfreigabe einen eigenen Implementierungsplan mit Superpowers schreiben. Reihenfolge: sichere Bestätigungs-/Startbasis; gemeinsamer Audio-Backend-Spike und getrennte Spotify-Gain-Stufe; TDD-Ducking-Controller samt Recovery; Integration in echten Playback-Lebenszyklus; Headless-Kopplung und private HTTPS-Remote-Eingabe; Gesamtabnahme. Unabhängige Aufgaben an Subagents vergeben, anschließend Anforderungs- und Qualitätsprüfung.

Jede zusammenhängende, geprüfte Änderung wird ohne Secrets in `qartex/Mark-LV-LaCasa` committet und gepusht; GitHub-Commit und tatsächlich ausgerollte Serverversion werden separat überprüft. Bestehende ungeprüfte Cannanas-Änderungen und veraltete Dashboard-Anleitungen nicht versehentlich mitstagen.

Nicht Bestandteil des ersten Meilensteins: iPhone-Audioausgabe, Echo-Dot-Flashen, ESP32-Firmware, Multiroom-Routing, Telegram und neue Cannanas-Schreibfunktionen. Die spätere Satelliten-Ausgabe soll denselben Playback-/Ducking-Lebenszyklus nutzen; ihre konkrete Hardware und Netzwerkverbindung werden separat geplant.
