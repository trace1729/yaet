import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def load_translate_cli_module(test_case: unittest.TestCase):
    module_path = SRC_DIR / "translate_text_cli.py"
    if not module_path.exists():
        test_case.fail("translate_text_cli.py is missing")

    spec = importlib.util.spec_from_file_location("translate_text_cli", module_path)
    if spec is None or spec.loader is None:
        test_case.fail("Unable to load translate_text_cli.py")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TranslateCliTests(unittest.TestCase):
    def test_main_outputs_translation_only_by_default(self):
        module = load_translate_cli_module(self)
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content="你好，世界。"))]
        )

        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text('DEEPSEEK_API_KEY="test-key"\n', encoding="utf-8")
            with patch.object(module, "create_client", return_value=client):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = module.main(["Hello world"], env_path=env_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "你好，世界。\n")
        self.assertEqual(stderr.getvalue(), "")
        system_prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("仅输出译文", system_prompt)
        self.assertIn("待译:", system_prompt)
        self.assertIn("Hello world", system_prompt)

    def test_main_outputs_translation_pronunciation_and_example_in_verbose_mode(self):
        module = load_translate_cli_module(self)
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[
                Mock(
                    message=Mock(
                        content=(
                            '{"translation":"你好，世界。","pronunciation":"ni hao, shi jie",'
                            '"example":"你好，世界。今天真安静。"}'
                        )
                    )
                )
            ]
        )

        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text('DEEPSEEK_API_KEY="test-key"\n', encoding="utf-8")
            with patch.object(module, "create_client", return_value=client):
                with contextlib.redirect_stdout(stdout):
                    exit_code = module.main(["--verbose", "Hello world"], env_path=env_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stdout.getvalue(),
            "Translation: 你好，世界。\nPronunciation: ni hao, shi jie\nExample: 你好，世界。今天真安静。\n",
        )

    def test_main_returns_error_when_api_key_is_missing(self):
        module = load_translate_cli_module(self)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            previous = os.environ.pop("DEEPSEEK_API_KEY", None)
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = module.main(["Hello world"], env_path=env_path)
            finally:
                if previous is not None:
                    os.environ["DEEPSEEK_API_KEY"] = previous

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("DEEPSEEK_API_KEY", stderr.getvalue())

    def test_bilingual_mode_reads_txt_file_and_writes_bilingual_output_file(self):
        module = load_translate_cli_module(self)
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content="第一行。"))]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            env_path = tmp_path / ".env"
            input_path = tmp_path / "sample.txt"
            output_path = tmp_path / "sample.bilingual.txt"
            env_path.write_text('DEEPSEEK_API_KEY="test-key"\n', encoding="utf-8")
            input_path.write_text("Line one.", encoding="utf-8")

            with patch.object(module, "create_client", return_value=client):
                exit_code = module.main(
                    ["--bilingual", "--input", str(input_path), "--output", str(output_path)],
                    env_path=env_path,
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_text(encoding="utf-8"), "Line one.\n\n第一行。\n")

    def test_bilingual_mode_reads_from_stdin_and_prints_bilingual_output(self):
        module = load_translate_cli_module(self)
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content="你好"))]
        )

        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text('DEEPSEEK_API_KEY="test-key"\n', encoding="utf-8")
            with patch.object(module, "create_client", return_value=client), patch.object(
                sys, "stdin", io.StringIO("Hello")
            ):
                with contextlib.redirect_stdout(stdout):
                    exit_code = module.main(["--bilingual"], env_path=env_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "Hello\n\n你好\n")

    def test_main_reads_provider_and_prompt_options_from_config(self):
        module = load_translate_cli_module(self)
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content="你好"))]
        )

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "yaet.yaml"
            config_path.write_text(
                "provider:\n"
                "  base_url: https://example-proxy.test/v1\n"
                "  api_key_env: MY_DEEPSEEK_KEY\n"
                "  model: deepseek-reasoner\n"
                "  temperature: 0.7\n"
                "prompt:\n"
                "  terms: \"面向体系结构研究\"\n"
                "glossary:\n"
                "  agent: 智能体\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MY_DEEPSEEK_KEY": "config-key"}, clear=False):
                with patch.object(module, "create_client", return_value=client) as create_client:
                    with contextlib.redirect_stdout(stdout):
                        exit_code = module.main(
                            ["--config", str(config_path), "hello"],
                            env_path=Path(tmp_dir) / "missing.env",
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "你好\n")
        create_client.assert_called_once_with("config-key", base_url="https://example-proxy.test/v1")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek-reasoner")
        self.assertEqual(kwargs["temperature"], 0.7)
        system_prompt = kwargs["messages"][0]["content"]
        self.assertIn("面向体系结构研究", system_prompt)
        self.assertIn("- agent -> 智能体", system_prompt)

    def test_main_model_flag_overrides_config_model(self):
        module = load_translate_cli_module(self)
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content="你好"))]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "yaet.yaml"
            config_path.write_text(
                "provider:\n"
                "  api_key_env: MY_DEEPSEEK_KEY\n"
                "  model: deepseek-reasoner\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MY_DEEPSEEK_KEY": "config-key"}, clear=False):
                with patch.object(module, "create_client", return_value=client):
                    with contextlib.redirect_stdout(io.StringIO()):
                        exit_code = module.main(
                            ["--config", str(config_path), "--model", "deepseek-chat", "hello"],
                            env_path=Path(tmp_dir) / "missing.env",
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "deepseek-chat")



if __name__ == "__main__":
    unittest.main()
