from __future__ import annotations

import argparse
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from ebooklib import ITEM_DOCUMENT, epub
from output_paths import book_output_dir, canonical_book_name, sanitize_book_dir_name


@dataclass(frozen=True)
class TocEntry:
    level: int
    title: str
    anchor: str


@dataclass(frozen=True)
class NoteReference:
    key: str
    label: str


class Slugger:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def slugify(self, text: str) -> str:
        lowered = text.strip().lower()
        pieces: list[str] = []
        last_was_dash = False
        for char in lowered:
            if char.isalnum():
                pieces.append(char)
                last_was_dash = False
            elif not last_was_dash and pieces:
                pieces.append("-")
                last_was_dash = True
        slug = "".join(pieces).strip("-") or "section"
        count = self._counts.get(slug, 0)
        self._counts[slug] = count + 1
        if count:
            return f"{slug}-{count}"
        return slug


class AssetExporter:
    def __init__(self, book: epub.EpubBook, output_path: Path, asset_dir_name: str | None = None) -> None:
        self.asset_dir = output_path.with_name(asset_dir_name or f"{output_path.stem}_assets")
        self._items: dict[str, object] = {}
        for item in book.get_items():
            href = getattr(item, "file_name", None) or item.get_name()
            if href:
                self._items[posixpath.normpath(href)] = item

    def export(self, document_href: str, source_href: str | None) -> str | None:
        if not source_href:
            return None
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document_href), source_href))
        return self._export_resolved_href(resolved)

    def export_book_asset(self, source_href: str | None) -> str | None:
        if not source_href:
            return None
        return self._export_resolved_href(posixpath.normpath(source_href))

    def _export_resolved_href(self, resolved: str) -> str | None:
        item = self._items.get(resolved)
        if item is None:
            return None

        filename = PurePosixPath(resolved).name
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.asset_dir / filename
        if not target_path.exists():
            target_path.write_bytes(item.get_content())
        return posixpath.join(self.asset_dir.name, filename)


def class_heading_level_for_element(element: ET.Element) -> int | None:
    class_names = (element.get("class") or "").split()
    for class_name in class_names:
        if class_name == "title":
            return 2
        match = re.fullmatch(r"title(\d+)", class_name)
        if match:
            return int(match.group(1)) + 1
    return None


def should_descend_into_children(element: ET.Element) -> bool:
    if local_name(element.tag) != "span":
        return False
    for child in element:
        tag = local_name(child.tag)
        if tag in {"section", "article", "div", "main", "body", "p", "ul", "ol", "blockquote", "pre", "img"}:
            return True
        if class_heading_level_for_element(child) is not None:
            return True
        if should_descend_into_children(child):
            return True
    return False


def normalize_heading_text(text: str) -> str:
    cleaned = re.sub(r"(\*\*|\*)([^\w\s]+)\1", r"\2", text)
    cleaned = re.sub(r"(?:(?<=[A-Za-z§])([.?!,:;])(?=\w)|(?<=\d)([.?!,:;])(?=[A-Za-z]))", lambda m: f"{m.group(1) or m.group(2)} ", cleaned)
    return collapse_whitespace(cleaned).strip()


def build_toc_level_map(
    book: epub.EpubBook,
    input_path: Path | None = None,
    backend: str = "yaet",
) -> dict[str, int]:
    if backend == "epub_translator":
        if input_path is None:
            raise ValueError("input_path is required for the epub_translator backend")
        return build_toc_level_map_from_epub_translator(input_path)

    levels: dict[str, int] = {}

    def register(item: object, depth: int) -> None:
        href = getattr(item, "href", None)
        if href:
            levels[posixpath.normpath(href)] = depth + 2

    def walk(items: Sequence[object], depth: int) -> None:
        for item in items:
            if isinstance(item, tuple):
                if not item:
                    continue
                register(item[0], depth)
                walk(list(item[1:]), depth + 1)
                continue
            if isinstance(item, list):
                walk(item, depth)
                continue
            register(item, depth)

    toc = book.toc if isinstance(book.toc, (list, tuple)) else [book.toc]
    walk(list(toc), depth=0)
    return levels


def lookup_toc_heading_level(
    document_href: str,
    toc_level_map: dict[str, int],
    element_anchor: str | None,
    inherited_anchor: str | None,
) -> int | None:
    for anchor in (element_anchor, inherited_anchor):
        if not anchor:
            continue
        href = posixpath.normpath(f"{document_href}#{anchor}")
        level = toc_level_map.get(href)
        if level is not None:
            return level
    return None


def convert_epub_to_markdown(
    input_path: Path,
    output_path: Path,
    asset_dir_name: str | None = None,
    backend: str = "yaet",
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    book = epub.read_epub(str(input_path))
    exporter = AssetExporter(book, output_path, asset_dir_name=asset_dir_name)
    slugger = Slugger()
    toc_level_map = build_toc_level_map(book, input_path=input_path, backend=backend)
    note_definitions = collect_note_definitions(book, exporter, input_path=input_path, backend=backend)
    used_notes: dict[str, str] = {}
    toc_entries: list[TocEntry] = []
    rendered_documents: list[str] = []
    book_title = primary_metadata_value(book.get_metadata("DC", "title")) or input_path.stem
    cover_image = detect_cover_image(book, exporter)
    book_title_slug = slugger.slugify(book_title)

    for item in iter_document_items(book, input_path=input_path, backend=backend):
        rendered = render_document(
            item,
            exporter,
            toc_entries,
            slugger,
            note_definitions,
            used_notes,
            book_title_slug,
            toc_level_map,
        )
        if rendered.strip():
            rendered_documents.append(rendered.strip())

    output = build_markdown_document(book_title, toc_entries, rendered_documents, used_notes, cover_image=cover_image)
    output_path.write_text(output, encoding="utf-8")


def detect_cover_image(book: epub.EpubBook, exporter: AssetExporter) -> str | None:
    cover_item_id = primary_metadata_value(book.get_metadata("OPF", "cover"))
    if cover_item_id:
        item = book.get_item_with_id(cover_item_id)
        cover_markdown = export_cover_item(item, exporter)
        if cover_markdown:
            return cover_markdown

    for item in book.get_items():
        item_id = getattr(item, "id", None) or item.get_id()
        href = getattr(item, "file_name", None) or item.get_name()
        properties = getattr(item, "properties", None) or []
        media_type = getattr(item, "media_type", None) or ""
        if not media_type.startswith("image/"):
            continue
        if "cover-image" in properties or "cover" in str(item_id).lower() or "cover" in str(href).lower():
            cover_markdown = export_cover_item(item, exporter)
            if cover_markdown:
                return cover_markdown

    return None


def export_cover_item(item: object | None, exporter: AssetExporter) -> str | None:
    if item is None:
        return None
    href = getattr(item, "file_name", None) or item.get_name()
    target = exporter.export_book_asset(href)
    if not target:
        return None
    return f"![]({target})"


def iter_document_items(
    book: epub.EpubBook,
    input_path: Path | None = None,
    backend: str = "yaet",
) -> list[epub.EpubHtml]:
    if backend == "epub_translator":
        if input_path is None:
            raise ValueError("input_path is required for the epub_translator backend")
        return iter_document_items_from_epub_translator(book, input_path)

    items: list[epub.EpubHtml] = []
    seen: set[str] = set()

    for spine_item in book.spine:
        item_id = spine_item[0] if isinstance(spine_item, tuple) else spine_item
        if item_id == "nav":
            continue
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        if item.get_name().endswith("nav.xhtml"):
            continue
        seen.add(item.get_id())
        items.append(item)

    for item in book.get_items():
        if item.get_type() != ITEM_DOCUMENT:
            continue
        if item.get_id() in seen or item.get_name().endswith("nav.xhtml"):
            continue
        items.append(item)

    return items


def render_document(
    item: epub.EpubHtml,
    exporter: AssetExporter,
    toc_entries: list[TocEntry],
    slugger: Slugger,
    note_definitions: dict[str, str],
    used_notes: dict[str, str],
    book_title_slug: str,
    toc_level_map: dict[str, int],
) -> str:
    root = ET.fromstring(item.get_content())
    body = root.find(".//{*}body")
    container = body if body is not None else root
    blocks = render_blocks(
        list(container),
        item.get_name(),
        exporter,
        toc_entries,
        slugger,
        note_definitions,
        used_notes,
        book_title_slug,
        toc_level_map,
    )
    return "\n\n".join(block for block in blocks if block.strip())


def render_blocks(
    elements: Iterable[ET.Element],
    document_href: str,
    exporter: AssetExporter,
    toc_entries: list[TocEntry],
    slugger: Slugger,
    note_definitions: dict[str, str],
    used_notes: dict[str, str],
    book_title_slug: str,
    toc_level_map: dict[str, int],
    current_anchor: str | None = None,
) -> list[str]:
    blocks: list[str] = []
    for element in elements:
        blocks.extend(
            render_block(
                element,
                document_href,
                exporter,
                toc_entries,
                slugger,
                note_definitions,
                used_notes,
                book_title_slug,
                toc_level_map,
                current_anchor,
            )
        )
    return blocks


def render_block(
    element: ET.Element,
    document_href: str,
    exporter: AssetExporter,
    toc_entries: list[TocEntry],
    slugger: Slugger,
    note_definitions: dict[str, str],
    used_notes: dict[str, str],
    book_title_slug: str,
    toc_level_map: dict[str, int],
    current_anchor: str | None = None,
) -> list[str]:
    tag = local_name(element.tag)
    next_anchor = element.get("id") or current_anchor

    class_heading_level = class_heading_level_for_element(element)
    if class_heading_level is not None:
        heading_level = (
            lookup_toc_heading_level(document_href, toc_level_map, element.get("id"), current_anchor) or class_heading_level
        )
        title = normalize_heading_text(render_inline(element, document_href, exporter, note_definitions, used_notes))
        if not title:
            return []
        anchor = slugger.slugify(title)
        if anchor == book_title_slug:
            return []
        toc_entries.append(TocEntry(level=heading_level, title=title, anchor=anchor))
        return [f"{'#' * heading_level} {title}"]

    if tag in {"section", "article", "div", "main", "body", "table", "thead", "tbody", "tfoot", "tr", "td", "th"}:
        return render_blocks(
            list(element),
            document_href,
            exporter,
            toc_entries,
            slugger,
            note_definitions,
            used_notes,
            book_title_slug,
            toc_level_map,
            next_anchor,
        )

    if should_descend_into_children(element):
        return render_blocks(
            list(element),
            document_href,
            exporter,
            toc_entries,
            slugger,
            note_definitions,
            used_notes,
            book_title_slug,
            toc_level_map,
            next_anchor,
        )

    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        title = normalize_heading_text(render_inline(element, document_href, exporter, note_definitions, used_notes))
        if not title:
            return []
        level = lookup_toc_heading_level(document_href, toc_level_map, element.get("id"), current_anchor) or int(tag[1])
        anchor = slugger.slugify(title)
        if anchor == book_title_slug:
            return []
        toc_entries.append(TocEntry(level=level, title=title, anchor=anchor))
        return [f"{'#' * level} {title}"]

    if tag == "p":
        paragraph = render_inline(element, document_href, exporter, note_definitions, used_notes).strip()
        return [paragraph] if paragraph else []

    if tag in {"ul", "ol"}:
        return [
            render_list(
                element,
                document_href,
                exporter,
                toc_entries,
                slugger,
                note_definitions,
                used_notes,
                book_title_slug,
                toc_level_map,
                current_anchor,
            )
        ]

    if tag == "blockquote":
        inner = render_blocks(
            list(element),
            document_href,
            exporter,
            toc_entries,
            slugger,
            note_definitions,
            used_notes,
            book_title_slug,
            toc_level_map,
            next_anchor,
        )
        if not inner:
            text = collapse_whitespace("".join(element.itertext()))
            inner = [text] if text else []
        quoted = []
        for block in inner:
            quoted.extend([f"> {line}" if line else ">" for line in block.splitlines()])
        return ["\n".join(quoted)] if quoted else []

    if tag == "pre":
        code = extract_text(element).strip("\n")
        return [f"```\n{code}\n```"] if code else []

    if tag == "hr":
        return ["---"]

    if tag == "img":
        image = render_image(element, document_href, exporter)
        return [image] if image else []

    fallback_text = collapse_whitespace("".join(element.itertext()))
    if fallback_text:
        return [fallback_text]
    return render_blocks(
        list(element),
        document_href,
        exporter,
        toc_entries,
        slugger,
        note_definitions,
        used_notes,
        book_title_slug,
        toc_level_map,
        next_anchor,
    )


def render_list(
    element: ET.Element,
    document_href: str,
    exporter: AssetExporter,
    toc_entries: list[TocEntry],
    slugger: Slugger,
    note_definitions: dict[str, str],
    used_notes: dict[str, str],
    book_title_slug: str,
    toc_level_map: dict[str, int],
    current_anchor: str | None = None,
    depth: int = 0,
) -> str:
    lines: list[str] = []
    is_ordered = local_name(element.tag) == "ol"

    for index, child in enumerate(element, start=1):
        if local_name(child.tag) != "li":
            continue
        prefix = f"{index}." if is_ordered else "-"
        indent = "  " * depth
        primary_text = render_list_item_text(child, document_href, exporter, note_definitions, used_notes)
        if primary_text:
            lines.append(f"{indent}{prefix} {primary_text}")

        for nested in child:
            nested_tag = local_name(nested.tag)
            if nested_tag in {"ul", "ol"}:
                lines.append(
                    render_list(
                        nested,
                        document_href,
                        exporter,
                        toc_entries,
                        slugger,
                        note_definitions,
                        used_notes,
                        book_title_slug,
                        toc_level_map,
                        current_anchor,
                        depth + 1,
                    )
                )
            elif nested_tag in {"blockquote", "pre"}:
                nested_blocks = render_block(
                    nested,
                    document_href,
                    exporter,
                    toc_entries,
                    slugger,
                    note_definitions,
                    used_notes,
                    book_title_slug,
                    toc_level_map,
                    current_anchor,
                )
                for block in nested_blocks:
                    for line in block.splitlines():
                        lines.append(f"{indent}  {line}" if line else "")

    return "\n".join(line for line in lines if line.strip())


def render_list_item_text(
    element: ET.Element,
    document_href: str,
    exporter: AssetExporter,
    note_definitions: dict[str, str],
    used_notes: dict[str, str],
) -> str:
    parts: list[str] = []
    if element.text:
        parts.append(collapse_whitespace(element.text))

    for child in element:
        tag = local_name(child.tag)
        if tag in {"ul", "ol", "blockquote", "pre"}:
            continue
        rendered = render_inline_node(child, document_href, exporter, note_definitions, used_notes)
        if rendered:
            parts.append(rendered)
        if child.tail:
            parts.append(collapse_whitespace(child.tail))

    return "".join(parts).strip()


def render_inline(
    element: ET.Element,
    document_href: str,
    exporter: AssetExporter,
    note_definitions: dict[str, str],
    used_notes: dict[str, str],
) -> str:
    parts: list[str] = []
    if element.text:
        parts.append(collapse_whitespace(element.text))
    for child in element:
        rendered = render_inline_node(child, document_href, exporter, note_definitions, used_notes)
        if rendered:
            parts.append(rendered)
        if child.tail:
            parts.append(collapse_whitespace(child.tail))
    return "".join(parts).strip()


def render_inline_node(
    element: ET.Element,
    document_href: str,
    exporter: AssetExporter,
    note_definitions: dict[str, str],
    used_notes: dict[str, str],
) -> str:
    tag = local_name(element.tag)
    text = render_inline(element, document_href, exporter, note_definitions, used_notes)
    raw_text = collapse_whitespace(extract_text(element))

    if tag in {"strong", "b"}:
        if raw_text and not re.search(r"\w", raw_text):
            return raw_text
        return f"**{text}**" if text else ""
    if tag in {"em", "i"}:
        if raw_text and not re.search(r"\w", raw_text):
            return raw_text
        return f"*{text}*" if text else ""
    if tag == "code":
        return f"`{text}`" if text else ""
    if tag == "a":
        href = element.get("href")
        label = text or href or ""
        note_key = resolve_note_key(document_href, href)
        if note_key and note_key in note_definitions:
            used_notes.setdefault(note_key, note_definitions[note_key])
            return f"[{label}][{note_key}]"
        if href and is_internal_book_link(href):
            return label
        if href:
            return f"[{label}]({href})"
        return label
    if tag == "img":
        return render_image(element, document_href, exporter) or ""
    if tag == "br":
        return "  \n"
    if tag in {"span", "sup", "sub"}:
        return text
    return text


def render_image(element: ET.Element, document_href: str, exporter: AssetExporter) -> str | None:
    source = element.get("src")
    target = exporter.export(document_href, source)
    if not target:
        return None
    alt = collapse_whitespace(element.get("alt", ""))
    return f"![{alt}]({target})"


def build_markdown_document(
    book_title: str,
    toc_entries: Sequence[TocEntry],
    rendered_documents: Sequence[str],
    used_notes: dict[str, str],
    cover_image: str | None = None,
) -> str:
    parts: list[str] = []
    title_anchor = Slugger().slugify(book_title)
    parts.append("# Table of Contents")
    toc_lines = [f"- [{book_title}](#{title_anchor})"]
    if toc_entries:
        toc_lines.append(render_toc(toc_entries))
    parts.append("\n".join(line for line in toc_lines if line.strip()))
    parts.append(f"# {book_title}")
    if cover_image and not any(cover_image in document for document in rendered_documents):
        parts.append(cover_image)
    parts.extend(rendered_documents)
    if used_notes:
        parts.append(render_note_definitions(used_notes))
    return "\n\n".join(part for part in parts if part.strip()) + "\n"


def render_note_definitions(used_notes: dict[str, str]) -> str:
    return "\n".join(f"[{key}]: {value}" for key, value in used_notes.items())


def render_toc(entries: Sequence[TocEntry]) -> str:
    filtered = [entry for entry in entries if entry.level > 1]
    if not filtered:
        return ""
    min_level = min(entry.level for entry in filtered)
    lines: list[str] = []
    for entry in filtered:
        indent = "  " * (entry.level - min_level)
        lines.append(f"{indent}- [{normalize_multiline_title(entry.title)}](#{entry.anchor})")
    return "\n".join(lines)


def normalize_multiline_title(text: str) -> str:
    return collapse_whitespace(text)


def collect_note_definitions(
    book: epub.EpubBook,
    exporter: AssetExporter,
    input_path: Path | None = None,
    backend: str = "yaet",
) -> dict[str, str]:
    note_definitions: dict[str, str] = {}
    for item in iter_document_items(book, input_path=input_path, backend=backend):
        root = ET.fromstring(item.get_content())
        for anchor in root.findall(".//{*}a[@id]"):
            note_id = anchor.get("id")
            if not note_id:
                continue
            paragraph = find_ancestor_paragraph(root, anchor)
            if paragraph is None:
                continue
            key = f"{item.get_name()}#{note_id}"
            note_definitions[key] = render_note_definition(paragraph, item.get_name(), exporter)
    return note_definitions


def find_ancestor_paragraph(root: ET.Element, target: ET.Element) -> ET.Element | None:
    for paragraph in root.findall(".//{*}p"):
        for child in paragraph.iter():
            if child is target:
                return paragraph
    return None


def render_note_definition(paragraph: ET.Element, document_href: str, exporter: AssetExporter) -> str:
    text = collapse_whitespace("".join(paragraph.itertext())).strip()
    return re.sub(r"^(\d+)\s+\.", r"\1.", text)


def resolve_note_key(document_href: str, href: str | None) -> str | None:
    if not href or "#" not in href:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document_href), href))
    if ".html#" not in resolved and ".xhtml#" not in resolved:
        return None
    return resolved


def primary_metadata_value(entries: Sequence[tuple[str, dict[str, str]]]) -> str | None:
    if not entries:
        return None
    return entries[0][0].strip() or None


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def is_internal_book_link(href: str) -> bool:
    return href.startswith("#") or ".xhtml#" in href or href.endswith(".xhtml")


def extract_text(element: ET.Element) -> str:
    return "".join(element.itertext())


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def iter_document_items_from_epub_translator(book: epub.EpubBook, input_path: Path) -> list[epub.EpubHtml]:
    document_items = {
        posixpath.normpath(item.get_name()): item
        for item in book.get_items()
        if item.get_type() == ITEM_DOCUMENT and not item.get_name().endswith("nav.xhtml")
    }
    items: list[epub.EpubHtml] = []
    seen: set[str] = set()

    for href in read_epub_translator_spine_paths(input_path):
        item = document_items.get(href)
        if item is None:
            continue
        seen.add(href)
        items.append(item)

    for href, item in document_items.items():
        if href in seen:
            continue
        items.append(item)

    return items


def build_toc_level_map_from_epub_translator(input_path: Path) -> dict[str, int]:
    with zipfile.ZipFile(input_path, "r") as archive:
        opf_path = _find_opf_path(archive)
        version = _detect_epub_version(archive, opf_path)
        toc_path = _find_toc_path(archive, opf_path, version)
        if toc_path is None:
            return {}
        root = ET.fromstring(archive.read(toc_path.as_posix()))
        _strip_namespace(root)
        toc_items = _read_nav_toc(root) if version == 3 else _read_ncx_toc(root)

    levels: dict[str, int] = {}

    def walk(items: Sequence[_TocItem], depth: int) -> None:
        for item in items:
            if item.full_href is not None:
                resolved = posixpath.normpath(posixpath.join(toc_path.parent.as_posix(), item.full_href))
                levels[resolved] = depth + 2
            walk(item.children, depth + 1)

    walk(toc_items, 0)
    return levels


def read_epub_translator_spine_paths(input_path: Path) -> list[str]:
    with zipfile.ZipFile(input_path, "r") as archive:
        opf_path = _find_opf_path(archive)
        root = ET.fromstring(archive.read(opf_path.as_posix()))
        _strip_namespace(root)

        manifest = root.find(".//manifest")
        if manifest is None:
            return []

        manifest_items: dict[str, tuple[str, str]] = {}
        for item in manifest.findall("item"):
            item_id = item.get("id")
            item_href = item.get("href")
            media_type = item.get("media-type", "")
            if item_id and item_href:
                manifest_items[item_id] = (item_href, media_type)

        spine = root.find(".//spine")
        if spine is None:
            return []

        paths: list[str] = []
        for itemref in spine.findall("itemref"):
            idref = itemref.get("idref")
            if not idref or idref not in manifest_items:
                continue
            href, media_type = manifest_items[idref]
            if media_type not in {"application/xhtml+xml", "text/html"}:
                continue
            paths.append(posixpath.normpath(posixpath.join(opf_path.parent.as_posix(), href)))
        return paths


@dataclass(frozen=True)
class _TocItem:
    title: str
    href: str | None = None
    fragment: str | None = None
    children: tuple["_TocItem", ...] = ()

    @property
    def full_href(self) -> str | None:
        if self.href is None:
            return None
        if self.fragment:
            return f"{self.href}#{self.fragment}"
        return self.href


def _find_opf_path(archive: zipfile.ZipFile) -> PurePosixPath:
    root = ET.fromstring(archive.read("META-INF/container.xml"))
    namespace = {"ns": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = root.find(".//ns:rootfile", namespace)
    if rootfile is None:
        rootfile = root.find(".//rootfile")
    if rootfile is None or rootfile.get("full-path") is None:
        raise ValueError("Cannot find OPF path in EPUB container.xml")
    return PurePosixPath(rootfile.get("full-path"))


def _strip_namespace(element: ET.Element) -> None:
    if element.tag.startswith("{"):
        element.tag = element.tag.split("}", 1)[1]
    for child in element:
        _strip_namespace(child)


def _detect_epub_version(archive: zipfile.ZipFile, opf_path: PurePosixPath) -> int:
    root = ET.fromstring(archive.read(opf_path.as_posix()))
    version = root.get("version", "2.0")
    return 3 if version.startswith("3") else 2


def _find_toc_path(archive: zipfile.ZipFile, opf_path: PurePosixPath, version: int) -> PurePosixPath | None:
    root = ET.fromstring(archive.read(opf_path.as_posix()))
    _strip_namespace(root)
    manifest = root.find(".//manifest")
    if manifest is None:
        return None

    if version == 2:
        for item in manifest.findall("item"):
            if item.get("media-type") == "application/x-dtbncx+xml" and item.get("href"):
                return opf_path.parent / item.get("href")
        return None

    for item in manifest.findall("item"):
        properties = item.get("properties", "")
        if "nav" in properties.split() and item.get("href"):
            return opf_path.parent / item.get("href")
    return None


def _read_ncx_toc(root: ET.Element) -> list[_TocItem]:
    nav_map = root.find(".//navMap")
    if nav_map is None:
        return []
    items: list[_TocItem] = []
    for nav_point in nav_map.findall("navPoint"):
        parsed = _parse_nav_point(nav_point)
        if parsed is not None:
            items.append(parsed)
    return items


def _parse_nav_point(nav_point: ET.Element) -> _TocItem | None:
    text_elem = nav_point.find("navLabel/text")
    if text_elem is None:
        return None
    title = collapse_whitespace("".join(text_elem.itertext())).strip()
    if not title:
        return None
    href = None
    fragment = None
    content = nav_point.find("content")
    if content is not None and content.get("src"):
        href, fragment = _split_href(content.get("src"))
    children = tuple(
        child
        for child in (_parse_nav_point(child_nav) for child_nav in nav_point.findall("navPoint"))
        if child is not None
    )
    return _TocItem(title=title, href=href, fragment=fragment, children=children)


def _read_nav_toc(root: ET.Element) -> list[_TocItem]:
    nav_elem = None
    for nav in root.findall(".//nav"):
        if any(value == "toc" for key, value in nav.attrib.items() if key.endswith("type")):
            nav_elem = nav
            break
    if nav_elem is None:
        return []

    ol = nav_elem.find(".//ol")
    if ol is None:
        return []

    items: list[_TocItem] = []
    for li in ol.findall("li"):
        parsed = _parse_nav_li(li)
        if parsed is not None:
            items.append(parsed)
    return items


def _parse_nav_li(li: ET.Element) -> _TocItem | None:
    anchor = li.find("a")
    href = None
    fragment = None
    if anchor is not None:
        title = collapse_whitespace("".join(anchor.itertext())).strip()
        if anchor.get("href"):
            href, fragment = _split_href(anchor.get("href"))
    else:
        span = li.find("span")
        if span is None:
            return None
        title = collapse_whitespace("".join(span.itertext())).strip()

    if not title:
        return None

    children = tuple(
        child
        for child in (_parse_nav_li(child_li) for child_li in li.findall("ol/li"))
        if child is not None
    )
    return _TocItem(title=title, href=href, fragment=fragment, children=children)


def _split_href(href: str) -> tuple[str | None, str | None]:
    if "#" not in href:
        return href, None
    document, fragment = href.split("#", 1)
    return document or None, fragment or None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert an EPUB file to Markdown.")
    parser.add_argument("input_path", help="Path to the source EPUB file.")
    parser.add_argument("-o", "--output", dest="output_path", help="Path to the output Markdown file.")
    parser.add_argument("--backend", choices=("yaet", "epub_translator"), default="yaet")
    parser.add_argument(
        "--asset-dir-name",
        default=None,
        help="Optional directory name for exported assets. Defaults to <output_stem>_assets.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input_path)
    output_path = (
        Path(args.output_path)
        if args.output_path
        else book_output_dir(input_path) / f"{sanitize_book_dir_name(canonical_book_name(input_path))}.md"
    )
    if args.backend == "yaet":
        convert_epub_to_markdown(input_path, output_path, asset_dir_name=args.asset_dir_name)
    else:
        convert_epub_to_markdown(input_path, output_path, asset_dir_name=args.asset_dir_name, backend=args.backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
