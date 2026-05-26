from __future__ import annotations

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
    def test_session_cookie_accepts_matching_secret(self) -> None:
        cookie = web_app.create_session_cookie("admin", "secret")

        self.assertTrue(web_app.is_valid_session(cookie, "admin", "secret"))

    def test_session_cookie_rejects_missing_or_wrong_secret(self) -> None:
        cookie = web_app.create_session_cookie("admin", "secret")

        self.assertFalse(web_app.is_valid_session(None, "admin", "secret"))
        self.assertFalse(web_app.is_valid_session("holy_colours_session=not-base64", "admin", "secret"))
        self.assertFalse(web_app.is_valid_session(cookie, "other", "secret"))
        self.assertFalse(web_app.is_valid_session(cookie, "admin", "changed"))

    def test_register_authenticate_save_update_and_delete_user_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            presets_path = Path(tmp_dir) / "presets.json"
            user = web_app.register_user(
                {"username": "Linus", "password": "super-secret"},
                presets_path,
            )
            user_key = str(user["username_key"])

            authenticated = web_app.authenticate_user(
                "linus",
                "super-secret",
                presets_path,
            )

            self.assertEqual(user["id"], authenticated["id"])

            created = web_app.save_preset(
                {
                    "name": "Folge 85",
                    "name_colors": {"LEON": "#ffd966"},
                    "fallback_colors": ["#f4cccc"],
                },
                user_key,
                presets_path,
            )

            self.assertEqual("Folge 85", created["name"])
            self.assertEqual({"LEON": "#FFD966"}, created["name_colors"])
            self.assertEqual([created], web_app.read_presets(user_key, presets_path))

            updated = web_app.save_preset(
                {
                    "id": created["id"],
                    "name": "Folge 85 final",
                    "name_colors": {"ERZAEHLER": "#9fc5e8"},
                    "fallback_colors": ["#d9ead3"],
                },
                user_key,
                presets_path,
            )

            self.assertEqual(created["id"], updated["id"])
            self.assertEqual("Folge 85 final", updated["name"])
            self.assertEqual([updated], web_app.read_presets(user_key, presets_path))

            web_app.delete_preset(str(created["id"]), user_key, presets_path)
            self.assertEqual([], web_app.read_presets(user_key, presets_path))

    def test_presets_are_isolated_between_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            presets_path = Path(tmp_dir) / "presets.json"
            anna = web_app.register_user(
                {"username": "anna", "password": "super-secret"},
                presets_path,
            )
            bob = web_app.register_user(
                {"username": "bob", "password": "another-secret"},
                presets_path,
            )

            created = web_app.save_preset(
                {
                    "name": "Annas Folge",
                    "name_colors": {"ANNA": "#FFD966"},
                    "fallback_colors": ["#F4CCCC"],
                },
                str(anna["username_key"]),
                presets_path,
            )

            self.assertEqual([created], web_app.read_presets(str(anna["username_key"]), presets_path))
            self.assertEqual([], web_app.read_presets(str(bob["username_key"]), presets_path))

    def test_first_user_imports_legacy_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            presets_path = Path(tmp_dir) / "presets.json"
            presets_path.write_text(
                '{"presets":[{"id":"legacy","name":"Alt","name_colors":{"LEON":"#FFD966"},"fallback_colors":["#F4CCCC"],"updated_at":"old"}]}',
                encoding="utf-8",
            )

            first = web_app.register_user(
                {"username": "first", "password": "super-secret"},
                presets_path,
            )
            second = web_app.register_user(
                {"username": "second", "password": "another-secret"},
                presets_path,
            )

            self.assertEqual(
                ["Alt"],
                [preset["name"] for preset in web_app.read_presets(str(first["username_key"]), presets_path)],
            )
            self.assertEqual([], web_app.read_presets(str(second["username_key"]), presets_path))

    def test_imports_legacy_backup_presets_into_current_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            presets_path = Path(tmp_dir) / "presets.json"
            user = web_app.register_user(
                {"username": "linus", "password": "super-secret"},
                presets_path,
            )
            user_key = str(user["username_key"])

            result = web_app.import_presets(
                {
                    "presets": [
                        {
                            "id": "legacy",
                            "name": "Folge 85",
                            "name_colors": {"LEON": "#FFD966"},
                            "fallback_colors": ["#F4CCCC"],
                            "updated_at": "old",
                        }
                    ]
                },
                user_key,
                presets_path,
            )

            self.assertEqual(1, result["imported"])
            self.assertEqual(1, result["created"])
            self.assertEqual(0, result["updated"])
            self.assertEqual(
                ["Folge 85"],
                [preset["name"] for preset in web_app.read_presets(user_key, presets_path)],
            )

    def test_import_updates_existing_preset_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            presets_path = Path(tmp_dir) / "presets.json"
            user = web_app.register_user(
                {"username": "linus", "password": "super-secret"},
                presets_path,
            )
            user_key = str(user["username_key"])
            web_app.save_preset(
                {
                    "id": "same",
                    "name": "Alt",
                    "name_colors": {"LEON": "#FFD966"},
                    "fallback_colors": ["#F4CCCC"],
                },
                user_key,
                presets_path,
            )

            result = web_app.import_presets(
                [
                    {
                        "id": "same",
                        "name": "Neu",
                        "name_colors": {"ERZAEHLER": "#9FC5E8"},
                        "fallback_colors": ["#D9EAD3"],
                    }
                ],
                user_key,
                presets_path,
            )

            self.assertEqual(0, result["created"])
            self.assertEqual(1, result["updated"])
            presets = web_app.read_presets(user_key, presets_path)
            self.assertEqual(1, len(presets))
            self.assertEqual("Neu", presets[0]["name"])
            self.assertEqual({"ERZAEHLER": "#9FC5E8"}, presets[0]["name_colors"])

    def test_imports_matching_user_from_user_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            presets_path = Path(tmp_dir) / "presets.json"
            user = web_app.register_user(
                {"username": "linus", "password": "super-secret"},
                presets_path,
            )
            user_key = str(user["username_key"])

            result = web_app.import_presets(
                {
                    "version": 2,
                    "users": [
                        {
                            "username": "linus",
                            "username_key": "linus",
                            "presets": [
                                {
                                    "id": "mine",
                                    "name": "Meine Folge",
                                    "name_colors": {"LEON": "#FFD966"},
                                    "fallback_colors": ["#F4CCCC"],
                                }
                            ],
                        },
                        {
                            "username": "other",
                            "username_key": "other",
                            "presets": [
                                {
                                    "id": "other",
                                    "name": "Andere Folge",
                                    "name_colors": {"BOB": "#D9EAD3"},
                                    "fallback_colors": ["#F4CCCC"],
                                }
                            ],
                        },
                    ],
                },
                user_key,
                presets_path,
            )

            self.assertEqual(1, result["imported"])
            self.assertEqual(
                ["Meine Folge"],
                [preset["name"] for preset in web_app.read_presets(user_key, presets_path)],
            )

    def test_exports_current_user_presets_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            presets_path = Path(tmp_dir) / "presets.json"
            user = web_app.register_user(
                {"username": "Linus", "password": "super-secret"},
                presets_path,
            )
            user_key = str(user["username_key"])
            web_app.save_preset(
                {
                    "id": "share-me",
                    "name": "Teilbar",
                    "name_colors": {"LEON": "#FFD966"},
                    "fallback_colors": ["#F4CCCC"],
                },
                user_key,
                presets_path,
            )

            exported = web_app.export_presets(user_key, str(user["username"]), presets_path)
            exported_json = web_app.json_bytes(exported).decode("utf-8")

            self.assertEqual("holy-colours-presets", exported["format"])
            self.assertEqual("Linus", exported["username"])
            self.assertEqual(["Teilbar"], [preset["name"] for preset in exported["presets"]])
            self.assertNotIn("password_hash", exported_json)
            self.assertNotIn("password_salt", exported_json)
            self.assertNotIn("session_secret", exported_json)

    def test_export_filename_uses_safe_username(self) -> None:
        self.assertEqual(
            "holy-colours-presets-Team_User.json",
            web_app.preset_export_filename("Team User"),
        )

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
