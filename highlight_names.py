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
UNCOLORED_NAME_NOTE_RE = re.compile(r"(?:^|[\s_-])RETAKE(?:$|[\s_-])", re.IGNORECASE)
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
class HighlightSpan:
    speaker_name: str
    start: int
    end: int


@dataclass(frozen=True)
class ResolvedHighlight:
    start: int
    end: int
    color: str


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


def should_skip_speaker_highlight(speaker_name: str) -> bool:
    return bool(UNCOLORED_NAME_NOTE_RE.search(speaker_name))


def _is_candidate_character(character: str) -> bool:
    return character.isalpha() or character.isdigit() or character in " _-.'’()/&,+;\\"


def split_speaker_candidate(
    candidate: str,
    start_offset: int,
    colon_offset: int,
) -> list[HighlightSpan]:
    candidate_spans: list[tuple[int, int, str]] = []
    part_start = 0
    separators = re.finditer(r"\s*(?:[/\\&,+;]|\bUND\b)\s*", candidate)

    for separator in separators:
        speaker_part = parse_speaker_part(candidate, part_start, separator.start())
        if speaker_part is None:
            return []
        candidate_spans.append(speaker_part)
        part_start = separator.end()

    speaker_part = parse_speaker_part(candidate, part_start, len(candidate))
    if speaker_part is None:
        return []
    candidate_spans.append(speaker_part)

    if not candidate_spans:
        return []

    spans: list[HighlightSpan] = []
    for index, (relative_start, relative_end, speaker_name) in enumerate(candidate_spans):
        highlight_end = (
            colon_offset + 1
            if index == len(candidate_spans) - 1
            else start_offset + relative_end
        )
        spans.append(
            HighlightSpan(
                speaker_name=speaker_name,
                start=start_offset + relative_start,
                end=highlight_end,
            )
        )
    return spans


def parse_speaker_part(
    candidate: str,
    start: int,
    end: int,
) -> tuple[int, int, str] | None:
    part = candidate[start:end]
    leading_spaces = len(part) - len(part.lstrip())
    trailing_spaces = len(part.rstrip())
    part_start = start + leading_spaces
    part_end = start + trailing_spaces
    speaker_name = candidate[part_start:part_end]
    if not is_valid_speaker_name(speaker_name):
        return None
    return part_start, part_end, speaker_name


def parse_speaker_label(text: str, start: int) -> tuple[list[HighlightSpan], int] | None:
    collected_length = 0

    for index in range(start, len(text)):
        if collected_length >= MAX_NAME_SCAN:
            return None

        character = text[index]
        collected_length += 1

        if character == ":":
            candidate = text[start:index]
            spans = split_speaker_candidate(candidate, start, index)
            if not spans:
                return None
            return spans, index + 1

        if not _is_candidate_character(character):
            return None

    return None


def skip_multi_speaker_separator(text: str, start: int) -> tuple[int, bool]:
    index = start
    saw_delimiter = False

    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character in "/\\&,+;":
            saw_delimiter = True
            index += 1
            continue
        break

    return index, saw_delimiter


def find_leading_highlight_spans(
    paragraph: ET.Element,
    known_speaker_names: set[str] | None = None,
) -> list[HighlightSpan]:
    slots = list(iter_text_slots(paragraph))
    text = "".join(slot.text for slot in slots)
    if not text:
        return []

    spans: list[HighlightSpan] = []
    index = 0

    while index < len(text):
        whitespace_only_separator = False
        if spans:
            next_index, saw_delimiter = skip_multi_speaker_separator(text, index)
            whitespace_only_separator = next_index > index and not saw_delimiter
            index = next_index

        parsed = parse_speaker_label(text, index)
        if parsed is None:
            break

        label_spans, index = parsed
        if whitespace_only_separator and known_speaker_names is not None:
            if any(span.speaker_name not in known_speaker_names for span in label_spans):
                break

        spans.extend(label_spans)

    return spans


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
        known_speaker_names = set(name_colors) | set(assigned_fallbacks)
        spans = find_leading_highlight_spans(paragraph, known_speaker_names)
        if not spans:
            continue

        highlights: list[ResolvedHighlight] = []
        for span in spans:
            if should_skip_speaker_highlight(span.speaker_name):
                continue

            color = name_colors.get(span.speaker_name)
            if color is None:
                fallback_index = len(assigned_fallbacks) % len(fallback_colors)
                color = assigned_fallbacks.setdefault(
                    span.speaker_name,
                    fallback_colors[fallback_index],
                )
            highlights.append(
                ResolvedHighlight(start=span.start, end=span.end, color=color)
            )

        if not highlights:
            continue

        apply_highlights(paragraph, highlights)
        highlighted_count += 1

    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml_bytes, assigned_fallbacks, highlighted_count


def apply_highlights(paragraph: ET.Element, highlights: list[ResolvedHighlight]) -> None:
    if not highlights:
        return

    sorted_highlights = sorted(highlights, key=lambda highlight: highlight.start)
    text_ranges: dict[ET.Element, tuple[int, int]] = {}
    affected_runs: set[ET.Element] = set()
    current_offset = 0

    for slot in iter_text_slots(paragraph):
        start = current_offset
        end = start + len(slot.text)
        text_ranges[slot.text_element] = (start, end)
        current_offset = end

        if any(highlight.start < end and start < highlight.end for highlight in sorted_highlights):
            affected_runs.add(slot.run)

    if not affected_runs:
        return

    parent_map = {child: parent for parent in paragraph.iter() for child in parent}
    runs = [
        element
        for element in paragraph.iter()
        if element.tag == qname(W_NS, "r") and element in affected_runs
    ]

    for run in reversed(runs):
        parent = parent_map.get(run)
        if parent is None:
            continue

        replacement_runs = build_replacement_runs(
            run=run,
            text_ranges=text_ranges,
            highlights=sorted_highlights,
        )
        insert_index = list(parent).index(run)
        for replacement_run in replacement_runs:
            parent.insert(insert_index, replacement_run)
            insert_index += 1
        parent.remove(run)


def build_replacement_runs(
    run: ET.Element,
    text_ranges: dict[ET.Element, tuple[int, int]],
    highlights: list[ResolvedHighlight],
) -> list[ET.Element]:
    replacement_runs: list[ET.Element] = []

    for child in list(run):
        if child.tag == qname(W_NS, "rPr"):
            continue

        if child.tag != qname(W_NS, "t"):
            replacement_runs.append(build_run_with_child(run, child, None))
            continue

        child_range = text_ranges.get(child)
        if child_range is None:
            replacement_runs.append(build_run_with_child(run, child, None))
            continue

        child_start, child_end = child_range
        replacement_runs.extend(
            split_text_child_into_runs(
                run=run,
                text_element=child,
                child_start=child_start,
                child_end=child_end,
                highlights=highlights,
            )
        )

    return replacement_runs


def split_text_child_into_runs(
    run: ET.Element,
    text_element: ET.Element,
    child_start: int,
    child_end: int,
    highlights: list[ResolvedHighlight],
) -> list[ET.Element]:
    text = text_element.text or ""
    boundaries = {0, len(text)}

    for highlight in highlights:
        if highlight.end <= child_start or child_end <= highlight.start:
            continue
        boundaries.add(max(0, highlight.start - child_start))
        boundaries.add(min(len(text), highlight.end - child_start))

    replacement_runs: list[ET.Element] = []
    sorted_boundaries = sorted(boundaries)

    for start, end in zip(sorted_boundaries, sorted_boundaries[1:]):
        if start == end:
            continue

        global_start = child_start + start
        global_end = child_start + end
        color = highlight_color_for_range(global_start, global_end, highlights)
        replacement_runs.append(
            build_run_with_child(
                run,
                text_element,
                color,
                text_value=text[start:end],
            )
        )

    return replacement_runs


def highlight_color_for_range(
    start: int,
    end: int,
    highlights: list[ResolvedHighlight],
) -> str | None:
    for highlight in highlights:
        if highlight.start <= start and end <= highlight.end:
            return highlight.color
    return None


def build_run_with_child(
    run: ET.Element,
    child: ET.Element,
    color: str | None,
    *,
    text_value: str | None = None,
) -> ET.Element:
    new_run = ET.Element(run.tag, dict(run.attrib))
    original_rpr = run.find(qname(W_NS, "rPr"))
    if original_rpr is not None or color is not None:
        new_rpr = (
            copy.deepcopy(original_rpr)
            if original_rpr is not None
            else ET.Element(qname(W_NS, "rPr"))
        )
        if color is not None:
            apply_run_background(new_rpr, color)
        new_run.append(new_rpr)

    new_child = copy.deepcopy(child)
    if text_value is not None:
        set_text_value(new_child, text_value)
    new_run.append(new_child)
    return new_run


def apply_run_background(rpr: ET.Element, color: str) -> None:
    shading = rpr.find(qname(W_NS, "shd"))
    if shading is None:
        shading = ET.SubElement(rpr, qname(W_NS, "shd"))
    shading.set(qname(W_NS, "val"), "clear")
    shading.set(qname(W_NS, "color"), "auto")
    shading.set(qname(W_NS, "fill"), color)


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
