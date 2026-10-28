# utils/text_extraction.py
"""
Document text extraction for CampusOS.

Supports: .pdf, .docx, .doc, .odt, .rtf, .pptx, .ppt,
          .xlsx, .xls, .csv, .txt, .md

Every extractor returns (full_text, pages) where pages is either None
or a list of {page: int, text: str}. For formats without native page
boundaries, we group content into virtual pages of ~DOCX_CHARS_PER_PAGE
chars so downstream code (correction reports, page citations) works
uniformly.

Old Word binary format (.doc) is converted via LibreOffice headless
if `soffice` is on the PATH. If it isn't, the extractor logs a warning
and returns empty text — the caller will see the "no text extracted"
warning on upload.
"""
import csv
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Virtual page size for formats with no native pages (DOCX, ODT, TXT, etc.)
DOCX_CHARS_PER_PAGE = 3000

# Timeout for LibreOffice conversions
SOFFICE_TIMEOUT = 45


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════

def extract_text_and_pages(file_field):
    """
    Return (full_text: str, pages: list | None).

    `pages` is None when the format has no page structure or when
    extraction found nothing. Otherwise it's a list of
    {"page": int, "text": str} ordered by page.
    """
    if not file_field:
        return "", None

    name = getattr(file_field, "name", "") or ""
    ext = os.path.splitext(name)[1].lower()

    # Read raw bytes once
    try:
        file_field.open("rb")
        raw = file_field.read()
    except Exception as exc:
        logger.warning("Could not open file for extraction: %s", exc)
        return "", None
    finally:
        try:
            file_field.close()
        except Exception:
            pass

    if not raw:
        logger.warning("Empty file body for %s", name)
        return "", None

    # Dispatch
    try:
        handler = _HANDLERS.get(ext)
        if handler is None:
            logger.warning(
                "No extraction handler for extension %r (file: %s). "
                "Returning empty text.", ext, name,
            )
            return "", None

        text, pages = handler(raw)

        # Sanity warning if a handler returned nothing useful
        if not text or len(text.strip()) < 50:
            logger.warning(
                "Extraction produced %d chars for %s (ext=%s). "
                "This document may not be AI-evaluable.",
                len(text or ""), name, ext,
            )

        return text or "", pages

    except Exception as exc:
        logger.exception("Text extraction failed for %s: %s", name, exc)
        return "", None


def extract_text(file_field):
    """Backwards-compatible wrapper returning only the flat text."""
    text, _ = extract_text_and_pages(file_field)
    return text


# ═══════════════════════════════════════════════════════════════════════
# Format handlers
# ═══════════════════════════════════════════════════════════════════════

def _extract_pdf(raw_bytes):
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF (fitz) not installed; skipping PDF extraction.")
        return "", None

    doc = fitz.open(stream=raw_bytes, filetype="pdf")
    try:
        pages = []
        full_chunks = []
        for i, page in enumerate(doc):
            try:
                text = (page.get_text("text") or "").strip()
            except Exception as exc:
                logger.warning("Failed to read PDF page %s: %s", i + 1, exc)
                text = ""
            if text:
                pages.append({"page": i + 1, "text": text})
                full_chunks.append(text)

        # Scanned PDF with no embedded text layer → try OCR if available
        if not full_chunks:
            logger.info("PDF has no text layer; treating as scanned document.")
            # Optional OCR hook: plug in pytesseract / ocrmypdf later.

        return "\n\n".join(full_chunks), pages or None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _extract_docx_with_pages(raw_bytes):
    """
    Return (full_text, pages) for a DOCX.

    `pages` is a list of {page, text} built by accumulating paragraphs
    into virtual pages of ~DOCX_CHARS_PER_PAGE chars. Table rows are
    included as paragraphs too — students often put methodology tables
    in DOCX.
    """
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx not installed; skipping DOCX extraction.")
        return "", None

    doc = Document(io.BytesIO(raw_bytes))
    paragraphs = []

    for para in doc.paragraphs:
        t = (para.text or "").strip()
        if t:
            paragraphs.append(t)

    for table in doc.tables:
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            row_text = " | ".join(c for c in cells if c)
            if row_text:
                paragraphs.append(row_text)

    if not paragraphs:
        return "", None

    pages = _chunk_into_pages(paragraphs)
    full_text = "\n\n".join(paragraphs)
    return full_text, pages


def _extract_doc(raw_bytes):
    """
    Old Word binary format (.doc). Requires LibreOffice (soffice) on PATH.
    Converts to .docx first, then delegates to the DOCX extractor.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        logger.warning(
            "LibreOffice not found on PATH; cannot extract .doc files. "
            "Install with: sudo apt install libreoffice-core --no-install-recommends"
        )
        return "", None

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input.doc")
        with open(src, "wb") as f:
            f.write(raw_bytes)

        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "docx",
                 "--outdir", tmp, src],
                check=True,
                timeout=SOFFICE_TIMEOUT,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "LibreOffice .doc→.docx conversion failed: %s",
                (exc.stderr or b"").decode(errors="replace")[:200],
            )
            return "", None
        except subprocess.TimeoutExpired:
            logger.warning("LibreOffice .doc conversion timed out")
            return "", None

        converted = os.path.join(tmp, "input.docx")
        if not os.path.exists(converted):
            logger.warning("LibreOffice did not produce input.docx")
            return "", None

        with open(converted, "rb") as f:
            docx_bytes = f.read()

    return _extract_docx_with_pages(docx_bytes)


def _extract_odt(raw_bytes):
    """
    OpenDocument Text (.odt) — a zip with content.xml inside.
    Uses odfpy if available, otherwise falls back to reading content.xml
    and stripping XML tags.
    """
    # Preferred: odfpy
    try:
        from odf import teletype
        from odf.opendocument import load
        from odf.text import P

        doc = load(io.BytesIO(raw_bytes))
        paragraphs = []
        for p in doc.getElementsByType(P):
            text = teletype.extractText(p).strip()
            if text:
                paragraphs.append(text)
        if paragraphs:
            full_text = "\n\n".join(paragraphs)
            return full_text, _chunk_into_pages(paragraphs)
    except ImportError:
        pass  # fall through to raw XML approach
    except Exception as exc:
        logger.warning("odfpy failed for .odt: %s", exc)

    # Fallback: unzip and strip tags from content.xml
    try:
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            with zf.open("content.xml") as f:
                xml = f.read().decode("utf-8", errors="replace")

        # Convert paragraph breaks and strip tags
        xml = re.sub(r"</text:p>", "\n\n", xml)
        xml = re.sub(r"<[^>]+>", "", xml)
        xml = re.sub(r"\n{3,}", "\n\n", xml).strip()

        if not xml:
            return "", None

        paragraphs = [p.strip() for p in xml.split("\n\n") if p.strip()]
        full_text = "\n\n".join(paragraphs)
        return full_text, _chunk_into_pages(paragraphs)

    except Exception as exc:
        logger.warning(".odt extraction failed: %s", exc)
        return "", None


def _extract_rtf(raw_bytes):
    """
    Rich Text Format (.rtf). Uses striprtf if available, else a
    conservative regex-based tag stripper.
    """
    text_bytes = raw_bytes

    try:
        from striprtf.striprtf import rtf_to_text

        decoded = raw_bytes.decode("latin-1", errors="replace")
        text = rtf_to_text(decoded, errors="ignore") or ""
        text = text.strip()
        if text:
            paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
            return "\n\n".join(paragraphs), _chunk_into_pages(paragraphs)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("striprtf failed: %s", exc)

    # Fallback: strip control words and braces
    try:
        s = text_bytes.decode("latin-1", errors="replace")
        # Remove groups / control words
        s = re.sub(r"\\'[0-9a-fA-F]{2}", "", s)      # hex escapes
        s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)     # control words
        s = s.replace("{", "").replace("}", "")
        s = re.sub(r"\n{3,}", "\n\n", s).strip()
        if not s:
            return "", None
        paragraphs = [p.strip() for p in s.split("\n") if p.strip()]
        return "\n\n".join(paragraphs), _chunk_into_pages(paragraphs)
    except Exception as exc:
        logger.warning(".rtf extraction failed: %s", exc)
        return "", None


def _extract_pptx(raw_bytes):
    try:
        from pptx import Presentation
    except ImportError:
        logger.warning("python-pptx not installed; skipping PPTX extraction.")
        return "", None

    prs = Presentation(io.BytesIO(raw_bytes))
    slides = []
    for i, slide in enumerate(prs.slides, start=1):
        slide_text = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs).strip()
                    if t:
                        slide_text.append(t)
        if slide_text:
            slides.append({"page": i, "text": "\n".join(slide_text)})

    if not slides:
        return "", None

    full_text = "\n\n".join(f"[Slide {s['page']}]\n{s['text']}" for s in slides)
    return full_text, slides


def _extract_ppt(raw_bytes):
    """
    Old PowerPoint binary format (.ppt). Same LibreOffice strategy as .doc.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        logger.warning("LibreOffice not found; cannot extract .ppt files.")
        return "", None

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input.ppt")
        with open(src, "wb") as f:
            f.write(raw_bytes)

        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pptx",
                 "--outdir", tmp, src],
                check=True, timeout=SOFFICE_TIMEOUT, capture_output=True,
            )
        except Exception as exc:
            logger.warning("LibreOffice .ppt→.pptx failed: %s", exc)
            return "", None

        converted = os.path.join(tmp, "input.pptx")
        if not os.path.exists(converted):
            return "", None

        with open(converted, "rb") as f:
            pptx_bytes = f.read()

    return _extract_pptx(pptx_bytes)


def _extract_xlsx(raw_bytes):
    try:
        from openpyxl import load_workbook
    except ImportError:
        logger.warning("openpyxl not installed; skipping .xlsx extraction.")
        return "", None

    wb = load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
    parts = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"[Sheet: {sheet_name}]\n" + "\n".join(rows))

    try:
        wb.close()
    except Exception:
        pass

    if not parts:
        return "", None

    full_text = "\n\n".join(parts)
    return full_text, None


def _extract_xls(raw_bytes):
    """Old Excel binary format (.xls). Requires xlrd."""
    try:
        import xlrd
    except ImportError:
        logger.warning("xlrd not installed; skipping .xls extraction.")
        return "", None

    wb = xlrd.open_workbook(file_contents=raw_bytes)
    parts = []
    for sheet in wb.sheets():
        rows = []
        for r in range(sheet.nrows):
            cells = [str(c).strip() for c in sheet.row_values(r) if str(c).strip()]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"[Sheet: {sheet.name}]\n" + "\n".join(rows))

    if not parts:
        return "", None

    return "\n\n".join(parts), None


def _extract_csv(raw_bytes):
    text = _decode_text(raw_bytes)
    reader = csv.reader(io.StringIO(text))
    rows = []
    for row in reader:
        cells = [c.strip() for c in row if c and c.strip()]
        if cells:
            rows.append(" | ".join(cells))

    if not rows:
        return "", None

    return "\n".join(rows), None


def _extract_text_file(raw_bytes):
    return _decode_text(raw_bytes), None


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _chunk_into_pages(paragraphs, chars_per_page=DOCX_CHARS_PER_PAGE):
    """
    Group a list of paragraph strings into virtual pages of roughly
    `chars_per_page` characters each. Returns a list of
    {"page": int, "text": str}.
    """
    if not paragraphs:
        return None

    pages = []
    current = []
    current_len = 0
    page_num = 1

    for p in paragraphs:
        # A single paragraph longer than a page — split it
        if len(p) > chars_per_page:
            if current:
                pages.append({"page": page_num, "text": "\n\n".join(current)})
                page_num += 1
                current = []
                current_len = 0
            for i in range(0, len(p), chars_per_page):
                pages.append({
                    "page": page_num,
                    "text": p[i:i + chars_per_page],
                })
                page_num += 1
            continue

        current.append(p)
        current_len += len(p)
        if current_len >= chars_per_page:
            pages.append({"page": page_num, "text": "\n\n".join(current)})
            page_num += 1
            current = []
            current_len = 0

    if current:
        pages.append({"page": page_num, "text": "\n\n".join(current)})

    return pages or None


def _decode_text(raw_bytes):
    for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _extract_docx(raw_bytes):
    """Legacy wrapper kept for backwards compatibility."""
    text, _ = _extract_docx_with_pages(raw_bytes)
    return text


# ═══════════════════════════════════════════════════════════════════════
# Handler dispatch table
# ═══════════════════════════════════════════════════════════════════════

_HANDLERS = {
    ".pdf":   _extract_pdf,
    ".docx":  _extract_docx_with_pages,
    ".doc":   _extract_doc,
    ".odt":   _extract_odt,
    ".rtf":   _extract_rtf,
    ".pptx":  _extract_pptx,
    ".ppt":   _extract_ppt,
    ".xlsx":  _extract_xlsx,
    ".xls":   _extract_xls,
    ".csv":   _extract_csv,
    ".txt":   _extract_text_file,
    ".md":    _extract_text_file,
    "":       _extract_text_file,
}