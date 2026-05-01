from pathlib import Path

import pytesseract
from PIL import Image

from app.config import settings
from app.types import PipelineJob


def parse_image(job: PipelineJob, local_path: str) -> str:
    if settings.env != "local":
        return f"[Image OCR placeholder for non-local env: {job.file_name}]"
    text = pytesseract.image_to_string(Image.open(local_path)).strip()
    if text:
        return text
    return f"[Image: {Path(local_path).name}]"
