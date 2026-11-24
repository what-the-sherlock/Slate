import pytest

from core.adapters.parsers.formats import FormatParser
from core.domain.errors import ValidationFailed
from core.domain.uploads import MIME, FileKind
from tests.fixtures.documents import blank_pdf, docx_bytes, minimal_pdf, zip_bomb_docx

P = FormatParser()


def parse(kind: FileKind, data: bytes):
    return P.parse(data, MIME[kind])


def test_pdf_text_by_page() -> None:
    doc = parse(FileKind.PDF, minimal_pdf(["Vacation policy", "25 days a year"], title="Handbook"))
    assert doc.title == "Handbook"
    assert doc.sections[0].heading_path == "Page 1"
    assert "25 days a year" in doc.sections[0].text


def test_pdf_without_text_explains_ocr() -> None:
    with pytest.raises(ValidationFailed, match="OCR"):
        parse(FileKind.PDF, blank_pdf())


def test_corrupt_pdf_is_a_validation_error() -> None:
    with pytest.raises(ValidationFailed, match="can't be opened"):
        parse(FileKind.PDF, b"%PDF-1.7\nthis is not really a pdf")


def test_docx_headings_become_heading_paths() -> None:
    data = docx_bytes(
        [("h1", "Policies"), ("p", "Intro text."), ("h2", "Leave"), ("p", "25 days.")],
        title="Handbook",
    )
    doc = parse(FileKind.DOCX, data)
    assert doc.title == "Handbook"
    paths = {s.heading_path: s.text for s in doc.sections}
    assert paths["Policies"] == "Intro text."
    assert paths["Policies > Leave"] == "25 days."


def test_docx_zip_bomb_is_rejected_before_decompression() -> None:
    with pytest.raises(ValidationFailed, match="unsafe size"):
        parse(FileKind.DOCX, zip_bomb_docx())


def test_plain_zip_is_not_a_docx() -> None:
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("readme.txt", "hi")
    with pytest.raises(ValidationFailed, match="isn't a Word"):
        parse(FileKind.DOCX, out.getvalue())


def test_markdown_sections_follow_headings_and_keep_code() -> None:
    md = b"# Guide\n\nIntro.\n\n## Install\n\n```bash\nuv sync\n```\n\n## Run\n\nStart it.\n"
    doc = parse(FileKind.MARKDOWN, md)
    assert doc.title == "Guide"
    by_path = {s.heading_path: s.text for s in doc.sections}
    assert by_path["Guide"] == "Intro."
    assert "uv sync" in by_path["Guide > Install"]
    assert by_path["Guide > Run"] == "Start it."


def test_html_drops_scripts_and_keeps_structure() -> None:
    html = (
        b"<html><head><title>Docs</title><script>steal()</script></head><body>"
        b"<nav>menu</nav><h1>Setup</h1><p>Install <b>uv</b>.</p><ul><li>Step one</li></ul>"
        b"<h2>Config</h2><p>Set ENV.</p></body></html>"
    )
    doc = parse(FileKind.HTML, html)
    assert doc.title == "Docs"
    text = " ".join(s.text for s in doc.sections)
    assert "steal" not in text
    assert "menu" not in text
    by_path = {s.heading_path: s.text for s in doc.sections}
    assert "Install uv ." in by_path["Setup"] or "Install uv." in by_path["Setup"]
    assert by_path["Setup > Config"] == "Set ENV."


def test_csv_rows_carry_their_column_names() -> None:
    doc = parse(FileKind.CSV, b"name,role\nAda,Owner\nGrace,Admin\n")
    assert doc.sections[0].text.split("\n\n") == [
        "name: Ada; role: Owner",
        "name: Grace; role: Admin",
    ]


@pytest.mark.parametrize("kind", [FileKind.TEXT, FileKind.CSV, FileKind.MARKDOWN, FileKind.HTML])
def test_text_formats_reject_invalid_utf8_anywhere_in_the_file(kind: FileKind) -> None:
    data = b"fine start " * 2000 + b"\xff\xfe broken"  # past the 8 KB sniff window
    with pytest.raises(ValidationFailed, match="UTF-8"):
        parse(kind, data)


def test_text_strips_bom_and_normalises_whitespace() -> None:
    doc = parse(FileKind.TEXT, "﻿Hello\t\tworld\n\n\n\nBye".encode())
    assert doc.sections[0].text == "Hello world\n\nBye"


def test_unknown_mime_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        P.parse(b"x", "application/x-msdownload")
