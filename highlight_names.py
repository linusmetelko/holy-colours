#!/usr/bin/env python3
"""Highlight speaker names at the start of DOCX paragraphs."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("w", W_NS)

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_NAME_SCAN = 200
DOCUMENT_XML_PATH = "word/document.xml"


def qname(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


@dataclass(frozen=True)
class TextSlot:
    run: ET.Element
    text_element: ET.Element
    text: str


@dataclass(frozen=True)
class Match:
    speaker_name: str
    slots_to_wrap: list[tuple[TextSlot, int]]


class ConfigError(ValueError):
    """Raised when the JSON config is invalid."""


def load_config(config_path: Path) -> tuple[dict[str, str], list[str]]:
    try:
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in config file: {exc}") from exc

    if not isinstance(config_data, dict):
        raise ConfigError("Config root must be a JSON object.")

    name_colors_raw = config_data.get("name_colors", {})
    fallback_colors_raw = config_data.get("fallback_colors", [])

    if not isinstance(name_colors_raw, dict):
        raise ConfigError("'name_colors' must be an object mapping names to colors.")
    if not isinstance(fallback_colors_raw, list):
        raise ConfigError("'fallback_colors' must be a list of hex colors.")
    if not fallback_colors_raw:
        raise ConfigError("'fallback_colors' must contain at least one color.")

    name_colors: dict[str, str] = {}
    for raw_name, raw_color in name_colors_raw.items():
        if not isinstance(raw_name, str):
            raise ConfigError("All speaker names in 'name_colors' must be strings.")
        color = _normalize_color(raw_color, field=f"name_colors.{raw_name}")
        name_colors[raw_name.strip()] = color

    fallback_colors = [
        _normalize_color(color, field=f"fallback_colors[{index}]")
        for index, color in enumerate(fallback_colors_raw)
    ]
    return name_colors, fallback_colors


def _normalize_color(value: object, field: str) -> str:
    if not isinstance(value, str) or not HEX_COLOR_RE.fullmatch(value):
        raise ConfigError(f"{field} must be a hex color in the form #RRGGBB.")
    return value[1:].upper()


def output_path_for(input_path: Path, explicit_output: Path | None) -> Path:
    if explicit_output is not None:
        return explicit_output
    return input_path.with_name(f"{input_path.stem}.colored{input_path.suffix}")


def iter_text_slots(paragraph: ET.Element) -> Iterable[TextSlot]:
    for run in paragraph.iter():
        if run.tag != qname(W_NS, "r"):
            continue

        for child in list(run):
            if child.tag == qname(W_NS, "t") and child.text:
                yield TextSlot(run=run, text_element=child, text=child.text)


def is_valid_speaker_name(candidate: str) -> bool:
    if not candidate:
        return False

    has_alpha = False
    for character in candidate:
        if character.isalpha():
            has_alpha = True
            if character != character.upper():
                return False
            continue

        if character.isdigit() or character in " _-.'’()/&":
            continue
        return False

    return has_alpha


def _is_candidate_character(character: str) -> bool:
    return character.isalpha() or character.isdigit() or character in " _-.'’()/&"


def find_leading_match(paragraph: ET.Element) -> Match | None:
    slots = list(iter_text_slots(paragraph))
    if not slots:
        return None

    candidate_parts: list[str] = []
    collected_length = 0

    for slot in slots:
        for character in slot.text:
            if collected_length >= MAX_NAME_SCAN:
                return None
            candidate_parts.append(character)
            collected_length += 1

            if character == ":":
                full_match = "".join(candidate_parts)
                speaker_name = full_match[:-1]
                if not is_valid_speaker_name(speaker_name):
                    return None

                remaining = len(full_match)
                slots_to_wrap: list[tuple[TextSlot, int]] = []
                for prefix_slot in slots:
                    if remaining <= 0:
                        break
                    consumed = min(len(prefix_slot.text), remaining)
                    slots_to_wrap.append((prefix_slot, consumed))
                    remaining -= consumed

                if remaining != 0:
                    return None
                return Match(speaker_name=speaker_name, slots_to_wrap=slots_to_wrap)

            if not _is_candidate_character(character):
                return None

    return None


def paragraph_elements(root: ET.Element) -> Iterable[ET.Element]:
    return (element for element in root.iter() if element.tag == qname(W_NS, "p"))


def process_document_xml(
    document_xml: bytes,
    name_colors: dict[str, str],
    fallback_colors: list[str],
) -> tuple[bytes, dict[str, str], int]:
    root = ET.fromstring(document_xml)
    assigned_fallbacks: dict[str, str] = {}
    highlighted_count = 0

    for paragraph in paragraph_elements(root):
        match = find_leading_match(paragraph)
        if match is None:
            continue

        color = name_colors.get(match.speaker_name)
        if color is None:
            fallback_index = len(assigned_fallbacks) % len(fallback_colors)
            color = assigned_fallbacks.setdefault(
                match.speaker_name,
                fallback_colors[fallback_index],
            )

        wrap_match(paragraph, match, color)
        highlighted_count += 1

    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml_bytes, assigned_fallbacks, highlighted_count


def wrap_match(paragraph: ET.Element, match: Match, color: str) -> None:
    parent_map = {child: parent for parent in paragraph.iter() for child in parent}
    runs_to_update: dict[ET.Element, list[tuple[ET.Element, int]]] = {}

    for slot, consumed in match.slots_to_wrap:
        runs_to_update.setdefault(slot.run, []).append((slot.text_element, consumed))

    for run, consumed_parts in reversed(list(runs_to_update.items())):
        parent = parent_map.get(run)
        if parent is None:
            continue

        prefix_run = build_highlight_run(run, consumed_parts, color)
        if prefix_run is not None:
            insert_index = list(parent).index(run)
            parent.insert(insert_index, prefix_run)

        trim_original_run(run, consumed_parts)
        if run_is_empty(run):
            parent.remove(run)


def build_highlight_run(
    run: ET.Element,
    consumed_parts: list[tuple[ET.Element, int]],
    color: str,
) -> ET.Element | None:
    consumed_by_element = {element: consumed for element, consumed in consumed_parts}
    new_run = ET.Element(qname(W_NS, "r"))

    original_rpr = run.find(qname(W_NS, "rPr"))
    new_rpr = copy.deepcopy(original_rpr) if original_rpr is not None else ET.Element(qname(W_NS, "rPr"))
    apply_run_background(new_rpr, color)
    new_run.append(new_rpr)

    added_text = False
    for child in list(run):
        if child.tag == qname(W_NS, "rPr"):
            continue
        if child.tag != qname(W_NS, "t"):
            continue

        consumed = consumed_by_element.get(child, 0)
        if consumed <= 0:
            continue

        prefix_text = (child.text or "")[:consumed]
        if not prefix_text:
            continue

        new_text = copy.deepcopy(child)
        set_text_value(new_text, prefix_text)
        new_run.append(new_text)
        added_text = True

    return new_run if added_text else None


def apply_run_background(rpr: ET.Element, color: str) -> None:
    shading = rpr.find(qname(W_NS, "shd"))
    if shading is None:
        shading = ET.SubElement(rpr, qname(W_NS, "shd"))
    shading.set(qname(W_NS, "val"), "clear")
    shading.set(qname(W_NS, "color"), "auto")
    shading.set(qname(W_NS, "fill"), color)


def trim_original_run(run: ET.Element, consumed_parts: list[tuple[ET.Element, int]]) -> None:
    consumed_by_element = {element: consumed for element, consumed in consumed_parts}

    for child in list(run):
        if child.tag != qname(W_NS, "t"):
            continue

        consumed = consumed_by_element.get(child, 0)
        if consumed <= 0:
            continue

        suffix_text = (child.text or "")[consumed:]
        if suffix_text:
            set_text_value(child, suffix_text)
            continue

        run.remove(child)


def run_is_empty(run: ET.Element) -> bool:
    for child in list(run):
        if child.tag == qname(W_NS, "rPr"):
            continue
        if child.tag == qname(W_NS, "t") and not (child.text or ""):
            continue
        return False
    return True


def set_text_value(text_element: ET.Element, value: str) -> None:
    text_element.text = value
    preserve_key = qname(XML_NS, "space")
    if value.startswith(" ") or value.endswith(" "):
        text_element.set(preserve_key, "preserve")
    else:
        text_element.attrib.pop(preserve_key, None)


def process_docx(
    input_path: Path,
    output_path: Path,
    name_colors: dict[str, str],
    fallback_colors: list[str],
) -> tuple[dict[str, str], int]:
    if input_path.resolve() == output_path.resolve():
        raise ConfigError("Input and output path must be different.")

    with zipfile.ZipFile(input_path, "r") as source_zip:
        document_xml = source_zip.read(DOCUMENT_XML_PATH)
        updated_document, assigned_fallbacks, highlighted_count = process_document_xml(
            document_xml=document_xml,
            name_colors=name_colors,
            fallback_colors=fallback_colors,
        )

        with zipfile.ZipFile(output_path, "w") as target_zip:
            for info in source_zip.infolist():
                new_info = zipfile.ZipInfo(info.filename)
                new_info.date_time = info.date_time
                new_info.compress_type = info.compress_type
                new_info.comment = info.comment
                new_info.extra = info.extra
                new_info.create_system = info.create_system
                new_info.external_attr = info.external_attr
                new_info.internal_attr = info.internal_attr
                new_info.flag_bits = info.flag_bits
                if info.is_dir():
                    target_zip.writestr(new_info, b"")
                    continue

                data = updated_document if info.filename == DOCUMENT_XML_PATH else source_zip.read(info.filename)
                target_zip.writestr(new_info, data)

    return assigned_fallbacks, highlighted_count


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Highlight speaker names at the start of DOCX paragraphs."
    )
    parser.add_argument("input", type=Path, help="Path to the input .docx file")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="JSON config with 'name_colors' and 'fallback_colors'",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the output .docx file (default: <name>.colored.docx)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    input_path = args.input
    output_path = output_path_for(input_path, args.output)

    try:
        name_colors, fallback_colors = load_config(args.config)
        assigned_fallbacks, highlighted_count = process_docx(
            input_path=input_path,
            output_path=output_path,
            name_colors=name_colors,
            fallback_colors=fallback_colors,
        )
    except (ConfigError, FileNotFoundError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Created {output_path} with {highlighted_count} highlighted speaker lines.")
    if assigned_fallbacks:
        print("Fallback assignments for this run:")
        for speaker_name, color in sorted(assigned_fallbacks.items()):
            print(f"  {speaker_name}: #{color}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
