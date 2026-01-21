import os
import tempfile
import pytest
from pypdf import PdfReader
from app.main import convert_to_pdf, merge_pdf_files, DPI, LETTER_WIDTH_PX, LETTER_HEIGHT_PX

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

@pytest.mark.asyncio
async def test_broken_jpf():
    image_path = os.path.join(ASSETS_DIR, "PALGEN_SEP-2736_Adjusted.jpf")
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    with tempfile.TemporaryDirectory() as temp_dir:
        converted_pdf = await convert_to_pdf(image_bytes, "application/octet-stream", temp_dir, "photo")
        assert converted_pdf is not None
        assert os.path.exists(converted_pdf)

@pytest.mark.asyncio
async def test_image_conversion_and_page_merging():
    # Static test files
    image_path = os.path.join(ASSETS_DIR, "SM219_WeGetUpAt8AM_1900_FrontCover.jpf")
    pdf_path = os.path.join(ASSETS_DIR, "SM219_WeGetUpAt8AM_1900_Score.pdf")

    with open(image_path, "rb") as f:
        image_bytes = f.read()
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    with tempfile.TemporaryDirectory() as temp_dir:
        # Convert image to PDF
        converted_pdf = await convert_to_pdf(image_bytes, "image/jpf", temp_dir, "cover")
        assert converted_pdf is not None
        assert os.path.exists(converted_pdf)

        # Save the static PDF for merging
        static_pdf_path = os.path.join(temp_dir, "score.pdf")
        with open(static_pdf_path, "wb") as f:
            f.write(pdf_bytes)

        # Merge both PDFs
        merged_pdf = await merge_pdf_files([converted_pdf, static_pdf_path], temp_dir)
        assert merged_pdf is not None
        assert os.path.exists(merged_pdf)

        # Check merged PDF page count
        reader = PdfReader(merged_pdf)
        assert len(reader.pages) == 4  # 1 from image + 3 from score PDF

        # Check page sizes (should be letter size)
        for i, page in enumerate(reader.pages):
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            print(f"Page {i+1}: width={width}, height={height}")
            print(f"Expected: width={LETTER_WIDTH_PX * (72.0 / DPI)}, height={LETTER_HEIGHT_PX * (72.0 / DPI)}")
            assert abs(width - LETTER_WIDTH_PX * (72.0 / DPI)) < 5
            assert abs(height - LETTER_HEIGHT_PX * (72.0 / DPI)) < 5
