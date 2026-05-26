from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import highlight_names as hn

W_NS = hn.W_NS


def qname(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


class HighlightNamesTests(unittest.TestCase):
    def test_highlights_known_name_and_preserves_other_zip_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "input.docx"
            output_path = tmp_path / "output.docx"

            extra_files = {"docProps/core.xml": b"<core/>"}
            create_test_docx(
                input_path,
                paragraphs=["<w:p><w:r><w:t>ANNA: Hallo Welt</w:t></w:r></w:p>"],
                extra_files=extra_files,
            )
            original_bytes = input_path.read_bytes()

            assigned_fallbacks, highlighted_count = hn.process_docx(
                input_path=input_path,
                output_path=output_path,
                name_colors={"ANNA": "FFD966"},
                fallback_colors=["F4CCCC"],
            )

            self.assertEqual({}, assigned_fallbacks)
            self.assertEqual(1, highlighted_count)
            self.assertEqual(original_bytes, input_path.read_bytes())

            with zipfile.ZipFile(output_path, "r") as archive:
                self.assertEqual(b"<core/>", archive.read("docProps/core.xml"))
                document_root = ET.fromstring(archive.read("word/document.xml"))

            paragraph = document_root.find(f".//{qname(W_NS, 'p')}")
            self.assertIsNotNone(paragraph)
            runs = paragraph.findall(qname(W_NS, "r"))
            self.assertEqual("ANNA:", runs[0].find(qname(W_NS, "t")).text)
            self.assertEqual(" Hallo Welt", runs[1].find(qname(W_NS, "t")).text)
            self.assertEqual("FFD966", get_run_fill(runs[0]))

    def test_assigns_fallback_colors_consistently_for_unknown_names(self) -> None:
        document_xml = build_document_xml(
            [
                "<w:p><w:r><w:t>BOB: Hallo</w:t></w:r></w:p>",
                "<w:p><w:r><w:t>CARL: Tschüss</w:t></w:r></w:p>",
                "<w:p><w:r><w:t>BOB: Noch mal</w:t></w:r></w:p>",
            ]
        ).encode("utf-8")

        updated_document, assigned_fallbacks, highlighted_count = hn.process_document_xml(
            document_xml=document_xml,
            name_colors={},
            fallback_colors=["F4CCCC", "D9EAD3"],
        )

        self.assertEqual(3, highlighted_count)
        self.assertEqual({"BOB": "F4CCCC", "CARL": "D9EAD3"}, assigned_fallbacks)

        root = ET.fromstring(updated_document)
        bob_runs = [run for run in root.findall(f".//{qname(W_NS, 'r')}") if run.find(qname(W_NS, "t")) is not None and run.find(qname(W_NS, "t")).text == "BOB:"]
        self.assertEqual(2, len(bob_runs))
        self.assertEqual({"F4CCCC"}, {get_run_fill(run) for run in bob_runs})

    def test_preserves_existing_run_formatting_on_name_and_dialog_text(self) -> None:
        document_xml = build_document_xml(
            [
                (
                    "<w:p>"
                    '<w:r><w:rPr><w:b/></w:rPr><w:t>AN</w:t></w:r>'
                    '<w:r><w:rPr><w:b/></w:rPr><w:t>NA:</w:t></w:r>'
                    '<w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve"> Hallo</w:t></w:r>'
                    "</w:p>"
                )
            ]
        ).encode("utf-8")

        updated_document, _, highlighted_count = hn.process_document_xml(
            document_xml=document_xml,
            name_colors={"ANNA": "FFD966"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual(1, highlighted_count)
        root = ET.fromstring(updated_document)
        paragraph = root.find(f".//{qname(W_NS, 'p')}")
        runs = paragraph.findall(qname(W_NS, "r"))

        highlighted_texts = []
        for run in runs:
            text_element = run.find(qname(W_NS, "t"))
            if text_element is None:
                continue
            if get_run_fill(run):
                highlighted_texts.append(text_element.text)
                self.assertIsNotNone(run.find(f"{qname(W_NS, 'rPr')}/{qname(W_NS, 'b')}"))
            else:
                self.assertEqual(" Hallo", text_element.text)
                self.assertIsNotNone(run.find(f"{qname(W_NS, 'rPr')}/{qname(W_NS, 'i')}"))

        self.assertEqual(["AN", "NA:"], highlighted_texts)
        self.assertEqual("ANNA: Hallo", paragraph_text(paragraph))

    def test_preserves_direct_font_on_highlighted_name(self) -> None:
        document_xml = build_document_xml(
            [
                (
                    "<w:p><w:r>"
                    '<w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:cs="Courier New"/></w:rPr>'
                    "<w:t>ANNA: Hallo</w:t>"
                    "</w:r></w:p>"
                )
            ]
        ).encode("utf-8")

        updated_document, _, highlighted_count = hn.process_document_xml(
            document_xml=document_xml,
            name_colors={"ANNA": "FFD966"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual(1, highlighted_count)
        root = ET.fromstring(updated_document)
        highlighted_run = root.find(f".//{qname(W_NS, 'r')}")
        self.assertEqual("ANNA:", highlighted_run.find(qname(W_NS, "t")).text)
        rfonts = highlighted_run.find(f"{qname(W_NS, 'rPr')}/{qname(W_NS, 'rFonts')}")
        self.assertIsNotNone(rfonts)
        self.assertEqual("Courier New", rfonts.get(qname(W_NS, "ascii")))
        self.assertEqual("Courier New", rfonts.get(qname(W_NS, "hAnsi")))

    def test_highlights_multiple_speakers_with_separate_colors(self) -> None:
        updated_document, assigned_fallbacks, highlighted_count = hn.process_document_xml(
            document_xml=build_document_xml(
                ["<w:p><w:r><w:t>ANNA: / BOB: Hallo zusammen</w:t></w:r></w:p>"]
            ).encode("utf-8"),
            name_colors={"ANNA": "FFD966", "BOB": "CFE2F3"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual({}, assigned_fallbacks)
        self.assertEqual(1, highlighted_count)
        root = ET.fromstring(updated_document)
        paragraph = root.find(f".//{qname(W_NS, 'p')}")
        runs = [
            run
            for run in paragraph.findall(qname(W_NS, "r"))
            if run.find(qname(W_NS, "t")) is not None
        ]

        self.assertEqual(
            ["ANNA:", " / ", "BOB:", " Hallo zusammen"],
            [run.find(qname(W_NS, "t")).text for run in runs],
        )
        self.assertEqual("FFD966", get_run_fill(runs[0]))
        self.assertIsNone(get_run_fill(runs[1]))
        self.assertEqual("CFE2F3", get_run_fill(runs[2]))
        self.assertIsNone(get_run_fill(runs[3]))
        self.assertEqual("ANNA: / BOB: Hallo zusammen", paragraph_text(paragraph))

    def test_highlights_multiple_speaker_labels_separated_by_space(self) -> None:
        updated_document, _, highlighted_count = hn.process_document_xml(
            document_xml=build_document_xml(
                ["<w:p><w:r><w:t>ANNA: BOB: Hallo zusammen</w:t></w:r></w:p>"]
            ).encode("utf-8"),
            name_colors={"ANNA": "FFD966", "BOB": "CFE2F3"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual(1, highlighted_count)
        root = ET.fromstring(updated_document)
        paragraph = root.find(f".//{qname(W_NS, 'p')}")
        runs = [
            run
            for run in paragraph.findall(qname(W_NS, "r"))
            if run.find(qname(W_NS, "t")) is not None
        ]

        self.assertEqual(
            ["ANNA:", " ", "BOB:", " Hallo zusammen"],
            [run.find(qname(W_NS, "t")).text for run in runs],
        )
        self.assertEqual("FFD966", get_run_fill(runs[0]))
        self.assertIsNone(get_run_fill(runs[1]))
        self.assertEqual("CFE2F3", get_run_fill(runs[2]))
        self.assertIsNone(get_run_fill(runs[3]))

    def test_splits_combined_speaker_label_before_colon(self) -> None:
        updated_document, assigned_fallbacks, highlighted_count = hn.process_document_xml(
            document_xml=build_document_xml(
                ["<w:p><w:r><w:t>ANNA / BOB: Hallo zusammen</w:t></w:r></w:p>"]
            ).encode("utf-8"),
            name_colors={"ANNA": "FFD966", "BOB": "CFE2F3"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual({}, assigned_fallbacks)
        self.assertEqual(1, highlighted_count)
        root = ET.fromstring(updated_document)
        paragraph = root.find(f".//{qname(W_NS, 'p')}")
        runs = [
            run
            for run in paragraph.findall(qname(W_NS, "r"))
            if run.find(qname(W_NS, "t")) is not None
        ]

        self.assertEqual(
            ["ANNA", " / ", "BOB:", " Hallo zusammen"],
            [run.find(qname(W_NS, "t")).text for run in runs],
        )
        self.assertEqual("FFD966", get_run_fill(runs[0]))
        self.assertIsNone(get_run_fill(runs[1]))
        self.assertEqual("CFE2F3", get_run_fill(runs[2]))
        self.assertIsNone(get_run_fill(runs[3]))
        self.assertEqual("ANNA / BOB: Hallo zusammen", paragraph_text(paragraph))

    def test_does_not_highlight_retake_speaker_names(self) -> None:
        updated_document, assigned_fallbacks, highlighted_count = hn.process_document_xml(
            document_xml=build_document_xml(
                [
                    "<w:p><w:r><w:t>BEN-RETAKE: Noch einmal</w:t></w:r></w:p>",
                    "<w:p><w:r><w:t>BEN-Retake: Noch einmal</w:t></w:r></w:p>",
                ]
            ).encode("utf-8"),
            name_colors={"BEN-RETAKE": "FFD966", "BEN": "CFE2F3"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual({}, assigned_fallbacks)
        self.assertEqual(0, highlighted_count)
        root = ET.fromstring(updated_document)
        runs = root.findall(f".//{qname(W_NS, 'r')}")

        self.assertEqual(
            ["BEN-RETAKE: Noch einmal", "BEN-Retake: Noch einmal"],
            [run.find(qname(W_NS, "t")).text for run in runs],
        )
        self.assertEqual([None, None], [get_run_fill(run) for run in runs])

    def test_retakes_do_not_receive_fallback_colors_in_multi_speaker_lines(self) -> None:
        updated_document, assigned_fallbacks, highlighted_count = hn.process_document_xml(
            document_xml=build_document_xml(
                ["<w:p><w:r><w:t>ANNA: / BEN-RETAKE: Noch einmal</w:t></w:r></w:p>"]
            ).encode("utf-8"),
            name_colors={"ANNA": "FFD966"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual({}, assigned_fallbacks)
        self.assertEqual(1, highlighted_count)
        root = ET.fromstring(updated_document)
        paragraph = root.find(f".//{qname(W_NS, 'p')}")
        runs = [
            run
            for run in paragraph.findall(qname(W_NS, "r"))
            if run.find(qname(W_NS, "t")) is not None
        ]

        self.assertEqual(
            ["ANNA:", " / BEN-RETAKE: Noch einmal"],
            [run.find(qname(W_NS, "t")).text for run in runs],
        )
        self.assertEqual("FFD966", get_run_fill(runs[0]))
        self.assertIsNone(get_run_fill(runs[1]))

    def test_does_not_highlight_name_in_middle_of_paragraph(self) -> None:
        updated_document, _, highlighted_count = hn.process_document_xml(
            document_xml=build_document_xml(
                ["<w:p><w:r><w:t>Hallo ANNA: Nein</w:t></w:r></w:p>"]
            ).encode("utf-8"),
            name_colors={"ANNA": "FFD966"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual(0, highlighted_count)
        root = ET.fromstring(updated_document)
        paragraph = root.find(f".//{qname(W_NS, 'p')}")
        runs = paragraph.findall(qname(W_NS, "r"))
        self.assertEqual(1, len(runs))
        self.assertEqual("Hallo ANNA: Nein", runs[0].find(qname(W_NS, "t")).text)
        self.assertIsNone(get_run_fill(runs[0]))

    def test_does_not_treat_dialog_time_as_second_speaker(self) -> None:
        updated_document, assigned_fallbacks, highlighted_count = hn.process_document_xml(
            document_xml=build_document_xml(
                ["<w:p><w:r><w:t>ANNA: UM 12:00 geht es los</w:t></w:r></w:p>"]
            ).encode("utf-8"),
            name_colors={"ANNA": "FFD966"},
            fallback_colors=["F4CCCC"],
        )

        self.assertEqual({}, assigned_fallbacks)
        self.assertEqual(1, highlighted_count)
        root = ET.fromstring(updated_document)
        paragraph = root.find(f".//{qname(W_NS, 'p')}")
        runs = paragraph.findall(qname(W_NS, "r"))

        self.assertEqual(
            ["ANNA:", " UM 12:00 geht es los"],
            [run.find(qname(W_NS, "t")).text for run in runs],
        )
        self.assertEqual("FFD966", get_run_fill(runs[0]))
        self.assertIsNone(get_run_fill(runs[1]))


def get_run_fill(run: ET.Element) -> str | None:
    shading = run.find(f"{qname(W_NS, 'rPr')}/{qname(W_NS, 'shd')}")
    if shading is None:
        return None
    return shading.get(qname(W_NS, "fill"))


def paragraph_text(paragraph: ET.Element) -> str:
    return "".join(text.text or "" for text in paragraph.iter() if text.tag == qname(W_NS, "t"))


def create_test_docx(path: Path, paragraphs: list[str], extra_files: dict[str, bytes] | None = None) -> None:
    extra_files = extra_files or {}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", build_content_types_xml())
        archive.writestr("_rels/.rels", build_root_rels_xml())
        archive.writestr("word/document.xml", build_document_xml(paragraphs))
        archive.writestr("word/_rels/document.xml.rels", build_document_rels_xml())
        for filename, content in extra_files.items():
            archive.writestr(filename, content)


def build_document_xml(paragraphs: list[str]) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}">
  <w:body>
    {''.join(paragraphs)}
    <w:sectPr/>
  </w:body>
</w:document>
"""


def build_content_types_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""


def build_root_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""


def build_document_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""


if __name__ == "__main__":
    unittest.main()
