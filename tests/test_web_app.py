from __future__ import annotations

import base64
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

import web_app
from test_highlight_names import create_test_docx, qname

W_NS = web_app.highlight_names.W_NS


class WebAppTests(unittest.TestCase):
    def test_basic_auth_accepts_matching_credentials(self) -> None:
        header = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")

        self.assertTrue(web_app.is_authorized(header, ("admin", "secret")))

    def test_basic_auth_rejects_missing_or_wrong_credentials(self) -> None:
        wrong_password = "Basic " + base64.b64encode(b"admin:wrong").decode("ascii")

        self.assertFalse(web_app.is_authorized(None, ("admin", "secret")))
        self.assertFalse(web_app.is_authorized("Bearer token", ("admin", "secret")))
        self.assertFalse(web_app.is_authorized("Basic not-base64", ("admin", "secret")))
        self.assertFalse(web_app.is_authorized(wrong_password, ("admin", "secret")))

    def test_basic_auth_is_disabled_without_credentials(self) -> None:
        self.assertTrue(web_app.is_authorized(None, None))

    def test_save_update_and_delete_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            presets_path = Path(tmp_dir) / "presets.json"

            created = web_app.save_preset(
                {
                    "name": "Folge 85",
                    "name_colors": {"LEON": "#ffd966"},
                    "fallback_colors": ["#f4cccc"],
                },
                presets_path,
            )

            self.assertEqual("Folge 85", created["name"])
            self.assertEqual({"LEON": "#FFD966"}, created["name_colors"])
            self.assertEqual([created], web_app.read_presets(presets_path))

            updated = web_app.save_preset(
                {
                    "id": created["id"],
                    "name": "Folge 85 final",
                    "name_colors": {"ERZAEHLER": "#9fc5e8"},
                    "fallback_colors": ["#d9ead3"],
                },
                presets_path,
            )

            self.assertEqual(created["id"], updated["id"])
            self.assertEqual("Folge 85 final", updated["name"])
            self.assertEqual([updated], web_app.read_presets(presets_path))

            web_app.delete_preset(str(created["id"]), presets_path)
            self.assertEqual([], web_app.read_presets(presets_path))

    def test_rejects_invalid_hex_codes(self) -> None:
        with self.assertRaises(web_app.WebAppError) as error:
            web_app.validate_processing_config(
                {
                    "name_colors": {"LEON": "FFD966"},
                    "fallback_colors": ["#F4CCCC"],
                }
            )

        self.assertIn("Hex-Farbwert", error.exception.message)

    def test_missing_libreoffice_has_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_docx = Path(tmp_dir) / "input.docx"
            input_docx.write_bytes(b"not used")

            with self.assertRaises(web_app.WebAppError) as error:
                web_app.convert_docx_to_pdf(
                    input_docx,
                    Path(tmp_dir),
                    find_converter=lambda: None,
                )

        self.assertEqual(500, error.exception.status.value)
        self.assertIn("LibreOffice wurde nicht gefunden", error.exception.message)

    def test_process_docx_upload_highlights_and_cleans_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_path = Path(tmp_dir) / "input.docx"
            create_test_docx(
                input_path,
                paragraphs=["<w:p><w:r><w:t>LEON: Hallo</w:t></w:r></w:p>"],
            )
            captured_temp_dir: Path | None = None

            def fake_convert(input_docx: Path, output_dir: Path, **_: object) -> Path:
                nonlocal captured_temp_dir
                captured_temp_dir = output_dir
                with zipfile.ZipFile(input_docx, "r") as archive:
                    root = ET.fromstring(archive.read("word/document.xml"))
                highlighted_run = root.find(f".//{qname(W_NS, 'r')}")
                self.assertIsNotNone(highlighted_run)
                fill = highlighted_run.find(
                    f"{qname(W_NS, 'rPr')}/{qname(W_NS, 'shd')}"
                ).get(qname(W_NS, "fill"))
                self.assertEqual("FFD966", fill)

                pdf_path = output_dir / "colored.pdf"
                pdf_path.write_bytes(b"%PDF-1.4\nfake pdf\n")
                return pdf_path

            with mock.patch.object(web_app, "convert_docx_to_pdf", fake_convert):
                filename, pdf_bytes = web_app.process_docx_upload(
                    "script.docx",
                    input_path.read_bytes(),
                    {
                        "name_colors": {"LEON": "#FFD966"},
                        "fallback_colors": ["#F4CCCC"],
                    },
                    find_converter=lambda: "unused",
                )

            self.assertEqual("script.colored.pdf", filename)
            self.assertEqual(b"%PDF-1.4\nfake pdf\n", pdf_bytes)
            self.assertIsNotNone(captured_temp_dir)
            self.assertFalse(captured_temp_dir.exists())


if __name__ == "__main__":
    unittest.main()
