# campusos/correction_pdf.py
"""
Branded correction report PDF generator.

Reads a ResearchSubmission (with a RubricEvaluation that carries page-aware
findings) and renders a Gulu-University-branded PDF using reportlab.

Public API:
    build_correction_pdf(submission) -> bytes

The caller (a Celery task) is responsible for saving those bytes to the
CorrectionReport.file field (Cloudinary).
"""
import io
import logging
from datetime import datetime

from django.conf import settings

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Branding — override via settings
# -----------------------------------------------------------------------------

UNIVERSITY_NAME = getattr(settings, "CORRECTION_UNIV_NAME", "Gulu University")
DEPARTMENT_NAME = getattr(settings, "CORRECTION_DEPT_NAME", "Department of Geography")
LOGO_PATH = getattr(settings, "CORRECTION_LOGO_PATH", "")  # optional filesystem path
BRAND_COLOR = getattr(settings, "CORRECTION_BRAND_COLOR", "#0d47a1")
PASS_COLOR = "#059669"
FAIL_COLOR = "#dc2626"


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------

def build_correction_pdf(submission):
    """
    Render the PDF. `submission` must be a ResearchSubmission with a
    non-null `document` and at least one RubricEvaluation.

    Returns: bytes
    Raises: ValueError if the submission has no evaluation.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image, PageBreak, KeepTogether,
    )

    evaluation = (
        submission.evaluations.order_by("-evaluated_at").first()
        or getattr(submission, "evaluations", None)
    )
    if evaluation is None:
        raise ValueError("Submission has no RubricEvaluation yet.")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Correction Report — {submission.document.title}",
        author=UNIVERSITY_NAME,
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"],
                        fontSize=18, leading=22,
                        textColor=colors.HexColor(BRAND_COLOR))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"],
                        fontSize=13, leading=16,
                        textColor=colors.HexColor(BRAND_COLOR))
    body = ParagraphStyle("body", parent=styles["BodyText"],
                          fontSize=10, leading=14)
    small = ParagraphStyle("small", parent=body, fontSize=9,
                           textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("cell", parent=body, fontSize=9, leading=12)
    cell_bold = ParagraphStyle("cell_bold", parent=cell, fontName="Helvetica-Bold")

    story = []

    # ── Header ──────────────────────────────────────────────
    header_row = []
    if LOGO_PATH:
        try:
            header_row.append(Image(LOGO_PATH, width=22 * mm, height=22 * mm))
        except Exception as exc:
            logger.warning("Could not load logo at %s: %s", LOGO_PATH, exc)
    header_text = [
        Paragraph(f"<b>{UNIVERSITY_NAME}</b>", h1),
        Paragraph(DEPARTMENT_NAME, small),
        Spacer(1, 2),
        Paragraph("<b>Research Correction Report</b>", h2),
    ]
    header_row.append(header_text)
    header_table = Table([header_row], colWidths=[30 * mm, None] if LOGO_PATH else [None])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 8))

    # ── Meta table ─────────────────────────────────────────
    doc_row = submission.document
    student_user = submission.student.user
    meta_data = [
        ["Student:", student_user.get_full_name() or student_user.username,
         "Student ID:", submission.student.student_id or "—"],
        ["Course:", (submission.course.code + " — " + submission.course.title)
                    if submission.course else "—",
         "Submitted:", submission.submitted_at.strftime("%d %b %Y")],
        ["Document:", doc_row.title or "—",
         "Rubric:", submission.rubric.title if submission.rubric else "—"],
    ]
    meta_table = Table(meta_data, colWidths=[25 * mm, 65 * mm, 27 * mm, 55 * mm])
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#eeeeee")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 12))

    # ── Overall score banner ───────────────────────────────
    overall = float(evaluation.overall_score)
    findings = _findings_from_feedback(evaluation.feedback)
    passes = sum(1 for f in findings if f["status"] == "PASS")
    fails = len(findings) - passes

    banner_color = PASS_COLOR if overall >= 60 else FAIL_COLOR
    banner = Table([[Paragraph(
        f'<font color="white" size="16"><b>Overall score: {overall:.1f}%</b></font>'
        f'<br/><font color="white" size="10">{passes} PASS · {fails} FAIL · '
        f'generated {datetime.utcnow().strftime("%d %b %Y")}</font>',
        ParagraphStyle("banner", parent=body, alignment=1, leading=22),
    )]], colWidths=[None])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(banner_color)),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(banner)
    story.append(Spacer(1, 14))

    # ── Summary table of corrections ───────────────────────
    story.append(Paragraph("Table of Corrections", h2))
    story.append(Spacer(1, 6))

    if not findings:
        story.append(Paragraph(
            "No per-criterion findings were recorded. "
            "Regenerate the evaluation to produce a detailed table.", body))
    else:
        rows = [[
            Paragraph("<b>Criterion</b>", cell_bold),
            Paragraph("<b>Status</b>", cell_bold),
            Paragraph("<b>Location</b>", cell_bold),
            Paragraph("<b>What to fix</b>", cell_bold),
        ]]
        for f in findings:
            loc = _location_label(f)
            status_color = PASS_COLOR if f["status"] == "PASS" else FAIL_COLOR
            rows.append([
                Paragraph(_esc(f["criterion"]), cell),
                Paragraph(f'<font color="{status_color}"><b>{f["status"]}</b></font>',
                          cell),
                Paragraph(_esc(loc), cell),
                Paragraph(
                    f'<b>Issue:</b> {_esc(f["issue"] or "—")}<br/>'
                    f'<b>Suggestion:</b> {_esc(f["suggestion"] or "—")}',
                    cell,
                ),
            ])
        tbl = Table(rows, colWidths=[30 * mm, 16 * mm, 26 * mm, 100 * mm],
                    repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(BRAND_COLOR)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor("#999999")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f6f8fb")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(tbl)

    # ── TODO list ──────────────────────────────────────────
    todo_items = [f for f in findings if f["status"] == "FAIL"]
    if todo_items:
        story.append(Spacer(1, 16))
        story.append(Paragraph("Your TODO list", h2))
        story.append(Spacer(1, 4))
        for i, f in enumerate(todo_items, start=1):
            loc = _location_label(f)
            story.append(Paragraph(
                f'<b>{i}.</b> [{_esc(f["criterion"])}] {_esc(f["suggestion"] or f["issue"] or "Review this section.")}'
                f' <font color="#777777">({_esc(loc)})</font>',
                body,
            ))
            story.append(Spacer(1, 3))

    # ── Footer ────────────────────────────────────────────
    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(18 * mm, 12 * mm,
                          f"{UNIVERSITY_NAME} — {DEPARTMENT_NAME}")
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _findings_from_feedback(feedback):
    """
    Normalize RubricEvaluation.feedback into a stable list shape.

    Two shapes exist in the DB:
      1. Page-aware:   {crit: {status, issue, suggestion, page, paragraph, snippet}}
      2. Legacy flat:  {crit: "some text"}
    Legacy entries still show up in the PDF, just without a location.
    """
    out = []
    if not isinstance(feedback, dict):
        return out
    for name, value in feedback.items():
        if isinstance(value, dict):
            out.append({
                "criterion": str(name),
                "status": str(value.get("status") or "FAIL").upper(),
                "issue": str(value.get("issue") or ""),
                "suggestion": str(value.get("suggestion") or ""),
                "page": value.get("page"),
                "paragraph": value.get("paragraph"),
                "snippet": str(value.get("snippet") or ""),
            })
        else:
            out.append({
                "criterion": str(name),
                "status": "FAIL",
                "issue": str(value or ""),
                "suggestion": "",
                "page": None,
                "paragraph": None,
                "snippet": "",
            })
    return out


def _location_label(f):
    page = f.get("page")
    para = f.get("paragraph")
    if page and para:
        return f"Page {page}, Paragraph {para}"
    if page:
        return f"Page {page}"
    return "Location not identified"


def _esc(text):
    """Escape a few characters reportlab's Paragraph treats specially."""
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))