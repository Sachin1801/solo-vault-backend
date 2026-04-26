from docx import Document


def parse_docx(local_path: str) -> str:
    doc = Document(local_path)
    return "\n".join([p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()])
