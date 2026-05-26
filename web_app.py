#!/usr/bin/env python3
"""Kleine lokale Web-App zum Einfärben von DOCX-Sprechernamen und PDF-Export."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
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
PRESETS_PATH = Path(os.environ.get("HOLY_COLOURS_PRESETS_PATH", ROOT_DIR / "presets.json"))
COLORS_EXAMPLE_PATH = ROOT_DIR / "colors.example.json"
DEFAULT_FALLBACK_COLORS = ["#F4CCCC", "#D9EAD3", "#CFE2F3", "#FFF2CC"]
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,40}$")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SESSION_COOKIE_NAME = "holy_colours_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
PASSWORD_MIN_LENGTH = 8
PBKDF2_ITERATIONS = 240_000

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


def session_secret_bytes(secret_value: str | bytes) -> bytes:
    if isinstance(secret_value, bytes):
        return secret_value
    return secret_value.encode("utf-8")


def sign_session(username_key: str, issued_at: int, secret_value: str | bytes) -> str:
    payload = f"{username_key}:{issued_at}"
    signature = hmac.new(
        session_secret_bytes(secret_value),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    token = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(token).decode("ascii")


def create_session_cookie(username_key: str, secret_value: str | bytes) -> str:
    token = sign_session(username_key, int(time.time()), secret_value)
    return (
        f"{SESSION_COOKIE_NAME}={token}; Max-Age={SESSION_MAX_AGE_SECONDS}; "
        "Path=/; HttpOnly; SameSite=Lax"
    )


def clear_session_cookie() -> str:
    return f"{SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"


def normalize_username(value: object) -> str:
    username = str(value or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise WebAppError(
            "Benutzername muss 2 bis 40 Zeichen lang sein und darf nur Buchstaben, Zahlen, Punkt, Unterstrich oder Bindestrich enthalten."
        )
    return username


def username_key(username: str) -> str:
    return username.casefold()


def validate_password(value: object) -> str:
    password = str(value or "")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise WebAppError(f"Passwort muss mindestens {PASSWORD_MIN_LENGTH} Zeichen lang sein.")
    return password


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS,
    ).hex()


def build_password_record(password: str) -> tuple[str, str]:
    salt_hex = secrets.token_hex(16)
    return salt_hex, hash_password(password, salt_hex)


def verify_password(password: str, user: dict[str, object]) -> bool:
    salt_hex = str(user.get("password_salt") or "")
    expected_hash = str(user.get("password_hash") or "")
    if not salt_hex or not expected_hash:
        return False
    try:
        actual_hash = hash_password(password, salt_hex)
    except ValueError:
        return False
    return hmac.compare_digest(actual_hash, expected_hash)


def parse_cookie_header(cookie_header: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not cookie_header:
        return cookies
    for raw_part in cookie_header.split(";"):
        name, separator, value = raw_part.strip().partition("=")
        if separator and name:
            cookies[name] = value
    return cookies


def decode_session_cookie(cookie_header: str | None) -> tuple[str, int, str] | None:
    token = parse_cookie_header(cookie_header).get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None

    try:
        cookie_username_key, raw_issued_at, signature = decoded.split(":", 2)
    except ValueError:
        return None
    try:
        issued_at = int(raw_issued_at)
    except ValueError:
        return None
    return cookie_username_key, issued_at, signature


def is_valid_session(
    cookie_header: str | None,
    expected_username_key: str,
    secret_value: str | bytes,
) -> bool:
    decoded = decode_session_cookie(cookie_header)
    if decoded is None:
        return False

    cookie_username_key, issued_at, signature = decoded
    if cookie_username_key != expected_username_key:
        return False
    if issued_at < int(time.time()) - SESSION_MAX_AGE_SECONDS:
        return False

    expected_token = sign_session(cookie_username_key, issued_at, secret_value)
    token = parse_cookie_header(cookie_header).get(SESSION_COOKIE_NAME, "")
    return hmac.compare_digest(token, expected_token)


def validate_preset(raw_preset: object, context: str) -> dict[str, object] | None:
    if not isinstance(raw_preset, dict):
        return None

    preset_id = str(raw_preset.get("id") or "").strip()
    name = str(raw_preset.get("name") or "").strip()
    if not preset_id or not name:
        return None
    try:
        name_colors = validate_name_colors(raw_preset.get("name_colors", {}))
        fallback_colors = validate_fallback_colors(raw_preset.get("fallback_colors", []))
    except WebAppError as exc:
        raise WebAppError(f"Ungültiges Preset {context}: {exc.message}") from exc

    return {
        "id": preset_id,
        "name": name,
        "name_colors": name_colors,
        "fallback_colors": fallback_colors,
        "updated_at": str(raw_preset.get("updated_at") or ""),
    }


def validate_preset_list(value: object, context: str) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WebAppError(f"{context} muss eine Preset-Liste enthalten.")

    presets = []
    for index, raw_preset in enumerate(value):
        preset = validate_preset(raw_preset, f"{context} an Position {index}")
        if preset is not None:
            presets.append(preset)
    return sorted(presets, key=lambda item: str(item["name"]).casefold())


def normalize_import_preset(raw_preset: object, context: str) -> dict[str, object] | None:
    if not isinstance(raw_preset, dict):
        return None

    name = str(raw_preset.get("name") or "").strip()
    if not name:
        return None
    try:
        name_colors = validate_name_colors(raw_preset.get("name_colors", {}))
        fallback_colors = validate_fallback_colors(raw_preset.get("fallback_colors", []))
    except WebAppError as exc:
        raise WebAppError(f"Ungültiges Import-Preset {context}: {exc.message}") from exc

    return {
        "id": str(raw_preset.get("id") or "").strip() or uuid.uuid4().hex,
        "name": name,
        "name_colors": name_colors,
        "fallback_colors": fallback_colors,
        "updated_at": str(raw_preset.get("updated_at") or "") or now_iso(),
    }


def normalize_import_preset_list(value: object, context: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise WebAppError(f"{context} muss eine Preset-Liste enthalten.")

    presets = []
    for index, raw_preset in enumerate(value):
        preset = normalize_import_preset(raw_preset, f"{context} an Position {index}")
        if preset is not None:
            presets.append(preset)
    return presets


def extract_import_presets(
    payload: object,
    current_username_key: str,
) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return normalize_import_preset_list(payload, "Import")
    if not isinstance(payload, dict):
        raise WebAppError("Importdatei muss ein JSON-Objekt oder eine Preset-Liste sein.")

    if "presets" in payload:
        return normalize_import_preset_list(payload.get("presets"), "Import")

    if "users" in payload:
        users = payload.get("users")
        if not isinstance(users, list):
            raise WebAppError("Importdatei enthält keine gültige Benutzer-Liste.")

        matching_presets: list[dict[str, object]] = []
        all_presets: list[dict[str, object]] = []
        for index, raw_user in enumerate(users):
            if not isinstance(raw_user, dict):
                continue

            user_presets = normalize_import_preset_list(
                raw_user.get("presets", []),
                f"Benutzer an Position {index}",
            )
            all_presets.extend(user_presets)

            imported_key = str(raw_user.get("username_key") or "").strip()
            if not imported_key:
                imported_username = str(raw_user.get("username") or "").strip()
                imported_key = username_key(imported_username) if imported_username else ""
            if imported_key == current_username_key:
                matching_presets.extend(user_presets)

        return matching_presets or all_presets

    preset = normalize_import_preset(payload, "als Einzelpreset")
    if preset is not None:
        return [preset]

    raise WebAppError("Importdatei enthält keine Presets.")


def empty_data_store() -> dict[str, object]:
    return {
        "version": 2,
        "session_secret": secrets.token_hex(32),
        "users": [],
        "legacy_presets": [],
    }


def validate_user(raw_user: object, context: str) -> dict[str, object] | None:
    if not isinstance(raw_user, dict):
        return None

    username = str(raw_user.get("username") or "").strip()
    if not username:
        return None
    try:
        username = normalize_username(username)
    except WebAppError as exc:
        raise WebAppError(f"Ungültiger Benutzer {context}: {exc.message}") from exc

    user_id = str(raw_user.get("id") or "").strip() or uuid.uuid4().hex
    return {
        "id": user_id,
        "username": username,
        "username_key": str(raw_user.get("username_key") or username_key(username)),
        "password_salt": str(raw_user.get("password_salt") or ""),
        "password_hash": str(raw_user.get("password_hash") or ""),
        "created_at": str(raw_user.get("created_at") or ""),
        "presets": validate_preset_list(raw_user.get("presets", []), f"Benutzer {username}"),
    }


def read_data_store(path: Path = PRESETS_PATH) -> dict[str, object]:
    if not path.exists():
        return empty_data_store()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WebAppError(f"Die Preset-Datei enthält ungültiges JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise WebAppError("Die Preset-Datei muss ein JSON-Objekt enthalten.")

    session_secret = str(raw.get("session_secret") or "").strip()
    if not session_secret:
        session_secret = secrets.token_hex(32)

    users_raw = raw.get("users", [])
    if not isinstance(users_raw, list):
        raise WebAppError("Die Preset-Datei muss eine Benutzer-Liste enthalten.")

    users = []
    for index, raw_user in enumerate(users_raw):
        user = validate_user(raw_user, f"an Position {index}")
        if user is not None:
            users.append(user)

    return {
        "version": 2,
        "session_secret": session_secret,
        "users": users,
        "legacy_presets": validate_preset_list(raw.get("presets", []), "presets"),
    }


def write_data_store(store: dict[str, object], path: Path = PRESETS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        {
            "version": 2,
            "session_secret": store["session_secret"],
            "users": store["users"],
        },
        ensure_ascii=False,
        indent=2,
    )
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp_file:
        tmp_file.write(data)
        tmp_path = Path(tmp_file.name)
    tmp_path.replace(path)


def find_user(store: dict[str, object], key: str) -> dict[str, object] | None:
    for user in store["users"]:
        if isinstance(user, dict) and user.get("username_key") == key:
            return user
    return None


def public_user(user: dict[str, object]) -> dict[str, str]:
    return {
        "id": str(user["id"]),
        "username": str(user["username"]),
    }


def register_user(payload: object, path: Path = PRESETS_PATH) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise WebAppError("Registrierungsdaten müssen als Objekt gesendet werden.")

    username = normalize_username(payload.get("username"))
    password = validate_password(payload.get("password"))
    store = read_data_store(path)
    key = username_key(username)
    if find_user(store, key) is not None:
        raise WebAppError("Benutzername ist bereits vergeben.", HTTPStatus.CONFLICT)

    salt_hex, password_hash = build_password_record(password)
    users = list(store["users"])
    user_presets = list(store["legacy_presets"]) if not users else []
    user = {
        "id": uuid.uuid4().hex,
        "username": username,
        "username_key": key,
        "password_salt": salt_hex,
        "password_hash": password_hash,
        "created_at": now_iso(),
        "presets": user_presets,
    }
    users.append(user)
    users.sort(key=lambda item: str(item["username"]).casefold())
    store["users"] = users
    store["legacy_presets"] = []
    write_data_store(store, path)
    return user


def authenticate_user(
    username_value: object,
    password_value: object,
    path: Path = PRESETS_PATH,
) -> dict[str, object]:
    username = normalize_username(username_value)
    password = str(password_value or "")
    store = read_data_store(path)
    user = find_user(store, username_key(username))
    if user is None or not verify_password(password, user):
        raise WebAppError("Benutzername oder Passwort ist falsch.", HTTPStatus.UNAUTHORIZED)
    return user


def user_from_session(
    cookie_header: str | None,
    path: Path = PRESETS_PATH,
) -> dict[str, object] | None:
    decoded = decode_session_cookie(cookie_header)
    if decoded is None:
        return None

    cookie_username_key, issued_at, _signature = decoded
    store = read_data_store(path)
    user = find_user(store, cookie_username_key)
    if user is None:
        return None
    if not is_valid_session(cookie_header, cookie_username_key, str(store["session_secret"])):
        return None
    if issued_at < int(time.time()) - SESSION_MAX_AGE_SECONDS:
        return None
    return user


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


def read_presets(username_key_value: str, path: Path = PRESETS_PATH) -> list[dict[str, object]]:
    store = read_data_store(path)
    user = find_user(store, username_key_value)
    if user is None:
        raise WebAppError("Benutzer nicht gefunden.", HTTPStatus.NOT_FOUND)
    return list(user["presets"])


def save_preset(
    payload: object,
    username_key_value: str,
    path: Path = PRESETS_PATH,
) -> dict[str, object]:
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

    store = read_data_store(path)
    user = find_user(store, username_key_value)
    if user is None:
        raise WebAppError("Benutzer nicht gefunden.", HTTPStatus.NOT_FOUND)

    presets = list(user["presets"])
    replaced = False
    for index, existing in enumerate(presets):
        if existing["id"] == preset_id:
            presets[index] = preset
            replaced = True
            break
    if not replaced:
        presets.append(preset)

    presets.sort(key=lambda item: str(item["name"]).casefold())
    user["presets"] = presets
    write_data_store(store, path)
    return preset


def delete_preset(
    preset_id: str,
    username_key_value: str,
    path: Path = PRESETS_PATH,
) -> None:
    preset_id = preset_id.strip()
    if not preset_id:
        raise WebAppError("Eine Preset-ID ist erforderlich.")

    store = read_data_store(path)
    user = find_user(store, username_key_value)
    if user is None:
        raise WebAppError("Benutzer nicht gefunden.", HTTPStatus.NOT_FOUND)

    presets = list(user["presets"])
    remaining = [preset for preset in presets if preset["id"] != preset_id]
    if len(remaining) == len(presets):
        raise WebAppError("Preset nicht gefunden.", HTTPStatus.NOT_FOUND)
    user["presets"] = remaining
    write_data_store(store, path)


def import_presets(
    payload: object,
    username_key_value: str,
    path: Path = PRESETS_PATH,
) -> dict[str, object]:
    imported_presets = extract_import_presets(payload, username_key_value)
    if not imported_presets:
        raise WebAppError("Importdatei enthält keine Presets.")

    store = read_data_store(path)
    user = find_user(store, username_key_value)
    if user is None:
        raise WebAppError("Benutzer nicht gefunden.", HTTPStatus.NOT_FOUND)

    presets = list(user["presets"])
    existing_by_id = {
        str(preset["id"]): index
        for index, preset in enumerate(presets)
    }
    created_count = 0
    updated_count = 0

    for preset in imported_presets:
        preset_id = str(preset["id"])
        if preset_id in existing_by_id:
            presets[existing_by_id[preset_id]] = preset
            updated_count += 1
            continue

        existing_by_id[preset_id] = len(presets)
        presets.append(preset)
        created_count += 1

    presets.sort(key=lambda item: str(item["name"]).casefold())
    user["presets"] = presets
    write_data_store(store, path)
    return {
        "imported": len(imported_presets),
        "created": created_count,
        "updated": updated_count,
        "presets": presets,
    }


def export_presets(
    username_key_value: str,
    username: str,
    path: Path = PRESETS_PATH,
) -> dict[str, object]:
    return {
        "format": "holy-colours-presets",
        "version": 1,
        "exported_at": now_iso(),
        "username": username,
        "presets": read_presets(username_key_value, path),
    }


def preset_export_filename(username: str) -> str:
    safe_username = re.sub(r"[^A-Za-z0-9_.-]+", "_", username).strip("._")
    safe_username = safe_username or "user"
    return f"holy-colours-presets-{safe_username}.json"


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

    profile_dir = output_dir / "libreoffice-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    command = [
        converter,
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
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
            if path == "/api/health":
                self.respond_json({"ok": True})
                return
            if path == "/api/session":
                self.respond_json(self.session_status())
                return
            if path == "/":
                self.respond_bytes(
                    build_index_html().encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if path.startswith("/static/"):
                self.serve_static(path)
                return
            current_user = self.require_auth()
            if current_user is None:
                return
            if path == "/api/presets":
                self.respond_json({"presets": read_presets(str(current_user["username_key"]))})
                return
            if path == "/api/presets/export":
                username = str(current_user["username"])
                payload = export_presets(
                    str(current_user["username_key"]),
                    username,
                )
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                filename = preset_export_filename(username)
                self.respond_bytes(
                    body,
                    "application/json; charset=utf-8",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    },
                )
                return
            self.respond_error("Nicht gefunden.", HTTPStatus.NOT_FOUND)
        except WebAppError as exc:
            self.respond_error(exc.message, exc.status)

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/login":
                self.login()
                return
            if path == "/api/register":
                self.register()
                return
            if path == "/api/logout":
                self.logout()
                return
            current_user = self.require_auth()
            if current_user is None:
                return
            if path == "/api/presets":
                preset = save_preset(parse_json_body(self), str(current_user["username_key"]))
                self.respond_json({"preset": preset})
                return
            if path == "/api/presets/import":
                result = import_presets(parse_json_body(self), str(current_user["username_key"]))
                self.respond_json(result)
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
            current_user = self.require_auth()
            if current_user is None:
                return
            prefix = "/api/presets/"
            path = self.path.split("?", 1)[0]
            if path.startswith(prefix):
                delete_preset(
                    unquote(path[len(prefix) :]),
                    str(current_user["username_key"]),
                )
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

    def session_status(self) -> dict[str, object]:
        current_user = self.current_user()
        if current_user is None:
            return {"authenticated": False}
        return {"authenticated": True, "user": public_user(current_user)}

    def login(self) -> None:
        payload = parse_json_body(self)
        if not isinstance(payload, dict):
            raise WebAppError("Login-Daten müssen als Objekt gesendet werden.")

        user = authenticate_user(payload.get("username"), payload.get("password"))
        store = read_data_store()

        self.respond_json(
            {"ok": True, "authenticated": True, "user": public_user(user)},
            extra_headers={
                "Set-Cookie": create_session_cookie(
                    str(user["username_key"]),
                    str(store["session_secret"]),
                )
            },
        )

    def register(self) -> None:
        user = register_user(parse_json_body(self))
        store = read_data_store()
        self.respond_json(
            {"ok": True, "authenticated": True, "user": public_user(user)},
            HTTPStatus.CREATED,
            extra_headers={
                "Set-Cookie": create_session_cookie(
                    str(user["username_key"]),
                    str(store["session_secret"]),
                )
            },
        )

    def logout(self) -> None:
        self.respond_json(
            {"ok": True, "authenticated": False},
            extra_headers={"Set-Cookie": clear_session_cookie()},
        )

    def current_user(self) -> dict[str, object] | None:
        if not hasattr(self, "_current_user"):
            self._current_user = user_from_session(self.headers.get("Cookie"))
        return self._current_user

    def require_auth(self) -> dict[str, object] | None:
        current_user = self.current_user()
        if current_user is not None:
            return current_user
        self.respond_auth_required()
        return None

    def respond_json(
        self,
        value: object,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.respond_bytes(
            json_bytes(value),
            "application/json; charset=utf-8",
            status,
            extra_headers,
        )

    def respond_auth_required(self) -> None:
        self.respond_bytes(
            json_bytes({"error": "Authentifizierung erforderlich."}),
            "application/json; charset=utf-8",
            HTTPStatus.UNAUTHORIZED,
        )

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
