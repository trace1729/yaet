from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
SCRIPT_PATH = SRC_DIR / "run_book_pipeline.py"


def load_module():
    if not SCRIPT_PATH.exists():
        raise AssertionError("run_book_pipeline.py is missing")

    spec = importlib.util.spec_from_file_location("run_book_pipeline", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load run_book_pipeline.py")

    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SRC_DIR))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeBook:
    def __init__(self, title: list[tuple[str, dict]], creator: list[tuple[str, dict]]) -> None:
        self._metadata = {
            ("DC", "title"): title,
            ("DC", "creator"): creator,
        }

    def get_metadata(self, namespace: str, name: str):
        return self._metadata.get((namespace, name), [])


class BookPipelineTests(unittest.TestCase):
    def test_derive_output_paths_uses_input_stem_by_default(self):
        module = load_module()
        input_path = Path("The Paper Menagerie and Oth_ (Z-Library).epub")

        paths = module.derive_output_paths(input_path)

        self.assertEqual(
            paths.markdown_path,
            Path("output") / "The_Paper_Menagerie_and_Oth_(Z-Library)" / "The_Paper_Menagerie_and_Oth_(Z-Library).md",
        )
        self.assertEqual(
            paths.cache_path,
            Path("output")
            / "The_Paper_Menagerie_and_Oth_(Z-Library)"
            / "The_Paper_Menagerie_and_Oth_(Z-Library).translation_cache.json",
        )
        self.assertEqual(
            paths.bilingual_markdown_path,
            Path("output")
            / "The_Paper_Menagerie_and_Oth_(Z-Library)"
            / "The_Paper_Menagerie_and_Oth_(Z-Library).bilingual.md",
        )
        self.assertEqual(
            paths.bilingual_epub_path,
            Path("output")
            / "The_Paper_Menagerie_and_Oth_(Z-Library)"
            / "The_Paper_Menagerie_and_Oth_(Z-Library).bilingual.epub",
        )

    def test_read_epub_metadata_falls_back_when_fields_are_missing(self):
        module = load_module()

        with patch.object(module.epub, "read_epub", return_value=FakeBook([], [])):
            metadata = module.read_epub_metadata(Path("missing.epub"))

        self.assertEqual(metadata.title, "missing")
        self.assertEqual(metadata.author, "Unknown Author")
        self.assertEqual(metadata.bilingual_title, "missing (Bilingual)")

    def test_run_pipeline_calls_all_stages_in_order(self):
        module = load_module()
        events: list[str] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "sample.epub"
            input_path.write_bytes(b"epub")

            paths = module.derive_output_paths(input_path)

            def fake_convert_epub_to_markdown(source, output, asset_dir_name=None):
                self.assertEqual(source, input_path)
                self.assertEqual(output, paths.markdown_path)
                self.assertTrue(output.parent.exists())
                events.append("epub_to_markdown")
                output.write_text("[STATE CHANGE](index_split_007.html#filepos28997)\n\nSTATE CHANGE\n", encoding="utf-8")

            def fake_fix_special_toc_headings(markdown):
                self.assertIn("[STATE CHANGE](index_split_007.html#filepos28997)", markdown)
                events.append("fix_special_toc_headings")
                return "[STATE CHANGE](#STATE_CHANGE)\n\n## STATE_CHANGE\n"

            def fake_create_client(api_key):
                self.assertEqual(api_key, "secret")
                events.append("create_client")
                return object()

            def fake_run_translation_pipeline(**kwargs):
                self.assertEqual(kwargs["input_path"], paths.markdown_path)
                self.assertEqual(kwargs["output_path"], paths.bilingual_markdown_path)
                self.assertEqual(kwargs["cache_path"], paths.cache_path)
                self.assertEqual(kwargs["max_workers"], 16)
                self.assertTrue(kwargs["output_path"].parent.exists())
                self.assertEqual(
                    kwargs["input_path"].read_text(encoding="utf-8"),
                    "[STATE CHANGE](#STATE_CHANGE)\n\n## STATE_CHANGE\n",
                )
                events.append("translate")
                paths.bilingual_markdown_path.write_text(
                    "### Chapter One\n\n### 第一章\n\nBody.\n",
                    encoding="utf-8",
                )

            def fake_postprocess_markdown(markdown):
                self.assertIn("### Chapter One", markdown)
                events.append("postprocess")
                return "### Chapter One (第一章)\n\nBody.\n"

            def fake_write_epub(input_markdown_path, output_epub_path, title, author):
                events.append("markdown_to_epub")
                self.assertEqual(input_markdown_path, paths.bilingual_markdown_path)
                self.assertEqual(output_epub_path, paths.bilingual_epub_path)
                self.assertEqual(title, "Sample Book (Bilingual)")
                self.assertEqual(author, "Sample Author")
                self.assertEqual(
                    input_markdown_path.read_text(encoding="utf-8"),
                    "### Chapter One (第一章)\n\nBody.\n",
                )
                output_epub_path.write_bytes(b"epub")

            metadata = module.BookMetadata(
                title="Sample Book",
                author="Sample Author",
                bilingual_title="Sample Book (Bilingual)",
            )

            with patch.object(module, "convert_epub_to_markdown", side_effect=fake_convert_epub_to_markdown), patch.object(
                module, "fix_special_toc_headings", side_effect=fake_fix_special_toc_headings
            ), patch.object(
                module, "load_api_key", return_value="secret"
            ), patch.object(module, "create_client", side_effect=fake_create_client), patch.object(
                module, "run_translation_pipeline", side_effect=fake_run_translation_pipeline
            ), patch.object(
                module, "postprocess_markdown", side_effect=fake_postprocess_markdown
            ), patch.object(
                module, "write_epub", side_effect=fake_write_epub
            ), patch.object(
                module, "read_epub_metadata", return_value=metadata
            ):
                result = module.run_pipeline(input_path, max_workers=16)

        self.assertEqual(
            events,
            [
                "epub_to_markdown",
                "fix_special_toc_headings",
                "create_client",
                "translate",
                "postprocess",
                "markdown_to_epub",
            ],
        )
        self.assertEqual(result.paths, paths)
        self.assertEqual(result.metadata, metadata)
        self.assertEqual(result.paths.markdown_path.parent.name, "sample")
        self.assertEqual(result.paths.markdown_path.parent.parent.name, "output")

    def test_run_pipeline_uses_pdf_conversion_for_pdf_inputs(self):
        module = load_module()
        events: list[str] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "CBP4-TAGE-SC-L.pdf"
            input_path.write_bytes(b"%PDF-1.4\n")

            paths = module.derive_output_paths(input_path)

            def fake_convert_pdf_to_markdown(source, output, **kwargs):
                self.assertEqual(source, input_path)
                self.assertEqual(output, paths.markdown_path)
                events.append("pdf_to_markdown")
                output.write_text("# CBP4-TAGE-SC-L\n\nBody.\n", encoding="utf-8")

            def fake_create_client(api_key):
                self.assertEqual(api_key, "secret")
                events.append("create_client")
                return object()

            def fake_run_translation_pipeline(**kwargs):
                events.append("translate")
                paths.bilingual_markdown_path.write_text("# Title\n\n# 标题\n", encoding="utf-8")

            def fake_postprocess_markdown(markdown):
                self.assertIn("# Title", markdown)
                events.append("postprocess")
                return "# Title (标题)\n"

            def fake_write_epub(input_markdown_path, output_epub_path, title, author):
                events.append("markdown_to_epub")
                self.assertEqual(title, "CBP4-TAGE-SC-L (Bilingual)")
                self.assertEqual(author, "Unknown Author")
                output_epub_path.write_bytes(b"epub")

            with patch.object(module, "convert_pdf_to_markdown", side_effect=fake_convert_pdf_to_markdown), patch.object(
                module, "load_mineru_api_key", return_value="mineru-key"
            ), patch.object(
                module, "load_api_key", return_value="secret"
            ), patch.object(module, "create_client", side_effect=fake_create_client), patch.object(
                module, "run_translation_pipeline", side_effect=fake_run_translation_pipeline
            ), patch.object(
                module, "postprocess_markdown", side_effect=fake_postprocess_markdown
            ), patch.object(
                module, "write_epub", side_effect=fake_write_epub
            ):
                result = module.run_pipeline(input_path, max_workers=2)

        self.assertEqual(events, ["pdf_to_markdown", "create_client", "translate", "postprocess", "markdown_to_epub"])
        self.assertEqual(result.metadata.title, "CBP4-TAGE-SC-L")
        self.assertEqual(result.metadata.author, "Unknown Author")


if __name__ == "__main__":
    unittest.main()
