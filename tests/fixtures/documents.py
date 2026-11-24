"""Synthetic test documents built in code (no real files are committed; SECURITY.md)."""

from __future__ import annotations

import io
import zipfile


def minimal_pdf(lines: list[str], *, title: str | None = None) -> bytes:
    """A tiny valid PDF with one page of Helvetica text (no external tools)."""
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({safe}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    info = f"<< /Title ({title}) >>" if title else "<< >>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        info.encode("latin-1"),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref)
    )
    return out.getvalue()


def blank_pdf() -> bytes:
    """A valid PDF with no text at all (like a scanned image)."""
    return minimal_pdf([])


def docx_bytes(build: list[tuple[str, str]], *, title: str | None = None) -> bytes:
    """build: [("h1", "Heading"), ("p", "Paragraph"), ("h2", "Sub"), ...]"""
    import docx

    document = docx.Document()
    if title:
        document.core_properties.title = title
    for kind, text in build:
        if kind.startswith("h"):
            document.add_heading(text, level=int(kind[1]))
        else:
            document.add_paragraph(text)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def zip_bomb_docx() -> bytes:
    """Looks like a .docx (right entries) but one entry expands ~1000x."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("word/padding.bin", b"\x00" * (20 * 1024 * 1024))
    return out.getvalue()
