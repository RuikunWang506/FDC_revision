"""Quick test: 4x6 PDF with 20 rows per page from label Excel."""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from py.Toolkits.Label_Maker import (
    LABEL_INTERNAL_SKU_TEXT,
    _should_include_summary_on_4up,
    create_label,
    excel_labels_to_pdf,
    excel_labels_to_pdf_4perpage,
    ft_summary,
)

OUT = ROOT / "instance" / "test_label_4x6.pdf"
OUT_4UP = ROOT / "instance" / "test_label_4perpage.pdf"


def _build_two_label_workbook():
    wb = Workbook()
    ws = wb.active
    ws.title = "Labels"
    for col, w in zip("ABCDE", (11.71, 12.29, 12.43, 6.57, 8.43)):
        ws.column_dimensions[col].width = w
    ws.append([])
    ws.append([])
    row = 3
    from_addr = ["FROM: E-Fashion Inc", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
    to_addr = ["TO: ROSS", "3404 INDIAN AVENUE", "DISTRIBUTION CENTER", "PERRIS, CA 92572"]
    for carton in (1, 2):
        row = create_label(
            ws, from_addr, to_addr, row,
            "T3770", "BK", "11677688", carton, 20, 67,
        )
        row += 2
    summary_start = row + 7
    ws[f"B{summary_start}"] = "FDC"
    ws[f"B{summary_start + 1}"] = LABEL_INTERNAL_SKU_TEXT
    ws[f"C{summary_start + 1}"] = "12345"
    ws[f"C{summary_start + 1}"].font = ft_summary
    ws[f"B{summary_start + 3}"] = "Boxes #:"
    ws[f"C{summary_start + 3}"] = "2"
    ws[f"C{summary_start + 3}"].font = ft_summary
    ws[f"B{summary_start + 4}"] = "PO#:"
    ws[f"C{summary_start + 4}"] = "11677688"
    ws[f"C{summary_start + 4}"].font = ft_summary
    ws[f"B{summary_start + 5}"] = "LOCATION: "
    ws[f"C{summary_start + 5}"] = "A-1"
    ws[f"C{summary_start + 5}"].font = ft_summary
    for r in range(1, ws.max_row + 1):
        ws.row_dimensions[r].height = 15
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def main():
    excel_data = _build_two_label_workbook()
    pdf_data = excel_labels_to_pdf(excel_data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(pdf_data)
    from pypdf import PdfReader

    n_pages = len(PdfReader(io.BytesIO(pdf_data)).pages)
    print(f"Wrote {OUT}")
    print(f"Pages: {n_pages} (expected 3 = 2 labels + 1 summary)")
    if n_pages != 3:
        raise SystemExit(f"FAIL: expected 3 pages, got {n_pages}")
    page = PdfReader(io.BytesIO(pdf_data)).pages[0]
    w_in = float(page.mediabox.width) / 72
    h_in = float(page.mediabox.height) / 72
    print(f"Page size: {w_in:.2f} x {h_in:.2f} in (expected 6.00 x 4.00 landscape)")
    if abs(w_in - 6) > 0.05 or abs(h_in - 4) > 0.05:
        raise SystemExit("FAIL: page size not 6x4 inches (horizontal 4x6 label)")
    assert _should_include_summary_on_4up(2, 100) is True
    assert _should_include_summary_on_4up(4, 100) is False
    assert _should_include_summary_on_4up(60, 100) is False

    pdf_4up = excel_labels_to_pdf_4perpage(excel_data)
    OUT_4UP.write_bytes(pdf_4up)
    n_4up = len(PdfReader(io.BytesIO(pdf_4up)).pages)
    print(f"Wrote {OUT_4UP}")
    print(f"4-up pages: {n_4up} (expected 1 = 2 labels + summary on one letter sheet)")
    if n_4up != 1:
        raise SystemExit(f"FAIL: expected 1 letter page, got {n_4up}")
    p0 = PdfReader(io.BytesIO(pdf_4up)).pages[0]
    w_in = float(p0.mediabox.width) / 72
    h_in = float(p0.mediabox.height) / 72
    if abs(w_in - 11) > 0.05 or abs(h_in - 8.5) > 0.05:
        raise SystemExit("FAIL: 4-up page not US Letter landscape")
    print("OK")


if __name__ == "__main__":
    main()
