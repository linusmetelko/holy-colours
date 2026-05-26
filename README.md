# DOCX Speaker Highlighting

Dieses kleine CLI-Tool markiert Sprecher-Namen am Anfang von Dialogzeilen in `.docx`-Dateien.
Es ändert nur die Formatierung des `NAME:`-Präfix und schreibt immer eine neue Ausgabedatei.

## Web-App

Die Web-App stellt dieselbe Highlight-Logik im Heimnetz bereit. Benutzer können eigene Accounts erstellen; Produktions-Presets werden pro Benutzer in der lokalen Datei `presets.json` gespeichert. Die App nimmt eine `.docx`-Datei entgegen und exportiert eine eingefärbte PDF-Datei.

`presets.json` ist bewusst nicht im Git-Repository enthalten, damit Produktionsdaten lokal bleiben. Für eine neue Installation kann `presets.example.json` als leere Vorlage verwendet werden. Falls noch eine alte Datei mit gemeinsamer `presets`-Liste existiert, übernimmt der erste neu erstellte Benutzer diese Presets automatisch.

Preset-Dateien können nach dem Login über `Exportieren` geteilt oder gesichert und über `Importieren` wieder in den aktuellen Benutzer importiert werden. Unterstützt werden alte Backups mit `{"presets":[...]}` sowie neue Benutzer-Backups.

Für den PDF-Export muss LibreOffice auf dem Server installiert sein, damit `soffice` verfügbar ist.

```bash
python3 web_app.py
```

Danach ist die App lokal unter `http://localhost:8000` erreichbar. Auf anderen Geräten im Heimnetz kann sie über die IP-Adresse des Mac mini geöffnet werden, zum Beispiel:

```text
http://192.168.1.20:8000
```

Optional können Host und Port gesetzt werden:

```bash
python3 web_app.py --host 0.0.0.0 --port 8000
```

## Docker-Deployment hinter Traefik

Für einen VPS mit bestehendem Traefik wird ein eigenes Docker-Image gebaut. Das Image enthält Python, LibreOffice und die Microsoft-Core-Fonts, damit der PDF-Export `Courier New` in 12 pt korrekt rendern kann.

### DNS vorbereiten

Lege für die gewünschte Subdomain einen `A`-Record auf die IPv4-Adresse deines VPS an. Falls du IPv6 nutzt, ergänze zusätzlich einen `AAAA`-Record.

Beispiel:

```text
colours.example.com -> 203.0.113.10
```

### VPS vorbereiten

Kopiere das Repository auf den VPS und lege die `.env` an:

```bash
git clone <repo-url> holy-colours
cd holy-colours
cp .env.example .env
```

Passe anschließend `.env` an:

- `HOLY_COLOURS_DOMAIN`: deine Subdomain
- `TRAEFIK_ENTRYPOINT`: meist `websecure`
- `TRAEFIK_CERTRESOLVER`: Name deines bestehenden Let's-Encrypt-Resolvers

### Starten und aktualisieren

Starte die App:

```bash
docker compose up -d --build
```

Die App veröffentlicht nur `127.0.0.1:8000` auf dem Host. Das passt zu Traefik im Host-Netzwerk: Traefik kann die App lokal erreichen, der Port ist aber nicht direkt auf der öffentlichen VPS-IP geöffnet. Traefik übernimmt HTTPS, die App schützt den Zugriff per eigenem Login-Screen.

Benutzer und Presets werden im Docker-Volume `holy-colours-data` gespeichert. Den ersten Benutzer legst du direkt auf der Login-Seite über `Account erstellen` an.

Updates laufen über:

```bash
git pull
docker compose up -d --build
```

Logs und Status:

```bash
docker compose ps
docker compose logs -f holy-colours
```

### Backup

Die Produktions-Presets liegen im Docker-Volume `holy-colours-data` unter `/data/presets.json`. Sichere dieses Volume regelmäßig, zum Beispiel zusammen mit den Hostinger-VPS-Backups.

## Verwendung

```bash
python3 highlight_names.py script.docx --config colors.example.json
```

Optional kann ein eigener Ausgabepfad gesetzt werden:

```bash
python3 highlight_names.py script.docx --config colors.example.json --output script.colored.docx
```

## Konfiguration

Die JSON-Datei hat zwei Bereiche:

- `name_colors`: feste Zuordnung von Sprechername zu Highlight-Farbe
- `fallback_colors`: Ersatzfarben für unbekannte Namen

Farben müssen als Hexwerte im Format `#RRGGBB` angegeben werden.

## Tests

```bash
python3 -m unittest discover -s tests
```
