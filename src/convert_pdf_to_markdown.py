from __future__ import annotations

import argparse
import html
import io
import os
import re
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

try:
    import requests
except ImportError:
    requests = SimpleNamespace(post=None, get=None, put=None)

from env_utils import load_key_from_dotenv
from output_paths import book_output_dir, canonical_book_name, sanitize_book_dir_name


CLOUD_API_URL = "https://mineru.net/api/v4"
DEFAULT_MODEL_VERSION = "pipeline"
DEFAULT_LANGUAGE = "ch"
DEFAULT_TIMEOUT = 600
DEFAULT_POLL_INTERVAL = 5
IMAGE_REFERENCE_PATTERN = re.compile(r"(!\[[^\]]*\]\()images/([^)]+)\)")
IMAGE_SUFFIXES = {
    ".avif",
    ".bmp",
    ".emf",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
    ".wmf",
}
HEADING_PATTERN = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>\S.*)$")
SECTION_NUMBER_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)*)\.?(?:\s+|$)(?P<title>.*)$")
BODY_HEADING_ENDING_PATTERN = re.compile(r"[:：。！？!?；;，,]\s*$")
CHINESE_SECTION_HEADING_PATTERN = re.compile(r"^#{1,6}\s*[一二三四五六七八九十]+、")
HTML_TABLE_PATTERN = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
MAX_BODY_HEADING_LENGTH = 80
CODE_FENCE_PREFIXES = ("```", "~~~")


def load_mineru_api_key(env_path: Path | None = None) -> str | None:
    api_key = os.environ.get("MINERU_API_KEY")
    if api_key:
        return api_key

    return load_key_from_dotenv("MINERU_API_KEY", __file__, env_path)


def convert_pdf_to_markdown(
    input_path: Path,
    output_path: Path,
    *,
    api_key: str,
    cloud_url: str = CLOUD_API_URL,
    model_version: str = DEFAULT_MODEL_VERSION,
    language: str = DEFAULT_LANGUAGE,
    timeout: int = DEFAULT_TIMEOUT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
) -> None:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input PDF not found: {input_path}")
    _ensure_requests_available()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_id = input_path.stem
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "files": [{"name": input_path.name, "data_id": data_id}],
        "model_version": model_version,
        "enable_formula": True,
        "enable_table": True,
        "language": language,
    }

    response = requests.post(f"{cloud_url}/file-urls/batch", headers=headers, json=payload, timeout=30)
    response_data = _json_or_raise(response, "MinerU upload-url request failed")
    file_urls = response_data.get("data", {}).get("file_urls", [])
    batch_id = response_data.get("data", {}).get("batch_id", "")
    if not batch_id or not file_urls:
        raise RuntimeError("MinerU cloud response did not include batch_id or upload URL")

    upload_url = file_urls[0] if isinstance(file_urls[0], str) else file_urls[0].get("url", "")
    if not upload_url:
        raise RuntimeError("MinerU cloud upload URL was empty")

    with input_path.open("rb") as source_file:
        upload_response = requests.put(upload_url, data=source_file, timeout=120)
    if upload_response.status_code not in (200, 201):
        raise RuntimeError(f"MinerU upload failed with HTTP {upload_response.status_code}")

    poll_headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        poll_response = requests.get(
            f"{cloud_url}/extract-results/batch/{batch_id}",
            headers=poll_headers,
            timeout=30,
        )
        poll_data = _json_or_raise(poll_response, "MinerU poll request failed")
        results = poll_data.get("data", {}).get("extract_result", [])
        if not results:
            continue
        item = results[0]
        state = item.get("state", "")
        if state == "running":
            continue
        if state == "failed":
            raise RuntimeError(f"MinerU parsing failed: {item.get('err_msg', 'unknown')}")
        if state == "done":
            markdown = _download_cloud_result(item, output_path)
            if not markdown:
                raise RuntimeError("MinerU cloud result did not contain Markdown output")
            output_path.write_text(normalize_pdf_markdown(markdown), encoding="utf-8")
            return

    raise RuntimeError(f"MinerU cloud parsing timed out after {timeout}s")


def _json_or_raise(response, context: str) -> dict:
    if response.status_code != 200:
        raise RuntimeError(f"{context}: HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"{context}: invalid JSON response") from error
    if payload.get("code", 0) not in {0, None}:
        raise RuntimeError(f"{context}: {payload.get('msg', 'unknown error')}")
    return payload


def _ensure_requests_available() -> None:
    if not callable(getattr(requests, "post", None)):
        raise RuntimeError("Missing dependency: install `requests` first")


def _download_cloud_result(item: dict, output_path: Path) -> str | None:
    direct_markdown = item.get("md_content")
    if isinstance(direct_markdown, str) and direct_markdown.strip():
        return direct_markdown

    zip_url = item.get("full_zip_url")
    if zip_url:
        response = requests.get(zip_url, timeout=120, proxies={"http": None, "https": None})
        if response.status_code != 200:
            raise RuntimeError(f"Failed to download MinerU zip result: HTTP {response.status_code}")
        return _extract_zip_markdown(response.content, output_path)

    md_url = item.get("md_url")
    if md_url:
        response = requests.get(md_url, timeout=60, proxies={"http": None, "https": None})
        if response.status_code != 200:
            raise RuntimeError(f"Failed to download MinerU markdown result: HTTP {response.status_code}")
        return response.text

    return None


def _extract_zip_markdown(zip_bytes: bytes, output_path: Path) -> str | None:
    asset_dir = output_path.with_name(f"{output_path.stem}_assets")
    markdown: str | None = None

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            if name.endswith(".md") and markdown is None:
                markdown = archive.read(name).decode("utf-8")
                continue
            if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            parts = Path(name).parts
            if any(part in {"", ".", ".."} for part in parts):
                continue
            filename = parts[-1]
            if "images" in parts:
                relative_path = Path(*parts[parts.index("images") + 1 :])
            else:
                relative_path = Path(filename)
            target_path = asset_dir / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(archive.read(name))

    if markdown is None:
        return None

    asset_dir_name = asset_dir.name
    return IMAGE_REFERENCE_PATTERN.sub(rf"\1{asset_dir_name}/\2)", markdown)


def normalize_pdf_markdown(markdown: str) -> str:
    trailing_newline = markdown.endswith("\n")
    normalized = _convert_html_tables(markdown)
    normalized = _normalize_pdf_headings(normalized)
    normalized = _ensure_table_spacing(normalized)
    if trailing_newline and not normalized.endswith("\n"):
        return normalized + "\n"
    return normalized


def _ensure_table_spacing(markdown: str) -> str:
    """GFM tables must be separated from surrounding paragraphs by a blank line."""
    lines = markdown.splitlines()
    output: list[str] = []
    in_code_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(CODE_FENCE_PREFIXES):
            in_code_fence = not in_code_fence
        is_table_row = not in_code_fence and stripped.startswith("|")
        if is_table_row and output and output[-1].strip() and not output[-1].strip().startswith("|"):
            output.append("")
        output.append(line)
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if is_table_row and next_line and not next_line.startswith("|"):
            output.append("")
    return "\n".join(output)


def _convert_html_tables(markdown: str) -> str:
    lines = markdown.splitlines()
    converted: list[str] = []
    in_code_fence = False
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith(CODE_FENCE_PREFIXES):
            in_code_fence = not in_code_fence
            converted.append(line)
            index += 1
            continue

        if not in_code_fence and re.search(r"<table\b", line, re.IGNORECASE):
            table_lines = [line]
            while "</table>" not in table_lines[-1].lower() and index + 1 < len(lines):
                index += 1
                table_lines.append(lines[index])
            table_block = "\n".join(table_lines)
            converted.append(HTML_TABLE_PATTERN.sub(lambda match: _html_table_to_pipe_table(match.group(0)), table_block))
            index += 1
            continue

        converted.append(line)
        index += 1

    return "\n".join(converted)


class _HtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.rows: list[list[dict]] = []
        self._row: list[dict] = []
        self._cell: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            attributes = dict(attrs)
            self._cell = {
                "text": [],
                "header": tag == "th",
                "rowspan": _positive_int(attributes.get("rowspan")),
                "colspan": _positive_int(attributes.get("colspan")),
            }
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)

    def handle_entityref(self, name: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
            self._row = []


def _positive_int(value: str | None) -> int:
    try:
        parsed = int(value or "1")
    except ValueError:
        return 1
    return max(parsed, 1)


def _html_table_to_pipe_table(table_html: str) -> str:
    parser = _HtmlTableParser()
    parser.feed(table_html)
    parser.close()
    if parser._row:
        parser.rows.append(parser._row)
    if not parser.rows:
        return table_html

    grid = _table_rows_to_grid(parser.rows)
    if not grid:
        return table_html

    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    header = grid[0]
    body = grid[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _table_rows_to_grid(rows: list[list[dict]]) -> list[list[str]]:
    grid: list[list[str | None]] = []

    for row_index, row in enumerate(rows):
        while len(grid) <= row_index:
            grid.append([])
        column_index = 0
        for cell in row:
            while column_index < len(grid[row_index]) and grid[row_index][column_index] is not None:
                column_index += 1
            text = _clean_table_cell(cell["text"])
            rowspan = cell["rowspan"]
            colspan = cell["colspan"]
            for row_offset in range(rowspan):
                target_row_index = row_index + row_offset
                while len(grid) <= target_row_index:
                    grid.append([])
                for column_offset in range(colspan):
                    target_column_index = column_index + column_offset
                    while len(grid[target_row_index]) <= target_column_index:
                        grid[target_row_index].append(None)
                    if grid[target_row_index][target_column_index] is None:
                        grid[target_row_index][target_column_index] = text
            column_index += colspan

    return [[cell or "" for cell in row] for row in grid]


def _clean_table_cell(chunks: Sequence[str]) -> str:
    text = html.unescape("".join(chunks))
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("|", r"\|")


def _normalize_pdf_headings(markdown: str) -> str:
    lines = markdown.splitlines()
    normalized: list[str] = []
    in_code_fence = False
    expect_numbered_list = False
    in_numbered_list = False
    chinese_toplevel = _uses_chinese_section_numbers(lines)
    next_dotted_numbers = _next_dotted_numbers(lines)
    last_level = 1
    last_bare_section: int | None = None
    last_dotted_root: str | None = None
    previous_emitted_is_heading = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(CODE_FENCE_PREFIXES):
            in_code_fence = not in_code_fence
            normalized.append(line)
            continue

        if in_code_fence:
            normalized.append(line)
            continue

        heading_match = HEADING_PATTERN.match(stripped)
        if heading_match is None:
            if stripped:
                expect_numbered_list = _ends_with_intro_punctuation(stripped)
                in_numbered_list = False
                previous_emitted_is_heading = False
            normalized.append(line)
            continue

        hashes = heading_match.group("hashes")
        title = heading_match.group("title").strip()
        if len(hashes) == 1:
            in_numbered_list = False
            last_level = 1
            previous_emitted_is_heading = True
            normalized.append(line)
            continue

        section_match = SECTION_NUMBER_PATTERN.match(title)
        if section_match is None:
            in_numbered_list = False
            if _looks_like_body_heading(title):
                normalized.append(title)
                expect_numbered_list = _ends_with_intro_punctuation(title)
                previous_emitted_is_heading = False
            else:
                normalized.append(line)
                expect_numbered_list = False
                last_level = len(hashes)
                previous_emitted_is_heading = True
            continue

        number = section_match.group("number")
        level = _section_level(number, chinese_toplevel)

        if "." in number:
            normalized.append(f"{'#' * level} {title}")
            expect_numbered_list = False
            in_numbered_list = False
            last_level = level
            last_dotted_root = number.split(".", 1)[0]
            previous_emitted_is_heading = True
            if level <= 2:
                last_bare_section = None
            continue

        is_section = _bare_number_is_section(
            number=int(number),
            level=level,
            last_level=last_level,
            previous_emitted_is_heading=previous_emitted_is_heading,
            next_dotted=next_dotted_numbers[index],
            last_bare_section=last_bare_section,
            last_dotted_root=last_dotted_root,
        )
        if (
            not is_section
            or _looks_like_body_heading(title)
            or in_numbered_list
            or expect_numbered_list
        ):
            normalized.append(title)
            in_numbered_list = True
            expect_numbered_list = False
            previous_emitted_is_heading = False
            continue

        normalized.append(f"{'#' * level} {title}")
        in_numbered_list = False
        expect_numbered_list = False
        last_level = level
        previous_emitted_is_heading = True
        last_bare_section = int(number)

    return "\n".join(normalized)


def _section_level(number: str, chinese_toplevel: bool) -> int:
    """Level for a numbered heading; 一、/二、 documents push numbering one level down."""
    dots = number.count(".")
    if chinese_toplevel:
        return min(6, 2 + max(dots, 1))
    return min(6, 2 + dots)


def _bare_number_is_section(
    *,
    number: int,
    level: int,
    last_level: int,
    previous_emitted_is_heading: bool,
    next_dotted: str | None,
    last_bare_section: int | None,
    last_dotted_root: str | None,
) -> bool:
    """Decide whether a bare ``N.`` heading is a real section instead of a list item.

    Numbering alone is ambiguous: ``1.`` can open a chapter or enumerate a list.
    Context is checked in this order:
    1. it deepens the hierarchy,
    2. it directly follows another heading (``二、`` then ``1.``),
    3. a later heading uses it as a prefix (``2.`` then ``2.1``) and the candidate
       is not just repeating the chapter that is already open,
    4. a later heading continues the previous section (``1.3`` after ``2.``) -> list item,
    5. it continues the sequence of accepted sections (``3.`` after ``2.``).
    """
    if level > last_level:
        return True
    if previous_emitted_is_heading:
        return True
    if next_dotted:
        opens_new_number = last_dotted_root is None or str(number) != last_dotted_root
        if opens_new_number and next_dotted.startswith(f"{number}."):
            return True
        if last_bare_section is not None and next_dotted.startswith(f"{last_bare_section}."):
            return False
    if last_bare_section is not None and number == last_bare_section + 1:
        return True
    return False


def _uses_chinese_section_numbers(lines: Sequence[str]) -> bool:
    """True when the document is outlined with 一、二、 instead of 1./1.1."""
    hits = 0
    in_code_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(CODE_FENCE_PREFIXES):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if CHINESE_SECTION_HEADING_PATTERN.match(stripped):
            hits += 1
            if hits >= 2:
                return True
    return False


def _next_dotted_numbers(lines: Sequence[str]) -> list[str | None]:
    """For every line, the number of the next ``N.M`` heading (or None)."""
    pending: list[str | None] = [None] * len(lines)
    upcoming: str | None = None
    for index in range(len(lines) - 1, -1, -1):
        pending[index] = upcoming
        match = HEADING_PATTERN.match(lines[index].strip())
        if match is None:
            continue
        section_match = SECTION_NUMBER_PATTERN.match(match.group("title").strip())
        if section_match is not None and "." in section_match.group("number"):
            upcoming = section_match.group("number")
    return pending


def _ends_with_intro_punctuation(text: str) -> bool:
    return bool(re.search(r"[:：]\s*$", text))


def _looks_like_body_heading(title: str) -> bool:
    if not title:
        return False
    if BODY_HEADING_ENDING_PATTERN.search(title):
        return True
    return len(title) > MAX_BODY_HEADING_LENGTH


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a PDF file to Markdown via MinerU cloud.")
    parser.add_argument("input_path", help="Path to the source PDF file.")
    parser.add_argument("-o", "--output", dest="output_path", help="Path to the output Markdown file.")
    parser.add_argument("--cloud-url", default=CLOUD_API_URL, help="MinerU cloud API base URL.")
    parser.add_argument("--model-version", default=DEFAULT_MODEL_VERSION)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input_path)
    output_path = (
        Path(args.output_path)
        if args.output_path
        else book_output_dir(input_path) / f"{sanitize_book_dir_name(canonical_book_name(input_path))}.md"
    )
    if not input_path.is_file():
        raise SystemExit(f"Input PDF not found: {input_path}")
    api_key = load_mineru_api_key()
    if not api_key:
        raise SystemExit("Missing MINERU_API_KEY; set the env var or add it to .env")

    convert_pdf_to_markdown(
        input_path,
        output_path,
        api_key=api_key,
        cloud_url=args.cloud_url,
        model_version=args.model_version,
        language=args.language,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
