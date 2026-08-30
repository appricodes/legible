"""Non-GUI verification of the pipeline: model ops, OCR (mock), exports."""

import os
os.environ["LEGIBLE_FORCE_MOCK"] = "1"

from PIL import Image, ImageDraw

from legible import ocr, export
from legible.model import Session, Page, OcrBlock


def make_test_image(path, text):
    img = Image.new("RGB", (850, 1100), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 60), text, fill="black")
    d.rectangle([60, 300, 500, 500], outline="black")
    img.save(path)
    return path


def main():
    # test_output/ is gitignored (see .gitignore) - never committed, so a
    # fresh checkout won't have it; create it here rather than assuming.
    os.makedirs("test_output", exist_ok=True)

    # 1. Model list operations
    s = Session()
    p1 = ocr.ocr_page(make_test_image("test_output/p1.png", "Page One"))
    p2 = ocr.ocr_page(make_test_image("test_output/p2.png", "Page Two"))
    p3 = ocr.ocr_page(make_test_image("test_output/p3.png", "Page Three"))
    s.add(p1); s.add(p2); s.add(p3)
    assert len(s) == 3

    # preview / list_label
    assert len(s.pages[0].preview(128)) <= 129
    print("preview[0]:", repr(s.pages[0].preview(60)))

    # reorder
    new = s.move_down(0)
    assert new == 1
    new = s.move_up(2)
    assert new == 1
    # rename
    s.rename(0, "Cover Sheet")
    assert s.pages[0].list_label() == "Cover Sheet"
    print("list_label[0] after rename:", s.pages[0].list_label())
    # delete
    s.delete(2)
    assert len(s) == 2
    print("Model ops: OK")

    # 2. full_text and table flattening
    assert "Item" in s.pages[0].full_text and "Widget" in s.pages[0].full_text
    print("full_text contains table rows: OK")

    # 3. HTML export
    html_path = "test_output/out.html"
    export.export_html(s, html_path, title="Test Doc")
    html = open(html_path, encoding="utf-8").read()
    assert "<table" in html and "Widget" in html and "<h2" in html
    assert "<img" not in html  # text-only requirement
    print("HTML export: OK  (", os.path.getsize(html_path), "bytes )")

    # 4. PDF export
    pdf_path = "test_output/out.pdf"
    export.export_pdf(s, pdf_path, title="Test Doc")
    assert os.path.getsize(pdf_path) > 1000
    header = open(pdf_path, "rb").read(5)
    assert header == b"%PDF-", header
    print("PDF export: OK  (", os.path.getsize(pdf_path), "bytes )")

    # 5. Confirm invisible-text render mode present in PDF content stream
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    content = b"".join(
        p.get_contents().get_data() for p in reader.pages
    )
    assert b"3 Tr" in content, "invisible text render mode not found"
    # And that the actual OCR text is embedded in that layer.
    assert b"Widget" in content or b"Sample" in content
    print("PDF invisible text layer (render mode 3): OK")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
