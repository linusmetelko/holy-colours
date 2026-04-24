#!/usr/bin/env python3
"""Kleine lokale Web-App zum Einfärben von DOCX-Sprechernamen und PDF-Export."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import highlight_names

ROOT_DIR = Path(__file__).resolve().parent
PRESETS_PATH = ROOT_DIR / "presets.json"
COLORS_EXAMPLE_PATH = ROOT_DIR / "colors.example.json"
DEFAULT_FALLBACK_COLORS = ["#F4CCCC", "#D9EAD3", "#CFE2F3", "#FFF2CC"]
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class WebAppError(Exception):
    """Fehler, der Web-Clients angezeigt werden kann."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_color(value: object, field: str) -> str:
    if not isinstance(value, str) or not HEX_COLOR_RE.fullmatch(value):
        raise WebAppError(f"{field} muss ein Hex-Farbwert wie #FFD966 sein.")
    return value.upper()


def validate_name_colors(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WebAppError("name_colors muss ein Objekt sein.")

    colors: dict[str, str] = {}
    for raw_name, raw_color in value.items():
        if not isinstance(raw_name, str):
            raise WebAppError("Alle Sprechernamen müssen Text sein.")
        name = raw_name.strip()
        if not name:
            continue
        colors[name] = normalize_color(raw_color, f"name_colors.{name}")
    return colors


def validate_fallback_colors(value: object) -> list[str]:
    if not isinstance(value, list):
        raise WebAppError("fallback_colors muss eine Liste sein.")
    colors = [
        normalize_color(raw_color, f"fallback_colors[{index}]")
        for index, raw_color in enumerate(value)
    ]
    if not colors:
        raise WebAppError("Mindestens eine Fallbackfarbe ist erforderlich.")
    return colors


def validate_processing_config(value: object) -> tuple[dict[str, str], list[str]]:
    if not isinstance(value, dict):
        raise WebAppError("Die Konfiguration muss ein Objekt sein.")
    return (
        validate_name_colors(value.get("name_colors", {})),
        validate_fallback_colors(value.get("fallback_colors", [])),
    )


def colors_for_highlighter(colors: dict[str, str]) -> dict[str, str]:
    return {name: color[1:] for name, color in colors.items()}


def fallback_for_highlighter(colors: list[str]) -> list[str]:
    return [color[1:] for color in colors]


def load_default_config() -> dict[str, object]:
    if COLORS_EXAMPLE_PATH.exists():
        try:
            raw = json.loads(COLORS_EXAMPLE_PATH.read_text(encoding="utf-8"))
            name_colors = validate_name_colors(raw.get("name_colors", {}))
            fallback_colors = validate_fallback_colors(raw.get("fallback_colors", []))
            return {"name_colors": name_colors, "fallback_colors": fallback_colors}
        except (json.JSONDecodeError, WebAppError):
            pass
    return {"name_colors": {}, "fallback_colors": DEFAULT_FALLBACK_COLORS}


def read_presets(path: Path = PRESETS_PATH) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WebAppError(f"Die Preset-Datei enthält ungültiges JSON: {exc}") from exc

    presets = raw.get("presets") if isinstance(raw, dict) else None
    if not isinstance(presets, list):
        raise WebAppError("Die Preset-Datei muss eine Preset-Liste enthalten.")

    validated = []
    for index, preset in enumerate(presets):
        if not isinstance(preset, dict):
            continue
        preset_id = str(preset.get("id") or "").strip()
        name = str(preset.get("name") or "").strip()
        if not preset_id or not name:
            continue
        try:
            name_colors = validate_name_colors(preset.get("name_colors", {}))
            fallback_colors = validate_fallback_colors(preset.get("fallback_colors", []))
        except WebAppError as exc:
            raise WebAppError(f"Ungültiges Preset an Position {index}: {exc.message}") from exc
        validated.append(
            {
                "id": preset_id,
                "name": name,
                "name_colors": name_colors,
                "fallback_colors": fallback_colors,
                "updated_at": str(preset.get("updated_at") or ""),
            }
        )
    return validated


def write_presets(presets: list[dict[str, object]], path: Path = PRESETS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"presets": presets}, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp_file:
        tmp_file.write(data)
        tmp_path = Path(tmp_file.name)
    tmp_path.replace(path)


def save_preset(payload: object, path: Path = PRESETS_PATH) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise WebAppError("Das Preset muss als Objekt gesendet werden.")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise WebAppError("Ein Preset-Name ist erforderlich.")

    preset_id = str(payload.get("id") or "").strip() or uuid.uuid4().hex
    preset = {
        "id": preset_id,
        "name": name,
        "name_colors": validate_name_colors(payload.get("name_colors", {})),
        "fallback_colors": validate_fallback_colors(payload.get("fallback_colors", [])),
        "updated_at": now_iso(),
    }

    presets = read_presets(path)
    replaced = False
    for index, existing in enumerate(presets):
        if existing["id"] == preset_id:
            presets[index] = preset
            replaced = True
            break
    if not replaced:
        presets.append(preset)

    presets.sort(key=lambda item: str(item["name"]).casefold())
    write_presets(presets, path)
    return preset


def delete_preset(preset_id: str, path: Path = PRESETS_PATH) -> None:
    preset_id = preset_id.strip()
    if not preset_id:
        raise WebAppError("Eine Preset-ID ist erforderlich.")
    presets = read_presets(path)
    remaining = [preset for preset in presets if preset["id"] != preset_id]
    if len(remaining) == len(presets):
        raise WebAppError("Preset nicht gefunden.", HTTPStatus.NOT_FOUND)
    write_presets(remaining, path)


def find_soffice() -> str | None:
    for command in ("soffice", "libreoffice"):
        path = shutil.which(command)
        if path:
            return path
    app_path = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if app_path.exists():
        return str(app_path)
    return None


def convert_docx_to_pdf(
    input_docx: Path,
    output_dir: Path,
    *,
    find_converter: Callable[[], str | None] = find_soffice,
) -> Path:
    converter = find_converter()
    if converter is None:
        raise WebAppError(
            "LibreOffice wurde nicht gefunden. Installiere LibreOffice auf dem Mac mini, damit DOCX-Dateien als PDF exportiert werden können.",
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    command = [
        converter,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(input_docx),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise WebAppError(
            "LibreOffice hat beim PDF-Export zu lange gebraucht.",
            HTTPStatus.INTERNAL_SERVER_ERROR,
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = "LibreOffice konnte das PDF nicht exportieren."
        if detail:
            message = f"{message} {detail}"
        raise WebAppError(message, HTTPStatus.INTERNAL_SERVER_ERROR)

    output_pdf = output_dir / f"{input_docx.stem}.pdf"
    if not output_pdf.exists():
        candidates = sorted(output_dir.glob("*.pdf"))
        if candidates:
            return candidates[0]
        raise WebAppError(
            "LibreOffice wurde beendet, hat aber kein PDF erstellt.",
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    return output_pdf


def pdf_filename_for(upload_filename: str) -> str:
    stem = Path(upload_filename).stem or "highlighted"
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "highlighted"
    return f"{safe_stem}.colored.pdf"


def process_docx_upload(
    upload_filename: str,
    upload_bytes: bytes,
    config: object,
    *,
    find_converter: Callable[[], str | None] = find_soffice,
) -> tuple[str, bytes]:
    if not upload_filename.lower().endswith(".docx"):
        raise WebAppError("Bitte lade eine .docx-Datei hoch.")
    if not upload_bytes:
        raise WebAppError("Die hochgeladene DOCX-Datei ist leer.")

    name_colors, fallback_colors = validate_processing_config(config)
    with tempfile.TemporaryDirectory(prefix="holy-colours-") as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        input_docx = tmp_dir / "input.docx"
        colored_docx = tmp_dir / "colored.docx"
        input_docx.write_bytes(upload_bytes)

        try:
            highlight_names.process_docx(
                input_path=input_docx,
                output_path=colored_docx,
                name_colors=colors_for_highlighter(name_colors),
                fallback_colors=fallback_for_highlighter(fallback_colors),
            )
            pdf_path = convert_docx_to_pdf(
                colored_docx, tmp_dir, find_converter=find_converter
            )
            return pdf_filename_for(upload_filename), pdf_path.read_bytes()
        except (
            highlight_names.ConfigError,
            FileNotFoundError,
            KeyError,
            zipfile.BadZipFile,
            ET.ParseError,
        ) as exc:
            raise WebAppError(f"DOCX konnte nicht verarbeitet werden: {exc}") from exc


def parse_json_body(handler: BaseHTTPRequestHandler) -> object:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        raise WebAppError("Der Anfrageinhalt ist leer.")
    if length > MAX_UPLOAD_BYTES:
        raise WebAppError("Der Anfrageinhalt ist zu groß.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise WebAppError(f"Ungültiges JSON: {exc}") from exc


def parse_multipart(handler: BaseHTTPRequestHandler) -> dict[str, dict[str, object]]:
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        raise WebAppError("multipart/form-data erwartet.")

    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        raise WebAppError("Der Anfrageinhalt ist leer.")
    if length > MAX_UPLOAD_BYTES:
        raise WebAppError("Der Upload ist zu groß.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    body = handler.rfile.read(length)
    header = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8")
    message = BytesParser(policy=email_default_policy).parsebytes(header + body)
    if not message.is_multipart():
        raise WebAppError("Ungültiger multipart-Upload.")

    fields: dict[str, dict[str, object]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_param("filename", header="content-disposition")
        fields[name] = {
            "filename": filename,
            "content_type": part.get_content_type(),
            "content": part.get_payload(decode=True) or b"",
        }
    return fields


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def build_index_html() -> str:
    default_config_json = json.dumps(load_default_config(), ensure_ascii=False)
    default_config_json = default_config_json.replace("</", "<\\/")
    return INDEX_HTML.replace("__DEFAULT_CONFIG__", default_config_json)


class HolyColoursHandler(BaseHTTPRequestHandler):
    server_version = "HolyColours/1.0"

    def do_GET(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self.respond_bytes(
                    build_index_html().encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if path == "/api/presets":
                self.respond_json({"presets": read_presets()})
                return
            self.respond_error("Nicht gefunden.", HTTPStatus.NOT_FOUND)
        except WebAppError as exc:
            self.respond_error(exc.message, exc.status)

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/presets":
                preset = save_preset(parse_json_body(self))
                self.respond_json({"preset": preset})
                return
            if path == "/api/process":
                fields = parse_multipart(self)
                file_field = fields.get("file")
                config_field = fields.get("config")
                if not file_field or not config_field:
                    raise WebAppError("Der Upload muss Datei- und Konfigurationsfelder enthalten.")
                config = json.loads(bytes(config_field["content"]).decode("utf-8"))
                filename, pdf_bytes = process_docx_upload(
                    str(file_field.get("filename") or "input.docx"),
                    bytes(file_field["content"]),
                    config,
                )
                self.respond_bytes(
                    pdf_bytes,
                    "application/pdf",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    },
                )
                return
            self.respond_error("Nicht gefunden.", HTTPStatus.NOT_FOUND)
        except json.JSONDecodeError as exc:
            self.respond_error(f"Ungültiges JSON: {exc}", HTTPStatus.BAD_REQUEST)
        except WebAppError as exc:
            self.respond_error(exc.message, exc.status)

    def do_DELETE(self) -> None:
        try:
            prefix = "/api/presets/"
            path = self.path.split("?", 1)[0]
            if path.startswith(prefix):
                delete_preset(unquote(path[len(prefix) :]))
                self.respond_json({"ok": True})
                return
            self.respond_error("Nicht gefunden.", HTTPStatus.NOT_FOUND)
        except WebAppError as exc:
            self.respond_error(exc.message, exc.status)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def respond_json(
        self, value: object, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        self.respond_bytes(json_bytes(value), "application/json; charset=utf-8", status)

    def respond_error(
        self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST
    ) -> None:
        self.respond_json({"error": message}, status)

    def respond_bytes(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


INDEX_HTML = r"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Holy Colours</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f1ec;
      --surface: #ffffff;
      --surface-soft: #faf9f6;
      --ink: #1d2329;
      --muted: #68717b;
      --line: #dad5cc;
      --line-strong: #bbb3a6;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --accent-soft: #d9efea;
      --warn: #b95c37;
      --danger: #a33c3c;
      --danger-soft: #f4dfdd;
      --focus: #b9e3dc;
      --shadow: 0 18px 55px rgba(38, 35, 31, .12);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        linear-gradient(180deg, #ebe5dc 0, var(--bg) 320px),
        var(--bg);
      color: var(--ink);
    }

    main {
      width: min(1180px, calc(100% - 40px));
      margin: 0 auto;
      padding: 36px 0 48px;
    }

    header {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 22px;
    }

    h1 {
      margin: 0;
      font-size: 3.4rem;
      line-height: 1;
      letter-spacing: 0;
    }

    .eyebrow {
      margin: 0 0 8px;
      color: var(--accent-dark);
      font-size: .78rem;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }

    .status {
      min-height: 40px;
      max-width: 360px;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 9px 12px;
      color: var(--muted);
      text-align: right;
      font-size: .95rem;
      background: rgba(255, 255, 255, .56);
    }

    .status:empty { visibility: hidden; }
    .status.error {
      color: var(--danger);
      border-color: #e2b7b2;
      background: var(--danger-soft);
    }
    .status.ok {
      color: var(--accent-dark);
      border-color: #a9d7ce;
      background: var(--accent-soft);
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(280px, 370px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }

    section,
    aside {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      box-shadow: var(--shadow);
    }

    h2 {
      margin: 0 0 16px;
      font-size: 1.05rem;
      letter-spacing: 0;
    }

    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }

    .panel-head h2 { margin: 0; }

    label {
      display: block;
      margin: 12px 0 6px;
      font-size: .82rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
    }

    input,
    select,
    button {
      font: inherit;
    }

    input[type="text"],
    input[type="file"],
    select {
      width: 100%;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      padding: 11px 12px;
      background: var(--surface-soft);
      color: var(--ink);
    }

    input[type="color"] {
      width: 44px;
      height: 44px;
      padding: 2px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: var(--surface-soft);
    }

    button {
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      min-height: 40px;
      padding: 9px 13px;
      background: var(--surface);
      color: var(--ink);
      cursor: pointer;
      font-weight: 700;
      transition: border-color .15s ease, background-color .15s ease, color .15s ease, transform .15s ease;
    }

    button:hover {
      border-color: #8f8578;
      transform: translateY(-1px);
    }
    button:focus-visible, input:focus-visible, select:focus-visible {
      outline: 3px solid var(--focus);
      outline-offset: 1px;
    }

    .primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      font-weight: 700;
    }

    .primary:hover { background: var(--accent-dark); }
    .danger {
      color: var(--danger);
      border-color: #d7aaa5;
      background: #fff8f7;
    }

    .button-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }

    .row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 44px minmax(94px, 118px) 40px;
      gap: 8px;
      align-items: center;
      margin-bottom: 8px;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-soft);
    }

    .fallback-row {
      grid-template-columns: 44px minmax(94px, 118px) 40px;
      justify-content: start;
    }

    .icon-button {
      width: 40px;
      padding: 0;
      font-weight: 700;
    }

    .muted {
      color: var(--muted);
      font-size: .92rem;
      line-height: 1.45;
    }

    .stack { display: grid; gap: 18px; }
    .divider {
      height: 1px;
      background: var(--line);
      margin: 18px 0;
    }

    .hint {
      margin: -6px 0 14px;
      color: var(--muted);
      font-size: .9rem;
    }

    .file-box {
      border: 1px dashed var(--line-strong);
      border-radius: 8px;
      padding: 12px;
      background: #fbfaf7;
    }

    @media (max-width: 820px) {
      main { width: min(100% - 20px, 720px); padding-top: 22px; }
      header { align-items: flex-start; flex-direction: column; }
      h1 { font-size: 2.35rem; }
      .status { justify-content: flex-start; text-align: left; max-width: 100%; }
      .layout { grid-template-columns: 1fr; }
      .row { grid-template-columns: minmax(0, 1fr) 44px minmax(82px, 104px) 40px; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <p class="eyebrow">Lokale Produktions-App</p>
        <h1>Holy Colours</h1>
        <p class="muted">DOCX hochladen, Sprecherfarben wählen, PDF laden.</p>
      </div>
      <div id="status" class="status"></div>
    </header>

    <div class="layout">
      <aside>
        <h2>Produktion</h2>
        <label for="preset-select">Preset</label>
        <select id="preset-select"></select>

        <label for="preset-name">Name</label>
        <input id="preset-name" type="text" placeholder="z. B. Folge 85">

        <div class="button-row">
          <button id="new-preset" type="button">Neu</button>
          <button id="save-preset" type="button">Speichern</button>
          <button id="delete-preset" type="button" class="danger">Löschen</button>
        </div>

        <div class="divider"></div>

        <h2>Export</h2>
        <div class="file-box">
          <label for="docx-file">DOCX-Datei</label>
          <input id="docx-file" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document">
        </div>
        <div class="button-row">
          <button id="process" type="button" class="primary">PDF erstellen</button>
        </div>
      </aside>

      <section class="stack">
        <div>
          <div class="panel-head">
            <h2>Sprecherfarben</h2>
            <button id="add-name" type="button">+ Name</button>
          </div>
          <div id="name-colors"></div>
        </div>

        <div>
          <div class="panel-head">
            <h2>Fallbackfarben</h2>
            <button id="add-fallback" type="button">+ Farbe</button>
          </div>
          <p class="hint">Diese Farben werden der Reihe nach für unbekannte Sprecher verwendet.</p>
          <div id="fallback-colors"></div>
        </div>
      </section>
    </div>
  </main>

  <script id="default-config" type="application/json">__DEFAULT_CONFIG__</script>
  <script>
    const defaultConfig = JSON.parse(document.getElementById('default-config').textContent);
    const state = {
      presets: [],
      selectedPresetId: '',
      nameColors: {...(defaultConfig.name_colors || {})},
      fallbackColors: [...(defaultConfig.fallback_colors || ['#F4CCCC'])]
    };

    const el = (id) => document.getElementById(id);
    const statusEl = el('status');

    function setStatus(message, kind = '') {
      statusEl.textContent = message;
      statusEl.className = `status ${kind}`.trim();
    }

    function normalizeHex(value) {
      const text = String(value || '').trim();
      if (/^#[0-9a-fA-F]{6}$/.test(text)) return text.toUpperCase();
      return null;
    }

    function configFromUI() {
      const nameColors = {};
      document.querySelectorAll('#name-colors .row').forEach((row) => {
        const name = row.querySelector('[data-name]').value.trim();
        const hex = normalizeHex(row.querySelector('[data-hex]').value);
        if (name && hex) nameColors[name] = hex;
      });

      const fallbackColors = [];
      document.querySelectorAll('#fallback-colors .fallback-row').forEach((row) => {
        const hex = normalizeHex(row.querySelector('[data-hex]').value);
        if (hex) fallbackColors.push(hex);
      });

      if (!fallbackColors.length) {
        throw new Error('Mindestens eine gültige Fallbackfarbe ist notwendig.');
      }
      return { name_colors: nameColors, fallback_colors: fallbackColors };
    }

    function syncHexInputs(row, color) {
      const colorInput = row.querySelector('[data-color]');
      const hexInput = row.querySelector('[data-hex]');
      colorInput.value = color;
      hexInput.value = color;
      colorInput.addEventListener('input', () => { hexInput.value = colorInput.value.toUpperCase(); });
      hexInput.addEventListener('input', () => {
        const hex = normalizeHex(hexInput.value);
        if (hex) colorInput.value = hex;
      });
      hexInput.addEventListener('blur', () => {
        const hex = normalizeHex(hexInput.value);
        if (hex) hexInput.value = hex;
      });
    }

    function renderNameRows(nameColors = state.nameColors) {
      const container = el('name-colors');
      container.innerHTML = '';
      const entries = Object.entries(nameColors);
      if (!entries.length) entries.push(['', '#FFD966']);
      entries.forEach(([name, color]) => addNameRow(name, normalizeHex(color) || '#FFD966'));
    }

    function addNameRow(name = '', color = '#FFD966') {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = `
        <input data-name type="text" placeholder="Sprechername">
        <input data-color type="color">
        <input data-hex type="text" inputmode="text" placeholder="#FFD966">
        <button class="icon-button danger" type="button" title="Zeile entfernen">×</button>
      `;
      row.querySelector('[data-name]').value = name;
      syncHexInputs(row, color);
      row.querySelector('button').addEventListener('click', () => row.remove());
      el('name-colors').appendChild(row);
    }

    function renderFallbackRows(colors = state.fallbackColors) {
      const container = el('fallback-colors');
      container.innerHTML = '';
      const list = colors.length ? colors : ['#F4CCCC'];
      list.forEach((color) => addFallbackRow(normalizeHex(color) || '#F4CCCC'));
    }

    function addFallbackRow(color = '#F4CCCC') {
      const row = document.createElement('div');
      row.className = 'row fallback-row';
      row.innerHTML = `
        <input data-color type="color">
        <input data-hex type="text" inputmode="text" placeholder="#F4CCCC">
        <button class="icon-button danger" type="button" title="Farbe entfernen">×</button>
      `;
      syncHexInputs(row, color);
      row.querySelector('button').addEventListener('click', () => row.remove());
      el('fallback-colors').appendChild(row);
    }

    function renderPresetSelect() {
      const select = el('preset-select');
      select.innerHTML = '<option value="">Aktuelle Einstellungen</option>';
      state.presets.forEach((preset) => {
        const option = document.createElement('option');
        option.value = preset.id;
        option.textContent = preset.name;
        select.appendChild(option);
      });
      select.value = state.selectedPresetId;
    }

    function applyConfig(config) {
      state.nameColors = {...(config.name_colors || {})};
      state.fallbackColors = [...(config.fallback_colors || ['#F4CCCC'])];
      renderNameRows();
      renderFallbackRows();
    }

    async function loadPresets() {
      const response = await fetch('/api/presets');
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Presets konnten nicht geladen werden.');
      state.presets = data.presets || [];
      renderPresetSelect();
    }

    async function savePreset() {
      const config = configFromUI();
      const name = el('preset-name').value.trim();
      if (!name) throw new Error('Bitte einen Preset-Namen eingeben.');
      const response = await fetch('/api/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: state.selectedPresetId || undefined,
          name,
          ...config
        })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Preset konnte nicht gespeichert werden.');
      state.selectedPresetId = data.preset.id;
      await loadPresets();
      setStatus('Preset gespeichert.', 'ok');
    }

    async function deletePreset() {
      if (!state.selectedPresetId) throw new Error('Kein Preset ausgewählt.');
      const response = await fetch(`/api/presets/${encodeURIComponent(state.selectedPresetId)}`, { method: 'DELETE' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Preset konnte nicht gelöscht werden.');
      state.selectedPresetId = '';
      el('preset-name').value = '';
      await loadPresets();
      setStatus('Preset gelöscht.', 'ok');
    }

    async function processFile() {
      const file = el('docx-file').files[0];
      if (!file) throw new Error('Bitte eine DOCX-Datei auswählen.');
      const formData = new FormData();
      formData.append('file', file);
      formData.append('config', JSON.stringify(configFromUI()));
      setStatus('PDF wird erstellt ...');
      const response = await fetch('/api/process', { method: 'POST', body: formData });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || 'PDF konnte nicht erstellt werden.');
      }
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="([^"]+)"/);
      const filename = match ? match[1] : file.name.replace(/\.docx$/i, '.colored.pdf');
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus('PDF bereit.', 'ok');
    }

    function run(action) {
      Promise.resolve()
        .then(action)
        .catch((error) => setStatus(error.message || String(error), 'error'));
    }

    el('add-name').addEventListener('click', () => addNameRow());
    el('add-fallback').addEventListener('click', () => addFallbackRow());
    el('new-preset').addEventListener('click', () => {
      state.selectedPresetId = '';
      el('preset-select').value = '';
      el('preset-name').value = '';
      applyConfig(defaultConfig);
      setStatus('Neues Preset.');
    });
    el('save-preset').addEventListener('click', () => run(savePreset));
    el('delete-preset').addEventListener('click', () => run(deletePreset));
    el('process').addEventListener('click', () => run(processFile));
    el('preset-select').addEventListener('change', (event) => {
      state.selectedPresetId = event.target.value;
      const preset = state.presets.find((item) => item.id === state.selectedPresetId);
      if (preset) {
        el('preset-name').value = preset.name;
        applyConfig(preset);
      }
    });

    applyConfig(defaultConfig);
    run(loadPresets);
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Holy-Colours-Web-App starten.")
    parser.add_argument("--host", default=os.environ.get("HOLY_COLOURS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HOLY_COLOURS_PORT", "8000")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mimetypes.add_type("application/pdf", ".pdf")
    server = ThreadingHTTPServer((args.host, args.port), HolyColoursHandler)
    print(f"Holy Colours läuft unter http://{args.host}:{args.port}")
    print("Zum Beenden Ctrl+C drücken.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHoly Colours wird beendet.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
