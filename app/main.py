import logging
import base64
import json
from typing import Optional
from fastapi import FastAPI, Request, Header, HTTPException, status, BackgroundTasks
from fastapi.responses import FileResponse
import httpx
import tempfile
import os
from pathlib import Path
from pypdf import PdfReader, PdfWriter, PageObject
from PIL import Image, ImageFile, ImageOps
Image.MAX_IMAGE_PIXELS = None # Allow big images.
ImageFile.LOAD_TRUNCATED_IMAGES = True # Some of our JPFs require this.
import io
from io import BytesIO
import atexit
import shutil
import pytesseract
from urllib.parse import urlparse, urljoin
from datetime import datetime

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# Standard DPI for PDF rendering
DPI = int(os.getenv("MERGEPDF_DPI", "200"))

# Letter size in pixels at standard DPI
LETTER_WIDTH_PX = int(8.5 * DPI)
LETTER_HEIGHT_PX = int(11.0 * DPI)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Merge PDF API", version="1.0.0")

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
	exc_str = f'{exc}'.replace('\n', ' ').replace('   ', ' ')

    # Extract headers as a dict
	headers = dict(request.headers)
	headers_str = ", ".join(f"{k}: {v}" for k, v in headers.sitems())

	logging.error(f"{request}: {exc_str}  | Headers: {headers_str}")
	content = {'status_code': 10422, 'message': exc_str, 'data': None}
	return JSONResponse(content=content, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

# Debug / keep-files flag driven by environment variable
# Set `MERGEPDF_KEEP_FILES=1` or `true` to keep downloaded/merged files for debugging
KEEP_FILES = os.getenv("MERGEPDF_KEEP_FILES", "false").lower() in ("1", "true", "yes")
logger.info(f"MERGEPDF_KEEP_FILES={KEEP_FILES}")


# Directory for persistent temporary files
PERSISTENT_TEMP_DIR = tempfile.mkdtemp(prefix="mergepdf_")
logger.info(f"Created persistent temp directory: {PERSISTENT_TEMP_DIR}")


def cleanup_temp_dir():
    """Clean up the persistent temporary directory on exit unless KEEP_FILES is set."""
    if KEEP_FILES:
        logger.info("KEEP_FILES enabled; skipping persistent temp directory cleanup on exit")
        return
    try:
        if os.path.exists(PERSISTENT_TEMP_DIR):
            shutil.rmtree(PERSISTENT_TEMP_DIR)
            logger.info(f"Cleaned up temp directory: {PERSISTENT_TEMP_DIR}")
    except Exception as e:
        logger.error(f"Error cleaning up temp directory: {str(e)}")


# Register cleanup on application exit
atexit.register(cleanup_temp_dir)


@app.get("/")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "merge-pdf-api"}


@app.get("/merge")
async def merge_pdfs(
    request: Request,
    background_tasks: BackgroundTasks,
    islandora_event: str = Header(..., alias="X-Islandora-Event")
):
    """
    Merge PDFs from a given resource URL.
    
    Expects the Apix-Ldp-Resource header containing a URL.
    Appends '/members-list?_format=json' to fetch the list of documents.
    Downloads and merges the first file for each nid into a single PDF.
    """
    # Decode the X-Islandora-Event header (base64 encoded JSON) and extract href
    if not islandora_event:
        raise HTTPException(status_code=400, detail="X-Islandora-Event header is required")

    try:
        decoded = base64.b64decode(islandora_event).decode("utf-8")
        event_json = json.loads(decoded)
    except Exception as e:
        logger.error(f"Failed to decode/parse X-Islandora-Event: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid X-Islandora-Event header: must be base64 encoded JSON")

    # Expecting structure: { ..., "object": { "url": [ {"href": "..."}, ... ] }, ... }
    logger.debug(f"Event object: {json.dumps(event_json)}")
    href = None
    obj = event_json.get("object") if isinstance(event_json, dict) else None
    if isinstance(obj, dict):
        urls = obj.get("url")
        if isinstance(urls, list):
            for u in urls:
                if isinstance(u, dict) and "href" in u and 'rel' in u and u.get("rel") == "canonical":
                    href = u.get("href")
                    break

    if not href:
        logger.error("Could not extract href from X-Islandora-Event payload")
        raise HTTPException(status_code=400, detail="Could not extract href from X-Islandora-Event payload")

    # Build the members list URL
    members_url = f"{href.rstrip('/')}/members-list?_format=json"
    logger.debug(f"Fetching members list from: {members_url}")
    
    try:
        # Fetch the members list
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            response = await client.get(members_url)
            response.raise_for_status()
            members_data = response.json()
    except httpx.ConnectError as e:
        logger.error(f"Failed to connect to {members_url}: {str(e)}")
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach the resource URL: {href}. Ensure the URL is accessible and the container has network access."
        )
    except httpx.TimeoutException as e:
        logger.error(f"Request timed out: {str(e)}")
        raise HTTPException(status_code=504, detail="Request to resource URL timed out")
    except Exception as e:
        logger.error(f"Error fetching members list: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching members list: {str(e)}")
    
    # Validate that we got a list
    if not isinstance(members_data, list):
        raise HTTPException(status_code=400, detail="Expected JSON array from members-list endpoint")
    
    logger.debug(f"Retrieved {len(members_data)} members from {members_url}")
    
    # Group files by nid and get the first file for each
    files_by_nid = {}
    for member in members_data:
        if not isinstance(member, dict):
            continue
        
        nid = member.get("nid")
        if not nid:
            continue
        
        # Skip if we already have a file for this nid
        if nid in files_by_nid:
            continue
        
        # Find the first field with a URL (file)
        file_url = None
        for key, value in member.items():
            if key.startswith("field_") and isinstance(value, str) and value.startswith(("http://", "https://")):
                file_url = value
                break
        
        if file_url:
            files_by_nid[nid] = file_url
    
    logger.debug(f"Found {len(files_by_nid)} unique nids with files")
    
    if not files_by_nid:
        raise HTTPException(status_code=400, detail=f"No files found in members data for {members_url}")
    
    # Use persistent temp directory for file processing
    processing_dir = os.path.join(PERSISTENT_TEMP_DIR, f"request_{id(href)}")
    os.makedirs(processing_dir, exist_ok=True)
    
    try:
        pdf_files = []
        
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            for nid, file_url in files_by_nid.items():
                try:
                    logger.debug(f"Downloading file for nid {nid} from: {file_url}")
                    response = await client.get(file_url)
                    response.raise_for_status()
                    
                    # Determine file extension from content-type
                    content_type = response.headers.get("content-type", "application/octet-stream")
                    file_bytes = response.content
                    
                    # Convert non-PDF files to PDF
                    pdf_path = await convert_to_pdf(file_bytes, content_type, processing_dir, nid)
                    if pdf_path:
                        pdf_files.append(pdf_path)
                
                except httpx.ConnectError as e:
                    logger.error(f"Failed to download file from {file_url}: {str(e)}")
                    continue
                except httpx.TimeoutException as e:
                    logger.error(f"Timeout downloading from {file_url}: {str(e)}")
                    continue
                except Exception as e:
                    logger.error(f"Error processing file for nid {nid}: {str(e)}")
                    continue
        
        if not pdf_files:
            raise HTTPException(status_code=500, detail="Could not convert any files to PDF")
        
        # Merge PDFs
        logger.debug(f"Merging {len(pdf_files)} PDF files for {members_url}")
        merged_pdf_path = await merge_pdf_files(pdf_files, processing_dir)
        
        logger.info(f"Successfully created merged PDF ({merged_pdf_path}) for {members_url}")
        
        # Extract base URL from href
        parsed_href = urlparse(href)
        base_url = f"{parsed_href.scheme}://{parsed_href.netloc}"

        # Extract authorization token from request headers
        auth_token = request.headers.get("Authorization")

        # Fetch TID from term endpoint
        tid_endpoint = f"{base_url}/term_from_term_name?vocab=islandora_media_use&name=Service+File&_format=json"
        logger.debug(f"Fetching TID from: {tid_endpoint}")

        try:
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                tid_response = await client.get(tid_endpoint, headers={"Authorization": auth_token})
                tid_response.raise_for_status()
                tid_data = tid_response.json()

                # Extract tid from .[0].tid[0].value
                if isinstance(tid_data, list) and len(tid_data) > 0:
                    first_item = tid_data[0]
                    if isinstance(first_item, dict) and "tid" in first_item:
                        tid_list = first_item["tid"]
                        if isinstance(tid_list, list) and len(tid_list) > 0:
                            tid_obj = tid_list[0]
                            tid = tid_obj.get("value") if isinstance(tid_obj, dict) else tid_obj
                            logger.debug(f"Extracted TID: {tid}")
                        else:
                            raise ValueError("tid array is empty or not found")
                    else:
                        raise ValueError("First item does not have tid field")
                else:
                    raise ValueError("TID response is not a list or is empty")
        except Exception as e:
            logger.error(f"Failed to fetch TID: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch TID: {str(e)}")

        # PUT the PDF to the media/document endpoint
        put_url = f"{href}/media/document/{tid}"
        logger.debug(f"Putting merged PDF to: {put_url}")

        put_headers = {
            "Content-Type": "application/pdf",
            "Content-Location": f"private://{datetime.now().strftime('%Y-%m')}/{nid}-ServiceFile.pdf"
            }
        if auth_token:
            put_headers["Authorization"] = auth_token
            logger.debug("Using Authorization token from incoming request")

        try:
            with open(merged_pdf_path, "rb") as pdf_file:
                pdf_content = pdf_file.read()

            async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
                put_response = await client.put(
                    put_url,
                    content=pdf_content,
                    headers=put_headers
                )
                put_response.raise_for_status()
                logger.info(f"Successfully PUT PDF to {put_url}")
        except Exception as e:
            logger.error(f"Failed to PUT PDF: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to PUT PDF: {str(e)}")

        # Schedule cleanup of the processing directory
        if background_tasks is not None:
            background_tasks.add_task(shutil.rmtree, processing_dir, ignore_errors=True)

        return {"status": "success", "message": f"PDF successfully merged and uploaded to {put_url}"}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in merge_pdfs for {members_url}: {str(e)}")
        # Clean up processing directory on error (only if not KEEP_FILES)
        try:
            if not KEEP_FILES:
                shutil.rmtree(processing_dir, ignore_errors=True)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")



async def _fit_image_to_pdf(file_bytes: bytes, temp_dir: str, identifier: str) -> str:
    """
    Convert an image to PDF, fitting it within a standard letter-size canvas (8.5" x 11").
    The image is scaled proportionally to fit within the canvas and centered on a white background.
    Returns the path to the created PDF.
    """
    image = Image.open(io.BytesIO(file_bytes))
    logger.debug(f"{image.filename} ({image.format}), {image.size}")
    
    # Convert to RGB if necessary (for RGBA, LA, P modes)
    if image.mode in ("RGBA", "LA", "P"):
        rgb_image = Image.new("RGB", image.size, (255, 255, 255))
        rgb_image.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
        image = rgb_image
    elif image.mode != "RGB":
        image = image.convert("RGB")
    
    # Force orientation
    image = ImageOps.exif_transpose(image)
    if image.width > image.height:
        logger.debug(f"Rotating image {image.filename} ({image.width}x{image.height}) to portrait.")
        # Rotate 90 degrees clockwise
        image = image.transpose(Image.ROTATE_90)

    # Get original image dimensions
    orig_width, orig_height = image.size
    
    # Calculate scaling factor to fit within the letter canvas while maintaining aspect ratio
    scale_width = LETTER_WIDTH_PX / orig_width
    scale_height = LETTER_HEIGHT_PX / orig_height
    scale_factor = min(scale_width, scale_height, 1.0)  # Don't upscale
    
    # Calculate new image dimensions
    new_width = int(orig_width * scale_factor)
    new_height = int(orig_height * scale_factor)
    
    # Resize the image if needed
    if scale_factor < 1.0:
        image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        logger.debug(f"Scaled image from {orig_width}x{orig_height} to {new_width}x{new_height}")
    
    # OCR and convert to PDF
    pdf_bytes = pytesseract.image_to_pdf_or_hocr(image, extension='pdf', config=f"--dpi {DPI}")
    pdf_reader = PdfReader(io.BytesIO(pdf_bytes))

    # Force PDF to Letter-sized (8.5x11) page
    letter_page = PageObject.create_blank_page(width=(LETTER_WIDTH_PX * (72.0 / DPI)), height=(LETTER_HEIGHT_PX * (72.0 / DPI)))
    letter_page.merge_page(pdf_reader.pages[0])

    # Save PDF
    pdf_path = os.path.join(temp_dir, f"file_{identifier}.pdf")
    pdf_writer = PdfWriter()
    pdf_writer.add_page(letter_page)
    with open(pdf_path, "wb") as f:
        pdf_writer.write(f)

    return pdf_path


async def convert_to_pdf(file_bytes: bytes, content_type: str, temp_dir: str, identifier: str) -> Optional[str]:
    """
    Convert a file to PDF if it's not already a PDF.
    Returns the path to the PDF file.
    """
    # Check if already a PDF
    if content_type == "application/pdf" or file_bytes.startswith(b"%PDF"):
        # Save as-is
        pdf_path = os.path.join(temp_dir, f"file_{identifier}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(file_bytes)
        return pdf_path
    
    # Convert image formats to PDF
    if content_type.startswith("image/"):
        try:
            return await _fit_image_to_pdf(file_bytes, temp_dir, identifier)
        except Exception as e:
            logger.error(f"Failed to convert image {identifier} to PDF: {str(e)}")
            return None
    
    # For other formats, try to treat as image
    try:
        return await _fit_image_to_pdf(file_bytes, temp_dir, identifier)
    except Exception as e:
        logger.warning(f"Could not convert file {identifier} with content-type {content_type}: {str(e)}")
        return None


async def merge_pdf_files(pdf_paths: list, temp_dir: str) -> str:
    """
    Merge multiple PDF files into a single PDF.
    Returns the path to the merged PDF.
    """
    writer = PdfWriter()

    try:
        for pdf_path in pdf_paths:
            try:
                reader = PdfReader(pdf_path)
                for page in reader.pages:
                    writer.add_page(page)
            except Exception as e:
                logger.error(f"Failed to read PDF {pdf_path}: {e}")
                raise

        merged_path = os.path.join(temp_dir, "merged.pdf")
        with open(merged_path, "wb") as out_f:
            writer.write(out_f)

        logger.debug(f"Successfully merged PDFs to: {merged_path}")
        return merged_path

    except Exception as e:
        logger.error(f"Failed to merge PDFs: {str(e)}")
        raise


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
