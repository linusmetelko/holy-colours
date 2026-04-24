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
STATIC_DIR = ROOT_DIR / "static"
PRESETS_PATH = ROOT_DIR / "presets.json"
COLORS_EXAMPLE_PATH = ROOT_DIR / "colors.example.json"
DEFAULT_FALLBACK_COLORS = ["#F4CCCC", "#D9EAD3", "#CFE2F3", "#FFF2CC"]
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

MIME_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def load_index_template() -> str:
    """Read the index.html template from the static directory."""
    template_path = STATIC_DIR / "index.html"
    return template_path.read_text(encoding="utf-8")


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
    return load_index_template().replace("__DEFAULT_CONFIG__", default_config_json)


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
            if path.startswith("/static/"):
                self.serve_static(path)
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

    def serve_static(self, url_path: str) -> None:
        """Serve a file from STATIC_DIR with directory-traversal protection."""
        relative = url_path.removeprefix("/static/")
        file_path = (STATIC_DIR / relative).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())):
            self.respond_error("Nicht erlaubt.", HTTPStatus.FORBIDDEN)
            return
        if not file_path.is_file():
            self.respond_error("Nicht gefunden.", HTTPStatus.NOT_FOUND)
            return
        suffix = file_path.suffix.lower()
        content_type = MIME_TYPES.get(suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.respond_bytes(body, content_type)

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
