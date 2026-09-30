"""
Extract plain text from uploaded documents.

Supports: .pdf, .docx, .odt, .txt, .md, .csv, .rtf, .html
Returns "" for unsupported or unreadable files — never raises.
"""
import io
import logging
import os

logger = logging.getLogger(__name__)

# Cap extracted text so a 500-page PDF doesn't blow up your DB or OpenAI bill
MAX_CHARS = 200_000


def extract_text(file_field, max_chars=MAX_CHARS):
    """
    Read a Django FileField and return its plain text content.
    Always returns a string. Never raises — safe to call in a view.
    """
    if not file_field:
        return ""

    name = (file_field.name or "").lower()
    ext = os.path.splitext(name)[1]

    # Read the raw bytes. This pulls the file from Cloudinary (or local disk).
    try:
        file_field.open("rb")
        raw = file_field.read()
    except Exception:
        logger.exception("Could not open/read file %s", name)
        return ""
    finally:
        try:
            file_field.close()
        except Exception:
            pass

    if not raw:
        return ""

    text = ""
    try:
        if ext == ".pdf":
            text = _extract_pdf(raw)
        elif ext == ".docx":
            text = _extract_docx(raw)
        elif ext == ".odt":
            text = _extract_odt(raw)
        elif ext in {".txt", ".md", ".csv", ".rtf"}:
            text = _decode(raw)
        elif ext in {".html", ".htm"}:
            text = _extract_html(raw)
        else:
            logger.info("No extractor for %s — skipping content_text.", ext)
    except Exception:
        logger.exception("Text extraction failed for %s", name)
        return ""

    return text[:max_chars].strip()


# ─── Format-specific extractors ─────────────────────────────────────

def _extract_pdf(raw: bytes) -> str:
    """Uses pypdf. Returns "" for scanned/image-only PDFs (no OCR here)."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            # One malformed page shouldn't kill the whole document
            continue
    return "\n".join(parts)


def _extract_docx(raw: bytes) -> str:
    """python-docx handles paragraphs + tables."""
    from docx import Document as DocxDocument
    doc = DocxDocument(io.BytesIO(raw))
    parts = [p.text for p in doc.paragraphs if p.text]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return "\n".join(parts)


def _extract_odt(raw: bytes) -> str:
    """odfpy handles the ODT zip format. teletype.extractText walks text nodes."""
    from odf.opendocument import load
    from odf import text, teletype

    doc = load(io.BytesIO(raw))
    paragraphs = doc.getElementsByType(text.P)
    return "\n".join(teletype.extractText(p) for p in paragraphs)


def _extract_html(raw: bytes) -> str:
    """Strip tags with the stdlib HTML parser."""
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self._skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self._skip += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self._skip:
                self._skip -= 1

        def handle_data(self, data):
            if not self._skip:
                self.parts.append(data)

    stripped = _Stripper()
    stripped.feed(_decode(raw))
    return " ".join(stripped.parts)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""
