import logging
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
import httpx
import tempfile
import os
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_bytes
from PIL import Image
import io
from io import BytesIO
import atexit
import shutil
import pytesseract

# Standard DPI for PDF rendering
DPI = int(os.getenv("MERGEPDF_DPI", "72"))

# Letter size in pixels at standard DPI
LETTER_WIDTH_PX = int(8.5 * DPI)
LETTER_HEIGHT_PX = int(11.0 * DPI)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Merge PDF API", version="1.0.0")


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
async def merge_pdfs(apix_ldp_resource: Optional[str] = Header(None), background_tasks: BackgroundTasks = None):
    """
    Merge PDFs from a given resource URL.
    
    Expects the Apix-Ldp-Resource header containing a URL.
    Appends '/members-list?_format=json' to fetch the list of documents.
    Downloads and merges the first file for each nid into a single PDF.
    """
    if not apix_ldp_resource:
        raise HTTPException(status_code=400, detail="Apix-Ldp-Resource header is required")
    
    # Build the members list URL
    members_url = f"{apix_ldp_resource.rstrip('/')}/members-list?_format=json"
    logger.info(f"Fetching members list from: {members_url}")
    
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
            detail=f"Cannot reach the resource URL: {apix_ldp_resource}. Ensure the URL is accessible and the container has network access."
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
    
    logger.info(f"Retrieved {len(members_data)} members")
    
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
    
    logger.info(f"Found {len(files_by_nid)} unique nids with files")
    
    if not files_by_nid:
        raise HTTPException(status_code=400, detail="No files found in members data")
    
    # Use persistent temp directory for file processing
    processing_dir = os.path.join(PERSISTENT_TEMP_DIR, f"request_{id(apix_ldp_resource)}")
    os.makedirs(processing_dir, exist_ok=True)
    
    try:
        pdf_files = []
        
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            for nid, file_url in files_by_nid.items():
                try:
                    logger.info(f"Downloading file for nid {nid} from: {file_url}")
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
        logger.info(f"Merging {len(pdf_files)} PDF files")
        merged_pdf_path = await merge_pdf_files(pdf_files, processing_dir)
        
        logger.info(f"Successfully created merged PDF at: {merged_pdf_path}")
        
        # Schedule cleanup of the processing directory after response if KEEP_FILES is not enabled
        if not KEEP_FILES and background_tasks is not None:
            background_tasks.add_task(shutil.rmtree, processing_dir, True)
            return FileResponse(
                merged_pdf_path,
                media_type="application/pdf",
                filename="merged.pdf",
                background=background_tasks,
            )

        # If KEEP_FILES is enabled or no background_tasks provided, just return the file directly
        return FileResponse(
            merged_pdf_path,
            media_type="application/pdf",
            filename="merged.pdf"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in merge_pdfs: {str(e)}")
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
    
    # Convert to RGB if necessary (for RGBA, LA, P modes)
    if image.mode in ("RGBA", "LA", "P"):
        rgb_image = Image.new("RGB", image.size, (255, 255, 255))
        rgb_image.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
        image = rgb_image
    elif image.mode != "RGB":
        image = image.convert("RGB")
    
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
        logger.info(f"Scaled image from {orig_width}x{orig_height} to {new_width}x{new_height}")
    
    # Create a white canvas at letter size with proper DPI info
    canvas = Image.new("RGB", (LETTER_WIDTH_PX, LETTER_HEIGHT_PX), (255, 255, 255))
    canvas.info['dpi'] = (DPI, DPI)
    
    # Calculate position to center the image on the canvas
    x_offset = (LETTER_WIDTH_PX - new_width) // 2
    y_offset = (LETTER_HEIGHT_PX - new_height) // 2
    
    # Paste the image onto the canvas
    canvas.paste(image, (x_offset, y_offset))
    
    # Save as PDF
    pdf_path = os.path.join(temp_dir, f"file_{identifier}.pdf")
    canvas.save(pdf_path, "PDF", dpi=(DPI, DPI))
    logger.info(f"Converted image to PDF with letter-size canvas: {pdf_path}")
    
    # Apply OCR using Tesseract to create a searchable PDF with embedded text
    try:
        logger.info(f"Applying OCR to image for nid {identifier}")
        # Use pytesseract with pdf extension to create a searchable PDF from the canvas image
        # This preserves the resized canvas dimensions
        pdf_data = pytesseract.image_to_pdf_or_hocr(canvas, extension='pdf')

        # Read OCR PDF bytes, scale pages to letter points to ensure correct physical size,
        # then write out the scaled, searchable PDF.
        try:
            ocr_reader = PdfReader(BytesIO(pdf_data))
            ocr_writer = PdfWriter()
            for page in ocr_reader.pages:
                try:
                    # Get current page size in PDF points (1 point = 1/72 inch)
                    current_width_pts = float(page.mediabox.width)
                    current_height_pts = float(page.mediabox.height)

                    # Convert page size from points to pixels at configured DPI
                    current_width_px = current_width_pts * (DPI / 72.0)
                    current_height_px = current_height_pts * (DPI / 72.0)

                    # Only scale down if the page is larger than our target letter canvas in pixels
                    if current_width_px > LETTER_WIDTH_PX or current_height_px > LETTER_HEIGHT_PX:
                        scale_factor = min(LETTER_WIDTH_PX / current_width_px, LETTER_HEIGHT_PX / current_height_px)
                        target_width_pts = current_width_pts * scale_factor
                        target_height_pts = current_height_pts * scale_factor
                        try:
                            page.scale_to(target_width_pts, target_height_pts)
                        except Exception:
                            # Fallback: adjust mediabox to target sizes in points
                            try:
                                page.mediabox.right = target_width_pts
                                page.mediabox.top = target_height_pts
                            except Exception:
                                pass
                except Exception as e:
                    logger.warning(f"Could not inspect/scale OCR page: {e}")
                ocr_writer.add_page(page)

            with open(pdf_path, 'wb') as f:
                ocr_writer.write(f)

            logger.info(f"OCR applied, scaled and embedded in PDF: {pdf_path}")
        except Exception as e:
            # If anything goes wrong with scaling, fall back to writing raw OCR PDF bytes
            logger.warning(f"Failed to scale OCR PDF pages: {e}. Falling back to raw OCR output.")
            with open(pdf_path, 'wb') as f:
                f.write(pdf_data)
    except Exception as e:
        logger.warning(f"OCR embedding failed for image {identifier}: {str(e)}. PDF created without embedded text layer.")
    
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
            logger.error(f"Failed to convert image to PDF: {str(e)}")
            return None
    
    # For other formats, try to treat as image
    try:
        return await _fit_image_to_pdf(file_bytes, temp_dir, identifier)
    except Exception as e:
        logger.warning(f"Could not convert file with content-type {content_type}: {str(e)}")
        return None


async def merge_pdf_files(pdf_paths: list, temp_dir: str) -> str:
    """
    Merge multiple PDF files into a single PDF.
    Returns the path to the merged PDF.
    """
    writer = PdfWriter()

    # # Define the target size in points (Letter size: 8.5" x 11")
    # target_width = 612 # 72 ppi * 8.5 inches
    # target_height = 792 # 72 ppi * 11 inches

    try:
        for pdf_path in pdf_paths:
            try:
                reader = PdfReader(pdf_path)
                for page in reader.pages:
                    # # Calculate scaling factor to fit content within new dimensions
                    # current_width = page.mediabox.width
                    # current_height = page.mediabox.height
                    
                    # # Scale proportionally to fit within the new size while maintaining aspect ratio
                    # scale_factor_width = target_width / current_width
                    # scale_factor_height = target_height / current_height
                    # scale_factor = min(scale_factor_width, scale_factor_height)

                    # # Apply scaling to the content
                    # page.scale_by(scale_factor)

                    # # Set the page size (MediaBox, CropBox, etc.) to the target Letter size
                    # # This creates a new 'canvas' size for the page
                    # page.mediabox.right = target_width
                    # page.mediabox.top = target_height
                    # page.cropbox.right = target_width
                    # page.cropbox.top = target_height
                    # page.bleedbox.right = target_width
                    # page.bleedbox.top = target_height
                    # page.artbox.right = target_width
                    # page.artbox.top = target_height
                    writer.add_page(page)
            except Exception as e:
                logger.error(f"Failed to read PDF {pdf_path}: {e}")
                raise

        merged_path = os.path.join(temp_dir, "merged.pdf")
        with open(merged_path, "wb") as out_f:
            writer.write(out_f)

        logger.info(f"Successfully merged PDFs to: {merged_path}")
        return merged_path

    except Exception as e:
        logger.error(f"Failed to merge PDFs: {str(e)}")
        raise


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
