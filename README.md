# Merge PDF API

A FastAPI application that merges PDF documents from a remote resource endpoint.

## Overview

This service provides two endpoints:
- **`GET /`** - Health check endpoint
- **`GET /merge`** - Merges PDFs from a resource's member list

The `/merge` endpoint accepts a resource URL via the `X-Islandora-Event` header, fetches the member list, downloads files for each member (retrying alternate URLs if conversion fails), converts non-PDFs to PDF format, and returns a merged PDF.

## Requirements

- Python 3.12+
- Docker (optional, for containerized deployment)

## Installation

### Local Setup

1. Clone or navigate to the project directory:
```bash
cd mergepdf
```

2. Create a virtual environment (recommended):
```bash
python -m venv venv
source venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

## Running the Application

> Logging level can be set with the environment variable `UVICORN_LOG_LEVEL`. E.g. `export UVICORN_LOG_LEVEL="debug"`.

### Local Development

Run the FastAPI server directly:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The application will be available at `http://localhost:8000`

### Docker

Build the Docker image:
```bash
docker build -t mergepdf:latest .
```

Run the container:
```bash
docker run --name mergepdf -e MERGEPDF_KEEP_FILES=1 -e MERGEPDF_DPI=300 -p 8000:8000 mergepdf:latest
```

The application will be available at `http://localhost:8000`

## API Usage

### Health Check

```bash
curl http://localhost:8000/
```

Response:
```json
{
  "status": "healthy",
  "service": "merge-pdf-api"
}
```

### Merge PDFs

```bash
curl -H "Apix-Ldp-Resource: https://example.com/resource" \
  http://localhost:8000/merge \
  --output merged.pdf
```

**Required Headers:**
- `Apix-Ldp-Resource` - URL to the resource endpoint

**Expected Behavior:**
1. Appends `/members-list?_format=json` to the resource URL
2. Expects a JSON array with objects containing:
   - `nid` - Unique identifier for the member
   - `field_*` - Fields containing file URLs (collects all URLs for each nid)
3. For each unique `nid`:
   - Attempts to download and convert files in order
   - If conversion fails, retries with the next available URL
   - Skips the nid if no URL produces a valid PDF
4. Converts non-PDF files to PDF with OCR (currently supports image formats)
5. Processes nids sequentially to maintain page ordering
6. Merges all PDFs into a single document
7. Returns the merged PDF

## Supported File Formats

The application automatically converts the following formats to PDF:
- **Images**: PNG, JPG, TIFF, Jpeg2000, etc.
- **PDF**: Already in PDF format (no conversion needed)

## Example Member List Format

The `https://prism.lib.asu.edu/node/{nid}/members-list?_format=json` endpoint should return:

```json
[
  {
    "nid": "1",
    "title": "Document 1",
    "field_document": "https://example.com/document1.pdf"
  },
  {
    "nid": "2",
    "title": "Page 1",
    "field_attachment": "https://example.com/image.jpg"
  }
]
```

## Development

### Project Structure

```
mergepdf/
├── Dockerfile             # Docker configuration
├── requirements.txt       # Python dependencies
├── README.md              # This file
├── app/
|   └── main.py            # FastAPI application
├── Dockerfile.test        # Docker configuration for tests
├── requirements-test.txt  # Python dependencies for tests
└── tests/
    ├── asets/             # Example files for testing
    └── test_merge.py      # Test script

```

### Logging

The application logs to stdout with INFO level logging. View logs with:

```bash
# Local
tail -f <output from uvicorn>

# Docker
docker logs -f <container-id>
```

## Error Handling

- **400 Bad Request**: Missing `Apix-Ldp-Resource` header or invalid member list format
- **500 Internal Server Error**: File download, conversion, or merge failures

Check logs for detailed error information.

## Running Tests with Docker

To run tests in a clean environment, use the provided test image:

1. Build the test image:
  ```bash
  docker build -f Dockerfile.test -t mergepdf-test:latest .
  ```

2. Run the tests:
  ```bash
  docker run --rm -v "$PWD":/app -w /app -e PYTHONPATH=/app mergepdf-test:latest pytest
  ```

You can also run a specific test file:
  ```bash
  docker run --rm -v "$PWD":/app -w /app -e PYTHONPATH=/app mergepdf-test:latest pytest tests/test_merge.py
  ```

## Dependencies

### Runtime Dependencies
- **fastapi** (0.109.0) - Modern web framework for building APIs
- **uvicorn** (0.27.0) - ASGI server for running FastAPI
- **httpx** (0.25.2) - HTTP client for making requests
- **requests** (2.31.0) - Additional HTTP library
- **pypdf** (6.0.0) - PDF manipulation and merging
- **Pillow** (10.1.0) - Image processing and conversion to PDF
- **pdf2image** (1.16.3) - PDF image extraction
- **pytesseract** (0.3.13) - Optical character recognition (OCR) for images

### Testing Dependencies
- **pytest** - Testing framework
- **pytest-mock** - Pytest plugin for mocking