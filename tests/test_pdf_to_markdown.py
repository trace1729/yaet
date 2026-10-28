from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
SCRIPT_PATH = SRC_DIR / "convert_pdf_to_markdown.py"


def load_module():
    if not SCRIPT_PATH.exists():
        raise AssertionError("convert_pdf_to_markdown.py is missing")

    spec = importlib.util.spec_from_file_location("convert_pdf_to_markdown", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load convert_pdf_to_markdown.py")

    module = importlib.util.module_from_spec(spec)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PdfToMarkdownTests(unittest.TestCase):
    def test_load_mineru_api_key_falls_back_to_dotenv(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text('MINERU_API_KEY="mineru-key"\n', encoding="utf-8")

            with patch.dict(module.os.environ, {}, clear=True):
                self.assertEqual(module.load_mineru_api_key(env_path), "mineru-key")

    def test_load_mineru_api_key_falls_back_to_repo_dotenv_when_cwd_differs(self):
        module = load_module()
        repo_env_path = PROJECT_ROOT / ".env"
        original_content = repo_env_path.read_text(encoding="utf-8") if repo_env_path.exists() else None

        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_cwd = Path.cwd()
            previous = os.environ.pop("MINERU_API_KEY", None)
            try:
                repo_env_path.write_text('MINERU_API_KEY="repo-mineru-key"\n', encoding="utf-8")
                os.chdir(tmp_dir)
                self.assertEqual(module.load_mineru_api_key(), "repo-mineru-key")
            finally:
                os.chdir(previous_cwd)
                if original_content is None:
                    repo_env_path.unlink(missing_ok=True)
                else:
                    repo_env_path.write_text(original_content, encoding="utf-8")
                if previous is not None:
                    os.environ["MINERU_API_KEY"] = previous

    def test_convert_pdf_to_markdown_downloads_zip_assets_and_rewrites_markdown_paths(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "paper.pdf"
            output_path = tmp_path / "output" / "paper.md"
            input_path.write_bytes(b"%PDF-1.4\n")

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w") as archive:
                archive.writestr("result.md", "# Title\n\n![](images/figure1.png)\n")
                archive.writestr("images/figure1.png", b"png-bytes")
                archive.writestr("paper_content_list.json", b"{}")
                archive.writestr("paper_origin.pdf", b"%PDF-1.4\n")
                archive.writestr("layout.json", b"{}")

            def fake_post(url, headers=None, json=None, timeout=None):
                if url.endswith("/file-urls/batch"):
                    self.assertEqual(headers["Authorization"], "Bearer mineru-key")
                    self.assertEqual(json["files"][0]["name"], "paper.pdf")
                    return Mock(
                        status_code=200,
                        json=lambda: {
                            "code": 0,
                            "data": {
                                "batch_id": "batch-1",
                                "file_urls": ["https://upload.example.com/file"],
                            },
                        },
                        text="ok",
                    )
                if url.endswith("/extract-results/batch/batch-1"):
                    return Mock(
                        status_code=200,
                        json=lambda: {
                            "code": 0,
                            "data": {
                                "extract_result": [
                                    {
                                        "data_id": "paper",
                                        "state": "done",
                                        "full_zip_url": "https://download.example.com/result.zip",
                                    }
                                ]
                            },
                        },
                        text="ok",
                    )
                raise AssertionError(f"unexpected POST/GET URL: {url}")

            def fake_get(url, headers=None, timeout=None, proxies=None):
                if url.endswith("/extract-results/batch/batch-1"):
                    return fake_post(url, headers=headers, timeout=timeout)
                if url == "https://download.example.com/result.zip":
                    return Mock(status_code=200, content=zip_buffer.getvalue())
                raise AssertionError(f"unexpected GET URL: {url}")

            def fake_put(url, data=None, timeout=None):
                self.assertEqual(url, "https://upload.example.com/file")
                self.assertIsNotNone(data)
                return Mock(status_code=200)

            with patch.object(module.requests, "post", side_effect=fake_post), patch.object(
                module.requests, "get", side_effect=fake_get
            ), patch.object(module.requests, "put", side_effect=fake_put), patch.object(
                module.time, "sleep", return_value=None
            ):
                module.convert_pdf_to_markdown(
                    input_path,
                    output_path,
                    api_key="mineru-key",
                    poll_interval=0,
                    timeout=1,
                )

            markdown = output_path.read_text(encoding="utf-8")
            asset_path = output_path.with_name("paper_assets") / "figure1.png"
            self.assertTrue(asset_path.exists())
            self.assertIn("![](paper_assets/figure1.png)", markdown)
            self.assertEqual(
                sorted(path.name for path in asset_path.parent.iterdir()),
                ["figure1.png"],
            )

    def test_convert_pdf_to_markdown_checks_input_before_network_request(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "missing.pdf"
            output_path = tmp_path / "output" / "missing.md"

            with patch.object(module.requests, "post") as post:
                with self.assertRaises(FileNotFoundError):
                    module.convert_pdf_to_markdown(input_path, output_path, api_key="mineru-key")

            post.assert_not_called()

    def test_normalize_pdf_markdown_restores_heading_hierarchy(self):
        module = load_module()

        markdown = "## 1. Introduction\n\n## 1.1 Overview\n\n## 1.1.1 Details\n"

        normalized = module.normalize_pdf_markdown(markdown)

        self.assertIn("## 1. Introduction", normalized)
        self.assertIn("### 1.1 Overview", normalized)
        self.assertIn("#### 1.1.1 Details", normalized)

    def test_normalize_pdf_markdown_demotes_body_sentences_and_numbered_lists(self):
        module = load_module()

        markdown = (
            "## 该预测器的具体预测策略如下：\n\n"
            "## 1. 基于模块层级的时钟门控插入\n\n"
            "## 2. 另一项策略\n"
        )

        normalized = module.normalize_pdf_markdown(markdown)

        self.assertIn("\n该预测器的具体预测策略如下：\n", "\n" + normalized)
        self.assertIn("\n1. 基于模块层级的时钟门控插入\n", "\n" + normalized)
        self.assertIn("\n2. 另一项策略\n", "\n" + normalized)
        self.assertNotIn("## 该预测器", normalized)
        self.assertNotIn("## 1. 基于", normalized)

    def test_normalize_pdf_markdown_converts_html_tables_to_pipe_tables(self):
        module = load_module()

        markdown = (
            "<table><tr><th>Name</th><th>Value</th></tr>"
            "<tr><td>A</td><td>1</td></tr></table>\n"
        )

        normalized = module.normalize_pdf_markdown(markdown)

        self.assertIn("| Name | Value |", normalized)
        self.assertIn("| --- | --- |", normalized)
        self.assertIn("| A | 1 |", normalized)
        self.assertNotIn("<table", normalized)

    def test_normalize_pdf_markdown_expands_rowspan_and_colspan(self):
        module = load_module()

        markdown = (
            '<table><tr><th rowspan="2">Group</th><th colspan="2">Values</th></tr>'
            "<tr><td>A</td><td>1</td></tr></table>\n"
        )

        normalized = module.normalize_pdf_markdown(markdown)

        self.assertIn("| Group | Values | Values |", normalized)
        self.assertIn("| Group | A | 1 |", normalized)
        self.assertNotIn("<table", normalized)

    def test_normalize_pdf_markdown_uses_chinese_sections_as_top_level(self):
        module = load_module()

        markdown = (
            "## 一、学位论文进展情况\n\n"
            "## 1.1 学位论文进展情况\n\n"
            "#### 1.1.1 研究背景\n\n"
            "## 二、下一步工作计划\n\n"
            "## 1. 下一步工作计划和内容\n\n"
            "## 2. 预计答辩时间\n"
        )

        normalized = module.normalize_pdf_markdown(markdown)

        self.assertIn("## 一、学位论文进展情况", normalized)
        # 一、/二、 sit at level 2, so 1.1 and 1. are both subsections (level 3)
        self.assertIn("### 1.1 学位论文进展情况", normalized)
        self.assertIn("#### 1.1.1 研究背景", normalized)
        self.assertIn("## 二、下一步工作计划", normalized)
        self.assertIn("### 1. 下一步工作计划和内容", normalized)
        self.assertIn("### 2. 预计答辩时间", normalized)
        self.assertNotIn("\n## 1. 下一步工作计划和内容", normalized)

    def test_normalize_pdf_markdown_demotes_numbered_list_inside_a_section(self):
        module = load_module()

        markdown = (
            "## 1. 学位论文进展情况\n\n"
            "#### 1.2.2 前端代码设计还有很大优化空间\n\n"
            "正文段落。\n\n"
            "## 1. 基于模块层级的时钟门控插入\n\n"
            "正文段落。\n\n"
            "## 2. 替换不必要复位的寄存器\n\n"
            "正文段落。\n\n"
            "### 1.3 下一步工作计划\n"
        )

        normalized = module.normalize_pdf_markdown(markdown)

        # 1.3 keeps the previous chapter open, so the bare "1."/"2." are list items
        self.assertIn("\n1. 基于模块层级的时钟门控插入\n", "\n" + normalized)
        self.assertIn("\n2. 替换不必要复位的寄存器\n", "\n" + normalized)
        self.assertNotIn("## 1. 基于模块层级", normalized)
        self.assertNotIn("## 2. 替换不必要复位", normalized)
        self.assertIn("## 1. 学位论文进展情况", normalized)
        self.assertIn("### 1.3 下一步工作计划", normalized)

    def test_normalize_pdf_markdown_keeps_real_sections_with_children(self):
        module = load_module()

        markdown = (
            "## 1. 学位论文进展情况\n\n"
            "### 1.1 学位论文进展情况\n\n"
            "### 1.3 已取得阶段性成果\n\n"
            "正文段落。\n\n"
            "## 2. 下一步工作计划\n\n"
            "### 2.1 工作计划\n\n"
            "## 3. 参考文献\n"
        )

        normalized = module.normalize_pdf_markdown(markdown)

        self.assertIn("## 2. 下一步工作计划", normalized)
        self.assertIn("### 2.1 工作计划", normalized)
        self.assertIn("## 3. 参考文献", normalized)



if __name__ == "__main__":
    unittest.main()
