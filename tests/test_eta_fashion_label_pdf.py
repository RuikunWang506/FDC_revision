import importlib.util
import unittest
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.units import inch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


eta = load_module("eta_fashion_label_pdf", "py/other/eta_fashion_label_pdf.py")


class EtaFashionLabelPdfTests(unittest.TestCase):
    def test_4x6_page_count_and_size_for_25_boxes(self):
        data = eta.build_eta_fashion_label_pdf_4x6(25)
        reader = PdfReader(BytesIO(data))
        self.assertEqual(len(reader.pages), 25)
        page = reader.pages[0]
        self.assertAlmostEqual(float(page.mediabox.width) / inch, 6.0, places=2)
        self.assertAlmostEqual(float(page.mediabox.height) / inch, 4.0, places=2)

    def test_letter_packs_four_labels_per_page(self):
        data = eta.build_eta_fashion_label_pdf_letter(5)
        reader = PdfReader(BytesIO(data))
        self.assertEqual(len(reader.pages), 2)


if __name__ == "__main__":
    unittest.main()
