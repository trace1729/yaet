from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import yaet_config
from translate_markdown_book import create_client, load_api_key


DEFAULT_MODEL = "deepseek-chat"


def build_translation_prompt(
    *,
    from_lang: str = "English",
    to_lang: str = "Simplified Chinese",
    title_prompt: str = "",
    summary_prompt: str = "",
    terms_prompt: str = "",
) -> str:
    return (
        f"{from_lang}→ {to_lang},仅输出译文\n"
        "要求:\n"
        f"1.  符合{to_lang}母语者表达习惯\n"
        "2.  根据语境灵活转换语气，译文传情达意\n"
        f"3.  {to_lang}受众易懂的地道表达\n"
        "4.  意译而非直译，专业内容保证术语准确\n"
        "5.  参考:\n"
        f"    {title_prompt}\n"
        f"    {summary_prompt}\n"
        f"    {terms_prompt}\n"
        "待译:\n"
        "{{text}}\n"
        "[仅输出译文，**无需任何**说明，注解，注释，解释]"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate one English string to Simplified Chinese.")
    parser.add_argument("text", nargs="?", help="English text to translate")
    parser.add_argument("--bilingual", action="store_true", help="Output bilingual text from a txt file, stdin, or direct input")
    parser.add_argument("--input", dest="input_path", help="Path to a txt file for bilingual mode")
    parser.add_argument("-o", "--output", dest="output_path", help="Optional output path for bilingual mode")
    parser.add_argument("-v", "--verbose", action="store_true", help="Include pronunciation and an example sentence")
    parser.add_argument("--model", help="Model name; overrides provider.model from --config")
    parser.add_argument("--config", help="Path to a yaet.yaml config file")
    return parser.parse_args(argv)


def build_prompt_options(config) -> tuple[str, str, str]:
    """Collect title/summary/terms (including glossary) for the prompt reference block."""
    prompt_config = config.prompt
    terms_parts = [prompt_config.terms.strip()]
    glossary = dict(getattr(config, "glossary", {}) or {})
    if glossary:
        terms_parts.append("\n".join(f"- {english} -> {chinese}" for english, chinese in glossary.items()))
    return prompt_config.title, prompt_config.summary, "\n".join(part for part in terms_parts if part)


def translate_text(client, text: str, model: str, temperature: float = 0.2, prompt_options: tuple[str, str, str] | None = None) -> str:
    title_prompt, summary_prompt, terms_prompt = prompt_options or ("", "", "")
    prompt = build_translation_prompt(
        title_prompt=title_prompt,
        summary_prompt=summary_prompt,
        terms_prompt=terms_prompt,
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": prompt.replace("{{text}}", text),
            },
        ],
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()


def translate_text_verbose(client, text: str, model: str, temperature: float = 0.2) -> dict[str, str]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You help English speakers understand Chinese translations. "
                    "Return JSON with translation, pronunciation, and example fields."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Translate this English text into Simplified Chinese and provide pronunciation guidance "
                    "plus one short Chinese example sentence as JSON: "
                    f"{text}"
                ),
            },
        ],
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    return parse_verbose_response(response.choices[0].message.content)


def translate_text_bilingual(
    client,
    text: str,
    model: str,
    temperature: float = 0.2,
    prompt_options: tuple[str, str, str] | None = None,
) -> str:
    translation = translate_text(client, text, model, temperature=temperature, prompt_options=prompt_options)
    return f"{text.strip()}\n\n{translation}"


def parse_verbose_response(content: str) -> dict[str, str]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("DeepSeek returned invalid verbose JSON") from error

    required_fields = ("translation", "pronunciation", "example")
    missing = [field for field in required_fields if not payload.get(field)]
    if missing:
        raise ValueError(f"DeepSeek verbose response is missing required fields: {', '.join(missing)}")

    return {field: str(payload[field]).strip() for field in required_fields}


def format_verbose_output(payload: dict[str, str]) -> str:
    return (
        f"Translation: {payload['translation']}\n"
        f"Pronunciation: {payload['pronunciation']}\n"
        f"Example: {payload['example']}"
    )


def resolve_input_text(args: argparse.Namespace) -> str:
    if args.input_path:
        return Path(args.input_path).read_text(encoding="utf-8")
    if args.text:
        return args.text
    return sys.stdin.read()


def main(argv: Sequence[str] | None = None, env_path: Path | None = None, config=None) -> int:
    args = parse_args(argv)
    if config is None:
        config = yaet_config.load_config(getattr(args, "config", None))
    provider = config.provider
    model = args.model or provider.model or DEFAULT_MODEL
    prompt_options = build_prompt_options(config)

    api_key = provider.api_key or load_api_key(env_path, env_var=provider.api_key_env)
    if not api_key:
        print(
            f"Missing {provider.api_key_env}. Set it in the environment or .env.",
            file=sys.stderr,
        )
        return 1

    try:
        client = create_client(api_key, base_url=provider.base_url)
        if args.bilingual:
            raw_text = resolve_input_text(args)
            if not raw_text.strip():
                print("Bilingual mode requires input text, --input, or stdin.", file=sys.stderr)
                return 1
            bilingual_text = translate_text_bilingual(
                client,
                raw_text,
                model=model,
                temperature=provider.temperature,
                prompt_options=prompt_options,
            )
            if args.output_path:
                Path(args.output_path).write_text(f"{bilingual_text}\n", encoding="utf-8")
            else:
                print(bilingual_text)
        elif args.verbose:
            if not args.text:
                print("Missing input text.", file=sys.stderr)
                return 1
            payload = translate_text_verbose(client, args.text, model=model, temperature=provider.temperature)
            print(format_verbose_output(payload))
        else:
            if not args.text:
                print("Missing input text.", file=sys.stderr)
                return 1
            print(
                translate_text(
                    client,
                    args.text,
                    model=model,
                    temperature=provider.temperature,
                    prompt_options=prompt_options,
                )
            )
    except Exception as error:
        print(f"Translation failed: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
