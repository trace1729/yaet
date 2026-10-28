import json
import importlib.util
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import Mock, patch
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import convert_markdown_to_epub as markdown_to_epub_module
import translate_markdown_book as translate_book_module
from monitor_translation_progress import compute_progress, estimate_eta_seconds, render_progress_line
from convert_markdown_to_epub import (
    build_book_sections,
    build_epub_book,
    collect_image_resources,
    markdown_to_html,
    rewrite_markdown_image_links,
    write_epub,
)
from translate_markdown_book import (
    Block,
    TranslationCache,
    batch_block_indexes,
    block_cache_key,
    build_messages,
    create_client,
    load_api_key,
    parse_blocks,
    reconstruct_markdown,
    restore_translation_text,
    run_translation_pipeline,
    translate_batch,
)


class ParseAndReconstructTests(unittest.TestCase):
    def test_parse_blocks_handles_headings_paragraphs_and_code_fences(self):
        source = "# Title\n\nFirst paragraph.\n\n- item one\n- item two\n\n```python\nprint('hi')\n```\n\n> quoted text\n"

        blocks = parse_blocks(source)

        self.assertEqual(
            [block.kind for block in blocks],
            [
                "heading",
                "blank",
                "paragraph",
                "blank",
                "list",
                "blank",
                "code_fence",
                "blank",
                "blockquote",
            ],
        )
        self.assertTrue(blocks[0].translatable)
        self.assertTrue(blocks[4].translatable)
        self.assertFalse(blocks[6].translatable)

    def test_parse_blocks_free_markdown_translator_protects_markdown_and_preserves_front_matter(self):
        source = (
            "---\n"
            "title: Sample Doc\n"
            "description: Intro\n"
            "---\n"
            "\n"
            "# Title\n"
            "\n"
            "Paragraph with `code` and [a link](https://example.com).\n"
        )

        blocks = parse_blocks(source, parser_backend="free_markdown_translator")

        self.assertEqual(blocks[0].kind, "front_matter")
        self.assertFalse(blocks[0].translatable)
        self.assertTrue(blocks[2].translatable)
        self.assertEqual(blocks[2].text, "# Title")
        self.assertIn("{{MD_0}}", blocks[2].prompt_text or "")
        self.assertIn("{{CODE_0}}", blocks[4].prompt_text or "")
        self.assertIn("{{LINK_", blocks[4].prompt_text or "")

    def test_parse_blocks_protects_complete_markdown_constructs_with_default_backend(self):
        source = (
            "# Title\n"
            "\n"
            "See [the docs](https://example.com/docs) and ![diagram](assets/figure.png).\n"
            "\n"
            "Keep `inline_code()` unchanged.\n"
        )

        blocks = parse_blocks(source)
        prompt_texts = "\n".join(block.prompt_text or "" for block in blocks if block.translatable)

        self.assertNotIn("[the docs](https://example.com/docs)", prompt_texts)
        self.assertNotIn("![diagram](assets/figure.png)", prompt_texts)
        self.assertNotIn("`inline_code()`", prompt_texts)
        self.assertIn("See", prompt_texts)
        self.assertIn("Keep", prompt_texts)

        link_block = next(block for block in blocks if "See [the docs]" in block.text)
        restored = restore_translation_text(link_block, "参见 {{LINK_0}} 和 {{IMAGE_1}}。")
        self.assertEqual(restored, "参见 [the docs](https://example.com/docs) 和 ![diagram](assets/figure.png)。")

    def test_unix_markdown_fixture_skips_images_and_code_in_translation_payload(self):
        fixture = PROJECT_ROOT / "output" / "Specification_of_the_UNIX_Filing_System" / "Specification_of_the_UNIX_Filing_System.md"
        if not fixture.exists():
            # `output/` is gitignored, so this fixture only exists on machines that
            # already converted that book; skip instead of failing a clean checkout.
            self.skipTest(f"missing local fixture: {fixture}")
        source = fixture.read_text(encoding="utf-8")

        blocks = parse_blocks(source)
        prompt_payload = "\n".join(block.prompt_text or block.text for block in blocks if block.translatable)

        self.assertNotIn("![](Specification_of_the_UNIX_Filing_System_assets/", prompt_payload)
        self.assertNotIn("```lisp", prompt_payload)
        self.assertNotIn("(file sucoffset)(n) =", prompt_payload)
        self.assertIn("Specification of the UNIX Filing System", prompt_payload)

    def test_reconstruct_markdown_inserts_translation_after_original_in_bilingual_mode(self):
        blocks = [
            Block(kind="heading", text="# Title", translatable=True),
            Block(kind="blank", text="", translatable=False),
            Block(kind="paragraph", text="First paragraph.", translatable=True),
            Block(kind="blank", text="", translatable=False),
            Block(kind="code_fence", text="```\ncode\n```", translatable=False),
        ]

        output = reconstruct_markdown(blocks, {0: "# 标题", 2: "第一段。"}, output_mode="bilingual")

        self.assertEqual(output, "# Title\n\n# 标题\n\nFirst paragraph.\n\n第一段。\n\n```\ncode\n```")

    def test_reconstruct_markdown_outputs_only_translations_in_chinese_mode(self):
        blocks = [
            Block(kind="heading", text="# Title", translatable=True),
            Block(kind="blank", text="", translatable=False),
            Block(kind="paragraph", text="First paragraph.", translatable=True),
            Block(kind="blank", text="", translatable=False),
            Block(kind="code_fence", text="```\ncode\n```", translatable=False),
        ]

        output = reconstruct_markdown(blocks, {0: "# 标题", 2: "第一段。"}, output_mode="chinese")

        self.assertEqual(output, "# 标题\n\n第一段。\n\n```\ncode\n```")


class BatchAndCacheTests(unittest.TestCase):
    def test_build_batches_respects_character_limit(self):
        blocks = [
            Block(kind="paragraph", text="alpha", translatable=True),
            Block(kind="paragraph", text="beta", translatable=True),
            Block(kind="code_fence", text="```\npass\n```", translatable=False),
            Block(kind="paragraph", text="gamma", translatable=True),
        ]

        batches = batch_block_indexes(blocks, max_chars_per_chunk=8)

        self.assertEqual(batches, [[0], [1], [3]])

    def test_build_batches_skips_blocks_that_only_contain_protected_markdown(self):
        blocks = parse_blocks("![](assets/figure.png)\n\nCaption text.\n")

        batches = batch_block_indexes(blocks, max_chars_per_chunk=1000)

        self.assertEqual(batches, [[2]])
        self.assertFalse(blocks[0].translatable)

        output = reconstruct_markdown(blocks, {2: "图注。"}, output_mode="bilingual")
        self.assertEqual(output.count("![](assets/figure.png)"), 1)

    def test_translation_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "cache.json"
            cache = TranslationCache(cache_path)
            cache.set("k1", "译文")
            cache.save()

            loaded = TranslationCache(cache_path)
            self.assertEqual(loaded.get("k1"), "译文")
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8")), {"k1": "译文"})

    def test_block_cache_key_changes_when_namespace_changes(self):
        block = Block(kind="paragraph", text="alpha", translatable=True)

        default_key = block_cache_key(block, "deepseek-chat")
        custom_key = block_cache_key(block, "deepseek-chat", namespace="fmt")

        self.assertNotEqual(default_key, custom_key)


class ApiFlowTests(unittest.TestCase):
    def test_build_messages_include_style_allusion_and_glossary_rules(self):
        messages = build_messages(
            ["The Mechanical Turk was a famous hoax."],
            current_heading="Chapter 1",
            previous_context={"source": "Previous source", "translation": "上一段译文"},
        )

        system_message = messages[0]["content"]
        user_message = messages[1]["content"]

        self.assertIn("philosophical", system_message.lower())
        self.assertIn("historical allusions", system_message.lower())
        self.assertIn("brief parenthetical", system_message.lower())
        self.assertIn("academic", user_message.lower())
        self.assertIn("literary", user_message.lower())
        self.assertIn("terminology", user_message.lower())
        self.assertIn("agent -> 智能体", user_message)
        self.assertIn("gestalt -> 格式塔", user_message)
        self.assertIn("Chapter 1", user_message)
        self.assertIn("上一段译文", user_message)

    def test_translate_batch_uses_client_and_validates_item_count(self):
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content='{"translations": [{"id": 0, "text": "甲"}, {"id": 1, "text": "乙"}]}'))]
        )

        result = translate_batch(client, ["first", "second"], model="deepseek-chat")

        self.assertEqual(result, ["甲", "乙"])

    def test_translate_batch_accepts_top_level_translation_array(self):
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content='[{"id": 0, "text": "甲"}, {"id": 1, "text": "乙"}]'))]
        )

        result = translate_batch(client, ["first", "second"], model="deepseek-chat")

        self.assertEqual(result, ["甲", "乙"])

    def test_translate_batch_recovers_by_splitting_when_batch_count_mismatches(self):
        client = Mock()
        client.chat.completions.create.side_effect = [
            Mock(message=Mock()),
        ]

        client.chat.completions.create.side_effect = [
            Mock(choices=[Mock(message=Mock(content='{"translations": [{"id": 0, "text": "甲"}]}'))]),
            Mock(choices=[Mock(message=Mock(content='{"translations": [{"id": 0, "text": "甲"}]}'))]),
            Mock(choices=[Mock(message=Mock(content='{"translations": [{"id": 0, "text": "乙"}]}'))]),
        ]

        result = translate_batch(client, ["first", "second"], model="deepseek-chat")

        self.assertEqual(result, ["甲", "乙"])
        self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_create_client_sets_default_timeout(self):
        openai_module = Mock(OpenAI=Mock())
        with patch.dict("sys.modules", {"openai": openai_module}):
            create_client("secret")

        openai_module.OpenAI.assert_called_once_with(
            api_key="secret",
            base_url="https://api.deepseek.com",
            timeout=60.0,
        )


class PipelineTests(unittest.TestCase):
    def test_run_translation_pipeline_submits_missing_batches_with_configured_workers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_path = Path(tmp_dir) / "input.md"
            output_path = Path(tmp_dir) / "output.md"
            cache_path = Path(tmp_dir) / "cache.json"
            input_path.write_text("# T\n\na\n\n b\n\n c\n\n d\n", encoding="utf-8")

            client = Mock()

            def create_response(*_, **kwargs):
                payload = json.loads(kwargs["messages"][1]["content"])
                size = len(payload["blocks"])
                body = {"translations": [{"id": index, "text": f"译文{index}"} for index in range(size)]}
                return Mock(choices=[Mock(message=Mock(content=json.dumps(body, ensure_ascii=False)))])

            client.chat.completions.create.side_effect = create_response

            executor = Mock()
            futures = []

            def submit(fn, *args, **kwargs):
                future = Future()
                future.set_result(fn(*args, **kwargs))
                futures.append((fn, args, kwargs))
                return future

            executor.submit.side_effect = submit
            executor.__enter__ = Mock(return_value=executor)
            executor.__exit__ = Mock(return_value=False)

            run_translation_pipeline(
                input_path=input_path,
                output_path=output_path,
                client=client,
                model="deepseek-chat",
                max_chars_per_chunk=1,
                cache_path=cache_path,
                resume=False,
                max_workers=3,
                executor_factory=Mock(return_value=executor),
            )

            self.assertEqual(client.chat.completions.create.call_count, 5)
            self.assertEqual(len(futures), 5)

    def test_load_api_key_falls_back_to_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text('DEEPSEEK_API_KEY="dotenv-key"\n', encoding="utf-8")

            previous = os.environ.pop("DEEPSEEK_API_KEY", None)
            try:
                self.assertEqual(load_api_key(env_path), "dotenv-key")
            finally:
                if previous is not None:
                    os.environ["DEEPSEEK_API_KEY"] = previous

    def test_load_api_key_falls_back_to_repo_dotenv_when_cwd_differs(self):
        repo_env_path = PROJECT_ROOT / ".env"
        original_content = repo_env_path.read_text(encoding="utf-8") if repo_env_path.exists() else None

        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_cwd = Path.cwd()
            previous = os.environ.pop("DEEPSEEK_API_KEY", None)
            try:
                repo_env_path.write_text('DEEPSEEK_API_KEY="repo-dotenv-key"\n', encoding="utf-8")
                os.chdir(tmp_dir)
                self.assertEqual(load_api_key(), "repo-dotenv-key")
            finally:
                os.chdir(previous_cwd)
                if original_content is None:
                    repo_env_path.unlink(missing_ok=True)
                else:
                    repo_env_path.write_text(original_content, encoding="utf-8")
                if previous is not None:
                    os.environ["DEEPSEEK_API_KEY"] = previous

    def test_run_translation_pipeline_writes_bilingual_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_path = Path(tmp_dir) / "input.md"
            output_path = Path(tmp_dir) / "output.md"
            cache_path = Path(tmp_dir) / "cache.json"
            input_path.write_text("# Title\n\nHello world.\n", encoding="utf-8")

            client = Mock()
            client.chat.completions.create.return_value = Mock(
                choices=[Mock(message=Mock(content='{"translations": [{"id": 0, "text": "# 标题"}, {"id": 1, "text": "你好，世界。"}]}'))]
            )

            run_translation_pipeline(
                input_path=input_path,
                output_path=output_path,
                client=client,
                model="deepseek-chat",
                max_chars_per_chunk=10_000,
                cache_path=cache_path,
                resume=True,
                max_workers=1,
            )

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "# Title\n\n# 标题\n\nHello world.\n\n你好，世界。",
            )

    def test_run_translation_pipeline_writes_chinese_only_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_path = Path(tmp_dir) / "input.md"
            output_path = Path(tmp_dir) / "output.md"
            cache_path = Path(tmp_dir) / "cache.json"
            input_path.write_text("# Title\n\nHello world.\n", encoding="utf-8")

            client = Mock()
            client.chat.completions.create.return_value = Mock(
                choices=[Mock(message=Mock(content='{"translations": [{"id": 0, "text": "# 标题"}, {"id": 1, "text": "你好，世界。"}]}'))]
            )

            run_translation_pipeline(
                input_path=input_path,
                output_path=output_path,
                client=client,
                model="deepseek-chat",
                max_chars_per_chunk=10_000,
                cache_path=cache_path,
                resume=True,
                max_workers=1,
                output_mode="chinese",
            )

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "# 标题\n\n你好，世界。",
            )


class ProgressMonitorTests(unittest.TestCase):
    def test_compute_progress_counts_translatable_blocks_against_cache(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "input.md"
            cache_path = Path(tmp_dir) / "cache.json"
            source_path.write_text("# Title\n\nHello world.\n\n```\ncode\n```\n", encoding="utf-8")

            blocks = parse_blocks(source_path.read_text(encoding="utf-8"))
            cache = {
                block_cache_key(blocks[0], "deepseek-chat"): "# 标题",
            }
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

            progress = compute_progress(source_path, cache_path, model="deepseek-chat")

            self.assertEqual(progress.total_blocks, 2)
            self.assertEqual(progress.completed_blocks, 1)
            self.assertEqual(progress.remaining_blocks, 1)

    def test_compute_progress_honors_cache_namespace_and_parser_backend(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "input.md"
            cache_path = Path(tmp_dir) / "cache.json"
            source_path.write_text("# Title\n\nHello `code`.\n", encoding="utf-8")

            blocks = parse_blocks(source_path.read_text(encoding="utf-8"), parser_backend="free_markdown_translator")
            cache = {
                block_cache_key(blocks[0], "deepseek-chat", namespace="fmt"): "# 标题",
                block_cache_key(blocks[2], "deepseek-chat", namespace="fmt"): "你好 `code`。",
            }
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

            progress = compute_progress(
                source_path,
                cache_path,
                model="deepseek-chat",
                parser_backend="free_markdown_translator",
                cache_namespace="fmt",
            )

            self.assertEqual(progress.total_blocks, 2)
            self.assertEqual(progress.completed_blocks, 2)
            self.assertEqual(progress.remaining_blocks, 0)

    def test_render_progress_line_formats_percent_and_bar(self):
        line = render_progress_line(completed=25, total=100, width=10, eta_seconds=3661)

        self.assertIn("25.0%", line)
        self.assertIn("25/100", line)
        self.assertIn("██", line)
        self.assertIn("ETA=01:01:01", line)

    def test_estimate_eta_seconds_uses_recent_progress_rate(self):
        samples = [
            (0.0, 10),
            (10.0, 20),
            (20.0, 30),
        ]

        eta = estimate_eta_seconds(samples=samples, remaining_blocks=15)

        self.assertEqual(eta, 15)


class EpubExportTests(unittest.TestCase):
    def test_main_defaults_output_to_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "book.bilingual.md"
            input_path.write_text("# Title\n", encoding="utf-8")
            captured: dict[str, Path | str] = {}

            def fake_write_epub(input_value: Path, output_value: Path, title: str, author: str) -> None:
                captured["input"] = input_value
                captured["output"] = output_value
                captured["title"] = title
                captured["author"] = author

            with patch.object(markdown_to_epub_module, "write_epub", side_effect=fake_write_epub):
                result = markdown_to_epub_module.main([str(input_path)])

        self.assertEqual(result, 0)
        self.assertEqual(captured["input"], input_path)
        self.assertEqual(captured["output"], tmp_path / "output" / "book" / "book.bilingual.epub")
        self.assertEqual(captured["title"], "book")
        self.assertEqual(captured["author"], "Unknown Author")

    def test_translate_main_defaults_output_and_cache_to_book_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "My Book.md"
            input_path.write_text("# Title\n", encoding="utf-8")
            captured: dict[str, Path | str | int | bool] = {}

            def fake_run_translation_pipeline(**kwargs):
                captured["input_path"] = kwargs["input_path"]
                captured["output_path"] = kwargs["output_path"]
                captured["cache_path"] = kwargs["cache_path"]

            with patch.object(translate_book_module, "load_api_key", return_value="secret"), patch.object(
                translate_book_module, "create_client", return_value=object()
            ), patch.object(
                translate_book_module, "run_translation_pipeline", side_effect=fake_run_translation_pipeline
            ):
                result = translate_book_module.main([str(input_path)])

        self.assertEqual(result, 0)
        self.assertEqual(captured["input_path"], input_path)
        self.assertEqual(captured["output_path"], tmp_path / "output" / "My_Book" / "My_Book.bilingual.md")
        self.assertEqual(captured["cache_path"], tmp_path / "output" / "My_Book" / "My_Book.translation_cache.json")

    def test_write_epub_creates_missing_output_directory(self):
        if importlib.util.find_spec("ebooklib") is None:
            self.skipTest("ebooklib is not installed in this interpreter")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "book.bilingual.md"
            output_path = tmp_path / "output" / "book.bilingual.epub"
            input_path.write_text("# Title\n\nParagraph.\n", encoding="utf-8")

            write_epub(input_path, output_path, title="Book (Bilingual)", author="Author")

            self.assertTrue(output_path.exists())

    def test_write_epub_marks_first_image_as_cover(self):
        if importlib.util.find_spec("ebooklib") is None:
            self.skipTest("ebooklib is not installed in this interpreter")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "book.bilingual.md"
            output_path = tmp_path / "book.bilingual.epub"
            cover_path = tmp_path / "cover.jpg"
            cover_path.write_bytes(b"jpeg-bytes")
            input_path.write_text("# Title\n\n![cover](cover.jpg)\n\nParagraph.\n", encoding="utf-8")

            write_epub(input_path, output_path, title="Book (Bilingual)", author="Author")

            with zipfile.ZipFile(output_path) as archive:
                opf_name = next(name for name in archive.namelist() if name.endswith(".opf"))
                opf = archive.read(opf_name).decode("utf-8", errors="replace")

            self.assertIn('name="cover"', opf)
            self.assertIn('content="cover-img"', opf)

    def test_write_epub_prefers_cover_named_image_over_first_image(self):
        if importlib.util.find_spec("ebooklib") is None:
            self.skipTest("ebooklib is not installed in this interpreter")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "book.bilingual.md"
            output_path = tmp_path / "book.bilingual.epub"
            (tmp_path / "frontispiece.jpg").write_bytes(b"jpeg-front")
            (tmp_path / "cover-final.jpg").write_bytes(b"jpeg-cover")
            input_path.write_text(
                "# Title\n\n"
                "![front](frontispiece.jpg)\n\n"
                "![cover](cover-final.jpg)\n\n"
                "Paragraph.\n",
                encoding="utf-8",
            )

            write_epub(input_path, output_path, title="Book (Bilingual)", author="Author")

            with zipfile.ZipFile(output_path) as archive:
                opf_name = next(name for name in archive.namelist() if name.endswith(".opf"))
                opf = archive.read(opf_name).decode("utf-8", errors="replace")

            self.assertIn('name="cover"', opf)
            self.assertIn('href="images/Image00001.jpg" id="cover-img"', opf)

    def test_markdown_to_html_preserves_supported_inline_emphasis(self):
        markdown = (
            "# Title with **bold** and *italic*\n\n"
            "Paragraph with **bold**, *italic*, ***both***, __strong__, _emphasis_, and ___combo___.\n"
        )

        html = markdown_to_html(markdown)

        self.assertIn("<h1>Title with <strong>bold</strong> and <em>italic</em></h1>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn("<strong><em>both</em></strong>", html)
        self.assertIn("<strong>strong</strong>", html)
        self.assertIn("<em>emphasis</em>", html)
        self.assertIn("<strong><em>combo</em></strong>", html)

    def test_build_book_sections_splits_on_headings(self):
        markdown = "# Title\n\nIntro\n\n## Chapter 1\n\nBody\n\n## Chapter 2\n\nMore\n"

        sections = build_book_sections(markdown)

        self.assertEqual([section.title for section in sections], ["Title", "Chapter 1", "Chapter 2"])
        self.assertIn("Intro", sections[0].markdown)
        self.assertIn("Body", sections[1].markdown)

    def test_build_epub_book_returns_book_with_toc_entries(self):
        if importlib.util.find_spec("ebooklib") is None:
            self.skipTest("ebooklib is not installed in this interpreter")
        markdown = "# Title\n\nIntro\n\n## Chapter 1\n\nBody\n"

        book = build_epub_book(markdown, title="The Society of Mind (Bilingual)", author="Marvin Minsky")

        self.assertEqual(book.title, "The Society of Mind (Bilingual)")
        self.assertGreaterEqual(len(book.toc), 2)

    def test_collect_image_resources_downloads_remote_and_caches_it(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / ".epub_image_cache"
            markdown = "![image](https://example.com/a.jpg)"

            def fake_fetch(url: str):
                self.assertEqual(url, "https://example.com/a.jpg")
                return b"jpeg-bytes", "image/jpeg"

            resources = collect_image_resources(markdown, base_dir=Path(tmp_dir), cache_dir=cache_dir, fetch_remote=fake_fetch)

            self.assertEqual(len(resources), 1)
            self.assertTrue(resources[0].cache_path.exists())
            self.assertEqual(resources[0].media_type, "image/jpeg")

    def test_rewrite_markdown_image_links_points_to_epub_internal_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / ".epub_image_cache"
            markdown = "![remote](https://example.com/a.jpg)\n\n![local](cover.png)"
            (Path(tmp_dir) / "cover.png").write_bytes(b"png-bytes")

            def fake_fetch(_url: str):
                return b"jpeg-bytes", "image/jpeg"

            resources = collect_image_resources(markdown, base_dir=Path(tmp_dir), cache_dir=cache_dir, fetch_remote=fake_fetch)
            rewritten = rewrite_markdown_image_links(markdown, resources)

            self.assertIn("images/", rewritten)
            self.assertNotIn("https://example.com/a.jpg", rewritten)
            self.assertNotIn("cover.png)", rewritten)

    def test_collect_image_resources_supports_paths_with_parentheses(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / ".epub_image_cache"
            asset_dir = Path(tmp_dir) / "Book (Draft)_assets"
            asset_dir.mkdir()
            image_path = asset_dir / "cover.jpg"
            image_path.write_bytes(b"jpeg-bytes")
            markdown = "![cover](Book (Draft)_assets/cover.jpg)"

            resources = collect_image_resources(markdown, base_dir=Path(tmp_dir), cache_dir=cache_dir)
            rewritten = rewrite_markdown_image_links(markdown, resources)

            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0].source, "Book (Draft)_assets/cover.jpg")
            self.assertIn("images/", rewritten)
            self.assertNotIn("Book (Draft)_assets/cover.jpg", rewritten)


if __name__ == "__main__":
    unittest.main()
