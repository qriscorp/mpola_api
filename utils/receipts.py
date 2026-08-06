"""
Real PDF receipt generation (reportlab, pure-Python — no external service).
Was previously a dead "Download Receipt" button on both frontends.
"""

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def build_repayment_receipt_pdf(*, receipt_id: str, borrower_name: str, lender_name: str,
                                 loan_reference: str, amount: float, instalment_number: int,
                                 payment_method: str, status: str, paid_at: str) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=30 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("MpolaTitle", parent=styles["Heading1"], textColor=colors.HexColor("#1B2B3A"))
    sub_style = ParagraphStyle("MpolaSub", parent=styles["Normal"], textColor=colors.HexColor("#2BB5A0"), fontSize=11)

    elements = [
        Paragraph("Mpola", title_style),
        Paragraph("Peer-to-Peer Lending Platform — Payment Receipt", sub_style),
        Spacer(1, 12 * mm),
    ]

    rows = [
        ["Receipt No.", receipt_id],
        ["Date", paid_at],
        ["Loan Reference", loan_reference],
        ["Borrower", borrower_name],
        ["Lender", lender_name],
        ["Instalment", f"#{instalment_number}"],
        ["Payment Method", payment_method.replace("_", " ").title()],
        ["Status", status.title()],
        ["Amount Paid", f"UGX {amount:,.0f}"],
    ]
    table = Table(rows, colWidths=[50 * mm, 100 * mm])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#556070")),
        ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#1B2B3A")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 12),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 20 * mm))
    elements.append(Paragraph(
        "This is a system-generated receipt from Mpola Uganda Ltd. "
        "For questions about this payment, contact support from the app.",
        ParagraphStyle("Footer", parent=styles["Normal"], textColor=colors.HexColor("#8A9BA8"), fontSize=8),
    ))

    doc.build(elements)
    return buf.getvalue()
