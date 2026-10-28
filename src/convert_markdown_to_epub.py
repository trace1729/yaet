from __future__ import annotations

import argparse
import hashlib
import html
import mimetypes
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence
from urllib.parse import urlparse
from urllib.request import urlopen

from output_paths import book_output_dir, canonical_book_name, sanitize_book_dir_name

IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>(?:[^()\n]|\([^()\n]*\))+)\)")
INLINE_TRIPLE_PATTERN = re.compile(r"(\*\*\*|___)(.+?)\1")
INLINE_DOUBLE_PATTERN = re.compile(r"(\*\*|__)(.+?)\1")
INLINE_SINGLE_PATTERN = re.compile(r"(\*|_)(.+?)\1")


@dataclass(frozen=True)
class Section:
    title: str
    markdown: str
    level: int


@dataclass(frozen=True)
class ImageResource:
    source: str
    cache_path: Path
    media_type: str
    epub_path: str
    alt: str


def select_cover_resource(resources: Sequence[ImageResource]) -> ImageResource | None:
    if not resources:
        return None
    for resource in resources:
        source_path = urlparse(resource.source).path if _is_remote(resource.source) else resource.source
        filename = PurePosixPath(source_path).name.lower()
        if filename.startswith("cover"):
            return resource
    return resources[0]


def build_book_sections(markdown: str) -> list[Section]:
    lines = markdown.splitlines()
    sections: list[Section] = []
    current_title = "Untitled"
    current_level = 1
    current_lines: list[str] = []

    for line in lines:
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            title = line[level:].strip() or "Untitled"
            if current_lines:
                sections.append(Section(title=current_title, markdown="\n".join(current_lines).strip(), level=current_level))
            current_title = title
            current_level = level
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append(Section(title=current_title, markdown="\n".join(current_lines).strip(), level=current_level))

    return sections


def _default_fetch_remote(url: str) -> tuple[bytes, str]:
    with urlopen(url) as response:
        data = response.read()
        media_type = response.headers.get_content_type() or _guess_media_type(url)
    return data, media_type


def _guess_media_type(source: str) -> str:
    guessed, _ = mimetypes.guess_type(source)
    return guessed or "application/octet-stream"


def _suffix_for_source(source: str, media_type: str) -> str:
    path = urlparse(source).path if source.startswith(("http://", "https://")) else source
    suffix = Path(path).suffix
    if suffix:
        return suffix
    return mimetypes.guess_extension(media_type) or ".bin"


def _is_remote(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def collect_image_resources(
    markdown: str,
    base_dir: Path,
    cache_dir: Path,
    fetch_remote: Callable[[str], tuple[bytes, str]] | None = None,
) -> list[ImageResource]:
    fetcher = fetch_remote or _default_fetch_remote
    cache_dir.mkdir(parents=True, exist_ok=True)
    resources: list[ImageResource] = []
    seen: set[str] = set()

    for index, match in enumerate(IMAGE_PATTERN.finditer(markdown)):
        source = match.group("src").strip()
        if source in seen:
            continue
        seen.add(source)
        alt = match.group("alt")

        if _is_remote(source):
            data, media_type = fetcher(source)
            suffix = _suffix_for_source(source, media_type)
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
            cache_path = cache_dir / f"{digest}{suffix}"
            if not cache_path.exists():
                cache_path.write_bytes(data)
        else:
            source_path = (base_dir / source).resolve()
            if not source_path.exists():
                raise FileNotFoundError(f"Image not found: {source}")
            media_type = _guess_media_type(source_path.name)
            suffix = source_path.suffix or _suffix_for_source(source_path.name, media_type)
            digest = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:16]
            cache_path = cache_dir / f"{digest}{suffix}"
            if not cache_path.exists():
                shutil.copyfile(source_path, cache_path)

        epub_path = f"images/Image{index:05d}{cache_path.suffix}"
        resources.append(
            ImageResource(
                source=source,
                cache_path=cache_path,
                media_type=media_type,
                epub_path=epub_path,
                alt=alt,
            )
        )

    return resources


def rewrite_markdown_image_links(markdown: str, resources: Sequence[ImageResource]) -> str:
    mapping = {resource.source: resource.epub_path for resource in resources}

    def replace(match: re.Match[str]) -> str:
        alt = match.group("alt")
        source = match.group("src").strip()
        new_source = mapping.get(source, source)
        return f"![{alt}]({new_source})"

    return IMAGE_PATTERN.sub(replace, markdown)


def render_inline_markdown(text: str) -> str:
    escaped = html.escape(text)

    escaped = INLINE_TRIPLE_PATTERN.sub(r"<strong><em>\2</em></strong>", escaped)
    escaped = INLINE_DOUBLE_PATTERN.sub(r"<strong>\2</strong>", escaped)
    escaped = INLINE_SINGLE_PATTERN.sub(r"<em>\2</em>", escaped)

    return escaped


def markdown_to_html(markdown: str) -> str:
    html_lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            title = render_inline_markdown(line[level:].strip())
            html_lines.append(f"<h{level}>{title}</h{level}>")
            continue

        stripped = line.strip()
        if not stripped:
            continue

        image_match = IMAGE_PATTERN.fullmatch(stripped)
        if image_match:
            alt = html.escape(image_match.group("alt"))
            src = html.escape(image_match.group("src").strip())
            html_lines.append(f'<p><img alt="{alt}" src="{src}" /></p>')
        else:
            html_lines.append(f"<p>{render_inline_markdown(line)}</p>")
    return "\n".join(html_lines)


def build_epub_book(
    markdown: str,
    title: str,
    author: str,
    *,
    base_dir: Path | None = None,
    cache_dir: Path | None = None,
    fetch_remote: Callable[[str], tuple[bytes, str]] | None = None,
):
    try:
        from ebooklib import epub
    except ImportError as error:
        raise RuntimeError("Missing dependency: install `ebooklib` first") from error

    resolved_base_dir = (base_dir or Path.cwd()).resolve()
    resolved_cache_dir = cache_dir or resolved_base_dir / ".epub_image_cache"

    resources = collect_image_resources(markdown, resolved_base_dir, resolved_cache_dir, fetch_remote=fetch_remote)
    rewritten_markdown = rewrite_markdown_image_links(markdown, resources)

    book = epub.EpubBook()
    book.set_identifier("the-society-of-mind-bilingual")
    book.set_title(title)
    book.set_language("zh-CN")
    book.add_author(author)

    cover_resource = select_cover_resource(resources)
    if cover_resource is not None:
        # The round-trip pipeline exports the detected cover as the first image.
        book.set_cover(cover_resource.epub_path, cover_resource.cache_path.read_bytes(), create_page=False)

    sections = build_book_sections(rewritten_markdown)
    chapters = []
    for index, section in enumerate(sections, start=1):
        chapter = epub.EpubHtml(title=section.title, file_name=f"chapter_{index}.xhtml", lang="zh-CN")
        chapter.content = markdown_to_html(section.markdown)
        book.add_item(chapter)
        chapters.append(chapter)

    for resource in resources:
        if resource == cover_resource:
            continue
        image_item = epub.EpubItem(
            uid=resource.epub_path,
            file_name=resource.epub_path,
            media_type=resource.media_type,
            content=resource.cache_path.read_bytes(),
        )
        book.add_item(image_item)

    book.toc = tuple(chapters)
    book.spine = ["nav", *chapters]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    return book


def write_epub(input_path: Path, output_path: Path, title: str, author: str) -> None:
    from ebooklib import epub

    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = input_path.read_text(encoding="utf-8")
    book = build_epub_book(
        markdown,
        title=title,
        author=author,
        base_dir=input_path.parent,
        cache_dir=input_path.parent / ".epub_image_cache",
    )
    epub.write_epub(str(output_path), book)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an EPUB from bilingual Markdown.")
    parser.add_argument("input_path", nargs="?", default="The_society_of_mind.bilingual.md")
    parser.add_argument("-o", "--output", dest="output_path")
    parser.add_argument("--title", default=None)
    parser.add_argument("--author", default="Unknown Author")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input_path)
    output_path = (
        Path(args.output_path)
        if args.output_path
        else book_output_dir(input_path) / f"{sanitize_book_dir_name(canonical_book_name(input_path))}.bilingual.epub"
    )
    write_epub(input_path, output_path, title=args.title or canonical_book_name(input_path), author=args.author)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
