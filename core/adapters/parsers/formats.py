"""Document parsers: bytes -> ParsedDocument (sections with heading paths).

Every input is untrusted (SECURITY.md). Each parser raises ValidationFailed with a
user-facing message for unreadable input, never a raw library exception. Parsing is
CPU-bound and synchronous; it runs only in the worker.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from core.domain.documents import ParsedDocument, Section
from core.domain.errors import ValidationFailed
from core.domain.uploads import FileKind

if TYPE_CHECKING:
    from markdown_it.token import Token

MAX_DOCX_UNCOMPRESSED = 100 * 1024 * 1024
MAX_DOCX_RATIO = 100  # zip bombs compress far better than real documents
MAX_CSV_ROWS = 50_000
MAX_PDF_PAGES = 2_000
MAX_TEXT_CHARS = 5_000_000  # extracted text per document (cost + memory bound)
MAX_HTML_ELEMENTS = 100_000
MAX_HTML_DEPTH = 200
_WS = re.compile(r"[ \t\f\v]+")


def decode_text(data: bytes) -> str:
    """Strict UTF-8 for the *whole* file (the 8 KB sniff was only an early check).
    Also bounds the text size (cost + memory), like the PDF parser.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValidationFailed("This file isn't valid UTF-8 text.") from e
    if "\x00" in text:
        raise ValidationFailed("This file contains binary data, not text.")
    if len(text) > MAX_TEXT_CHARS:
        raise ValidationFailed("This file contains too much text to index.")
    return text


def _clean(text: str) -> str:
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# ---------------------------------------------------------------- PDF


def parse_pdf(data: bytes) -> ParsedDocument:
    # Local import: heavy native module, worker-only. The package ships no type stubs.
    import pypdfium2 as pdfium  # pyright: ignore[reportMissingTypeStubs]

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ValidationFailed("This PDF can't be opened. It may be damaged or encrypted.") from e
    try:
        if len(pdf) > MAX_PDF_PAGES:
            raise ValidationFailed(f"This PDF has more than {MAX_PDF_PAGES:,} pages.")
        sections: list[Section] = []
        chars = 0
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            text = _clean(textpage.get_text_range())
            textpage.close()
            page.close()
            chars += len(text)
            if chars > MAX_TEXT_CHARS:
                raise ValidationFailed("This PDF contains too much text to index.")
            if text:
                sections.append(Section(f"Page {index + 1}", text))
        title = (pdf.get_metadata_dict().get("Title") or "").strip() or None
    finally:
        pdf.close()
    if not sections:
        raise ValidationFailed(
            "No extractable text in this PDF. It may be a scan; OCR isn't supported yet."
        )
    return ParsedDocument(title=title, sections=tuple(sections))


# ---------------------------------------------------------------- DOCX


def _check_docx_container(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ValidationFailed("This isn't a Word (.docx) document.")
            total = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile as e:
        raise ValidationFailed("This Word document is damaged.") from e
    if total > MAX_DOCX_UNCOMPRESSED or total > MAX_DOCX_RATIO * max(len(data), 1):
        raise ValidationFailed("This Word document expands to an unsafe size.")


def parse_docx(data: bytes) -> ParsedDocument:
    _check_docx_container(data)  # before python-docx decompresses anything
    import docx  # local import: worker-only

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:  # python-docx raises many unrelated types for bad input
        raise ValidationFailed("This Word document can't be read.") from e
    sections: list[Section] = []
    path: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            sections.append(Section(" > ".join(path) or None, "\n\n".join(buffer)))
            buffer.clear()

    for para in document.paragraphs:
        text = para.text.strip()
        style = (para.style.name if para.style is not None else "") or ""
        level = _heading_level(style)
        if level and text:
            flush()
            del path[level - 1 :]
            path.append(text)
        elif text:
            buffer.append(text)
    for table in document.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        if rows:
            buffer.append("\n".join(rows))
    flush()
    title = (document.core_properties.title or "").strip() or None
    return ParsedDocument(title=title, sections=tuple(s for s in sections if s.text.strip()))


def _heading_level(style: str) -> int:
    match = re.match(r"(?i)heading\s*(\d)", style)
    if match:
        return int(match.group(1))
    return 1 if style.lower() == "title" else 0


# ---------------------------------------------------------------- Markdown


def parse_markdown(data: bytes) -> ParsedDocument:
    from markdown_it import MarkdownIt

    text = decode_text(data)
    lines = text.splitlines()
    tokens = MarkdownIt("commonmark").parse(text)
    sections: list[Section] = []
    path: list[str] = []
    buffer: list[str] = []
    title: str | None = None

    def flush() -> None:
        if buffer:
            sections.append(Section(" > ".join(path) or None, "\n\n".join(buffer)))
            buffer.clear()

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open" and tok.level == 0:
            level = int(tok.tag[1])
            heading = tokens[i + 1].content.strip()
            flush()
            del path[level - 1 :]
            path.append(heading)
            title = title or (heading if level == 1 else None)
            i += 3
            continue
        if tok.level == 0 and tok.map and tok.nesting in (0, 1):
            start, end = tok.map
            block = "\n".join(lines[start:end]).strip()
            if block:
                buffer.append(block)
            i = _skip_block(tokens, i)
            continue
        i += 1
    flush()
    return ParsedDocument(title=title, sections=tuple(sections))


def _skip_block(tokens: Sequence[Token], i: int) -> int:
    """Skip to the token after this top-level block (its closing token)."""
    depth = 0
    while i < len(tokens):
        depth += tokens[i].nesting
        i += 1
        if depth <= 0:
            return i
    return i


# ---------------------------------------------------------------- HTML


def parse_html(data: bytes) -> ParsedDocument:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(decode_text(data), "html.parser")
    _check_html_tree(soup)
    for tag in soup(["script", "style", "noscript", "template", "nav", "footer", "iframe"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else None
    sections: list[Section] = []
    path: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            sections.append(Section(" > ".join(path) or None, "\n\n".join(buffer)))
            buffer.clear()

    blocks = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "td", "th"]
    for el in soup.find_all(blocks):
        if el.find_parent(["p", "li", "pre", "blockquote", "td", "th"]):
            continue  # already included via its parent block
        text = _clean(el.get_text(" ", strip=True))
        if not text:
            continue
        if el.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(el.name[1])
            flush()
            del path[level - 1 :]
            path.append(text)
        else:
            buffer.append(text)
    flush()
    return ParsedDocument(title=title or None, sections=tuple(sections))


def _check_html_tree(soup: object) -> None:
    """Bound work before walking the tree (security review: quadratic ancestor walks)."""
    from itertools import islice

    from bs4 import BeautifulSoup

    assert isinstance(soup, BeautifulSoup)
    for count, el in enumerate(soup.find_all(True), start=1):
        if count > MAX_HTML_ELEMENTS:
            raise ValidationFailed("This HTML page is too large to index.")
        if sum(1 for _ in islice(el.parents, MAX_HTML_DEPTH + 1)) > MAX_HTML_DEPTH:
            raise ValidationFailed("This HTML page is nested too deeply to index.")


# ---------------------------------------------------------------- text and CSV


def parse_text(data: bytes) -> ParsedDocument:
    text = _clean(decode_text(data))
    return ParsedDocument(title=None, sections=(Section(None, text),) if text else ())


def parse_csv(data: bytes) -> ParsedDocument:
    """Each row becomes a paragraph of "column: value" pairs, so a retrieved chunk is
    understandable without the header row.
    """
    text = decode_text(data)
    try:
        reader = csv.reader(io.StringIO(text))
        header: list[str] = next(reader, None) or []
        rows: list[str] = []
        for n, row in enumerate(reader):
            if n >= MAX_CSV_ROWS:
                break
            pairs = [
                f"{h.strip() or f'col{i + 1}'}: {v.strip()}"
                for i, (h, v) in enumerate(zip(header, row, strict=False))
                if v.strip()
            ]
            if pairs:
                rows.append("; ".join(pairs))
    except csv.Error as e:
        raise ValidationFailed("This CSV file can't be read.") from e
    return ParsedDocument(title=None, sections=(Section(None, "\n\n".join(rows)),) if rows else ())


_PARSERS: dict[FileKind, Callable[[bytes], ParsedDocument]] = {
    FileKind.PDF: parse_pdf,
    FileKind.DOCX: parse_docx,
    FileKind.MARKDOWN: parse_markdown,
    FileKind.HTML: parse_html,
    FileKind.TEXT: parse_text,
    FileKind.CSV: parse_csv,
}


class FormatParser:
    """Parser port implementation dispatching on the verified FileKind."""

    def supports(self, mime: str) -> bool:
        return any(mime == m for m in _MIME_TO_KIND)

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        kind = _MIME_TO_KIND.get(mime)
        if kind is None:
            raise ValidationFailed("This file type isn't supported.")
        return _PARSERS[kind](data)


def _mime_map() -> dict[str, FileKind]:
    from core.domain.uploads import MIME

    return {m: k for k, m in MIME.items()}


_MIME_TO_KIND = _mime_map()
