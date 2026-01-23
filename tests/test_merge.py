import os
import sys
import tempfile
import pytest
import base64
import json
from unittest.mock import patch, MagicMock
from pypdf import PdfReader
from fastapi.testclient import TestClient

# Add the parent directory to the path so we can import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import convert_to_pdf, merge_pdf_files, DPI, LETTER_WIDTH_PX, LETTER_HEIGHT_PX, app

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

def test_broken_jpf():
    image_path = os.path.join(ASSETS_DIR, "PALGEN_SEP-2736_Adjusted.jpf")
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    with tempfile.TemporaryDirectory() as temp_dir:
        converted_pdf = convert_to_pdf(image_bytes, "application/octet-stream", temp_dir, "photo")
        assert converted_pdf is not None
        assert os.path.exists(converted_pdf)

def test_image_conversion_and_page_merging():
    # Static test files
    image_path = os.path.join(ASSETS_DIR, "SM219_WeGetUpAt8AM_1900_FrontCover.jpf")
    pdf_path = os.path.join(ASSETS_DIR, "SM219_WeGetUpAt8AM_1900_Score.pdf")

    with open(image_path, "rb") as f:
        image_bytes = f.read()
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    with tempfile.TemporaryDirectory() as temp_dir:
        # Convert image to PDF
        converted_pdf = convert_to_pdf(image_bytes, "image/jpf", temp_dir, "cover")
        assert converted_pdf is not None
        assert os.path.exists(converted_pdf)

        # Save the static PDF for merging
        static_pdf_path = os.path.join(temp_dir, "score.pdf")
        with open(static_pdf_path, "wb") as f:
            f.write(pdf_bytes)

        # Merge both PDFs
        merged_pdf = merge_pdf_files([converted_pdf, static_pdf_path], temp_dir)
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

def test_merge_pdfs_endpoint(monkeypatch):
    """Test the /merge endpoint with mocked HTTP requests."""
    # Import app.main to use for monkeypatching, but save app reference first
    import app.main as app_main

    client = TestClient(app)

    # Create base64 encoded event JSON
    event_data = {
        "object": {
            "url": [
                {
                    "href": "http://localhost:8000/resource/item1",
                    "rel": "canonical"
                }
            ]
        }
    }
    event_json = json.dumps(event_data)
    encoded_event = base64.b64encode(event_json.encode()).decode()

    # Load test files from assets to mock file downloads
    jpf_path = os.path.join(ASSETS_DIR, "SM219_WeGetUpAt8AM_1900_FrontCover.jpf")
    with open(jpf_path, "rb") as f:
        file1_bytes = f.read()

    pdf_path = os.path.join(ASSETS_DIR, "SM219_WeGetUpAt8AM_1900_Score.pdf")
    with open(pdf_path, "rb") as f:
        file2_bytes = f.read()

    # Members list response data
    # Simulate one missing file (code should fall back to the next 'item1'), one JPF, and one PDF.
    members_data = [
        {
            "nid": "item1",
            "field_document": "http://localhost:8000/resource/item1/MISSING"
        },
        {
            "nid": "item1",
            "field_document": "http://localhost:8000/resource/item1/file1.jpf"
        },
        {
            "nid": "item2",
            "field_document": "http://localhost:8000/resource/item2/file2.pdf"
        }
    ]

    # Create mock response objects
    class MockResponse:
        def __init__(self, json_data=None, content=None, headers=None):
            self._json_data = json_data
            self.content = content
            self.headers = headers or {}

        def json(self):
            return self._json_data

        def raise_for_status(self):
            pass

    # Create a mock client that returns different responses
    class MockClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            if "members-list" in url:
                return MockResponse(json_data=members_data)
            elif "term_from_term_name" in url:
                return MockResponse(json_data=[
                    {
                        "tid": [
                            {
                                "value": "12345"
                            }
                        ]
                    }
                ])
            elif "file1.jpf" in url:
                # Return JPF file
                return MockResponse(
                    json_data=None,
                    content=file1_bytes,
                    headers={"content-type": "image/jpf"}
                )
            elif "file2.pdf" in url:
                # Return PDF file
                return MockResponse(
                    json_data=None,
                    content=file2_bytes,
                    headers={"content-type": "application/pdf"}
                )
            elif "MISSING" in url:
                # Simulate missing file
                response = MockResponse()
                response.raise_for_status = MagicMock(side_effect=Exception("404 Not Found"))
                return response
            else:
                # Default file download
                return MockResponse(
                    json_data=None,
                    content=file2_bytes,
                    headers={"content-type": "application/pdf"}
                )

    # Mock the httpx.Client
    monkeypatch.setattr(app_main.httpx, "Client", MockClient)

    # Mock the sync httpx.put and save the file for manual review
    put_called = []
    output_dir = "/app/test_output"
    os.makedirs(output_dir, exist_ok=True)

    def mock_put(url, **kwargs):
        put_called.append((url, kwargs))

        # Save the PDF file content to disk for manual review
        if "content" in kwargs:
            # kwargs["content"] is a file-like object
            file_obj = kwargs["content"]
            output_path = os.path.join(output_dir, "merged_output.pdf")
            with open(output_path, "wb") as out_file:
                out_file.write(file_obj.read())
            print(f"\nMerged PDF saved to: {output_path}")

        response = MagicMock()
        response.raise_for_status = MagicMock()
        return response

    monkeypatch.setattr(app_main.httpx, "put", mock_put)

    # Make the request to /merge endpoint
    headers = {
        "X-Islandora-Event": encoded_event,
        "Authorization": "Bearer test-token"
    }
    response = client.get("/merge", headers=headers)

    # Verify the response
    assert response.status_code == 200, f"Expected 200, got {response.status_code}. Response: {response.json()}"
    assert response.json()["status"] == "success"
    assert "merged and uploaded" in response.json()["message"]

    # Verify PUT was called with correct parameters
    assert len(put_called) > 0
    put_url, put_kwargs = put_called[0]
    assert "media/document" in put_url
    assert put_kwargs["headers"]["Content-Type"] == "application/pdf"