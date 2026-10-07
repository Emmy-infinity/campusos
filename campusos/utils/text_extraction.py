# utils/text_extraction.py
import io
import logging
import os

logger = logging.getLogger(__name__)

# ~3000 chars per virtual page. DOCX has no native pages, so we group
# paragraphs into chunks and label them as pages 1..N. The correction
# report then cites "Page 2, Paragraph 5" against these virtual pages.
DOCX_CHARS_PER_PAGE = 3000


def extract_text_and_pages(file_field):
    if not file_field:
        return "", None

    name = getattr(file_field, "name", "") or ""
    ext = os.path.splitext(name)[1].lower()

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

    try:
        if ext == ".pdf":
            return _extract_pdf(raw)
        if ext == ".docx":
            return _extract_docx_with_pages(raw)
        if ext == ".pptx":
            return _extract_pptx(raw), None
        if ext in (".txt", ".md", ""):
            return _decode_text(raw), None
        return "", None
    except Exception as exc:
        logger.exception("Text extraction failed for %s: %s", name, exc)
        return "", None


def extract_text(file_field):
    text, _ = extract_text_and_pages(file_field)
    return text


def _extract_pdf(raw_bytes):
    try:
        import fitz
    except ImportError:
        logger.warning("PyMuPDF (fitz) not installed; skipping PDF extraction.")
        return "", None

    doc = fitz.open(stream=raw_bytes, filetype="pdf")
    try:
        pages = []
        full_chunks = []
        for i, page in enumerate(doc):
            try:
                text = page.get_text("text") or ""
            except Exception as exc:
                logger.warning("Failed to read page %s: %s", i + 1, exc)
                text = ""
            text = text.strip()
            if text:
                pages.append({"page": i + 1, "text": text})
                full_chunks.append(text)
        return "\n\n".join(full_chunks), pages or None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _extract_docx_with_pages(raw_bytes):
    """
    Return (full_text, pages) for a DOCX.

    `pages` is a list of {page: int, text: str} built by accumulating
    paragraphs into virtual pages of ~DOCX_CHARS_PER_PAGE chars each.
    Table rows are included as paragraphs too — students often put
    methodology tables in DOCX.
    """
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx not installed; skipping DOCX extraction.")
        return "", None

    doc = Document(io.BytesIO(raw_bytes))
    paragraphs = []

    # Body paragraphs
    for para in doc.paragraphs:
        t = (para.text or "").strip()
        if t:
            paragraphs.append(t)

    # Table text
    for table in doc.tables:
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            row_text = " | ".join(c for c in cells if c)
            if row_text:
                paragraphs.append(row_text)

    if not paragraphs:
        return "", None

    # Accumulate paragraphs into virtual pages
    pages = []
    current = []
    current_len = 0
    page_num = 1

    for p in paragraphs:
        # Split a single very long paragraph across multiple pages
        if len(p) > DOCX_CHARS_PER_PAGE:
            if current:
                pages.append({"page": page_num, "text": "\n\n".join(current)})
                page_num += 1
                current = []
                current_len = 0
            for i in range(0, len(p), DOCX_CHARS_PER_PAGE):
                chunk = p[i:i + DOCX_CHARS_PER_PAGE]
                pages.append({"page": page_num, "text": chunk})
                page_num += 1
            continue

        # Normal paragraph: accumulate
        current.append(p)
        current_len += len(p)
        if current_len >= DOCX_CHARS_PER_PAGE:
            pages.append({"page": page_num, "text": "\n\n".join(current)})
            page_num += 1
            current = []
            current_len = 0

    # Flush the tail
    if current:
        pages.append({"page": page_num, "text": "\n\n".join(current)})

    full_text = "\n\n".join(paragraphs)
    return full_text, pages


def _extract_docx(raw_bytes):
    """Legacy wrapper — callers that only need the flat text."""
    text, _ = _extract_docx_with_pages(raw_bytes)
    return text


def _extract_pptx(raw_bytes):
    try:
        from pptx import Presentation
    except ImportError:
        logger.warning("python-pptx not installed; skipping PPTX extraction.")
        return ""

    prs = Presentation(io.BytesIO(raw_bytes))
    parts = []
    for i, slide in enumerate(prs.slides, start=1):
        slide_text = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs).strip()
                    if t:
                        slide_text.append(t)
        if slide_text:
            parts.append(f"[Slide {i}]\n" + "\n".join(slide_text))
    return "\n\n".join(parts)


def _decode_text(raw_bytes):
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")