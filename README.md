# DOCX Speaker Highlighting

Dieses kleine CLI-Tool markiert Sprecher-Namen am Anfang von Dialogzeilen in `.docx`-Dateien.
Es ändert nur die Formatierung des `NAME:`-Präfix und schreibt immer eine neue Ausgabedatei.

## Web-App

Die Web-App stellt dieselbe Highlight-Logik im Heimnetz bereit. Sie speichert Produktions-Presets zentral in der lokalen Datei `presets.json`, nimmt eine `.docx`-Datei entgegen und exportiert eine eingefärbte PDF-Datei.

`presets.json` ist bewusst nicht im Git-Repository enthalten, damit Produktionsdaten lokal bleiben. Für eine neue Installation kann `presets.example.json` als leere Vorlage verwendet werden.

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
