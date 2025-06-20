import os
import logging
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Request
from sqlmodel import Session
from app.database import get_db
from app.models import OCRResultCreate
from app.crud import save_ocr_results_bulk, save_carrier_data_bulk
from app.helpers.ocr import cloud_ocr_from_image_file
from app.helpers.safer_web import safer_web_lookup_from_dot
from app.routes.auth import verify_login
from google.cloud import vision
from safer import CompanySnapshot
from fastapi.responses import JSONResponse

# Set up a module-level logger
logger = logging.getLogger(__name__)

# Initialize Google Cloud Vision client
vision_client = vision.ImageAnnotatorClient(
    client_options={"api_key": os.environ.get("GCP_OCR_API_KEY")}
)

# Initialize SAFER web crawler
safer_client = CompanySnapshot()

# Initialize APIRouter
router = APIRouter()


@router.post("/upload",
             dependencies=[Depends(verify_login)])
async def upload_file(files: list[UploadFile] = File(...), 
                      request: Request = None,
                      db: Session = Depends(get_db)):
    ocr_records = []  # Store OCR results before batch insert
    valid_files = []
    invalid_files = []
    user_id = request.session['userinfo']['sub']
    org_id = (request.session['userinfo']['org_id'] 
                if 'org_id' in request.session['userinfo'] else user_id)
    
    for file in files:
        try:
            # Validate file type
            supported_types = ('.png', '.jpg', '.jpeg', '.bmp', '.heic', '.heif')
            if not file.filename.lower().endswith(supported_types):
                logger.error(f"❌ Invalid file type. Only image files {supported_types} are allowed.")
                invalid_files.append(file.filename)
                continue         
               
            # perform OCR on image
            ocr_text = await cloud_ocr_from_image_file(vision_client, file)
            ocr_record = OCRResultCreate(extracted_text=ocr_text, 
                                         filename=file.filename,
                                         user_id=user_id,
                                         org_id=org_id)
            ocr_records.append(ocr_record)
            valid_files.append(file.filename)
        except Exception as e:
            logger.exception(f"❌ Error processing file: {e}")
    
    if not ocr_records:
        raise HTTPException(status_code=400, detail="No valid files were processed.")
    


    if ocr_records:
        logger.info("✅ All OCR results saved successfully.")
        safer_lookups = []
        for result in ocr_records:
            
            # Perform SAFER web lookup
            if result.dot_reading:
                safer_data = safer_web_lookup_from_dot(safer_client, result.dot_reading)
                if safer_data.lookup_success_flag:
                    safer_lookups.append(safer_data)

        # Save carrier data to database
        if safer_lookups:
            _ = save_carrier_data_bulk(db, safer_lookups, 
                                       user_id=user_id,
                                       org_id=org_id)
                                       
        # Save to database using schema
        ocr_results = save_ocr_results_bulk(db, ocr_records)       

        logger.info(f"✅ Processed {len(ocr_results)} OCR results, {safer_lookups} carrier records saved.")

    # Collect all OCR result IDs
    ocr_result_ids = [
        {"id": result.id, "dot_reading": result.dot_reading}
        for result in ocr_results
    ]
    
    # Redirect to home with all OCR result IDs
    return JSONResponse(
        content={
            "message": "Processing complete",
            "result_ids": ocr_result_ids,
            "valid_files": valid_files,
            "invalid_files": invalid_files
        },
        status_code=200
    )