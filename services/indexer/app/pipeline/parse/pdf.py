from pathlib import Path

from pypdf import PdfReader


def parse_pdf(local_path: str) -> str:
    try:
        reader = PdfReader(local_path)
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        text = "\n\n".join([p for p in pages if p])
        if text and len(text) >= 50:
            return text
    except Exception:
        pass
    return f"[PDF OCR fallback required: {Path(local_path).name}]"
