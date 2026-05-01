from pathlib import Path


def parse_with_docling(local_path: str) -> str:
    try:
        from docling.document_converter import DocumentConverter
    except Exception:
        return ""

    path = Path(local_path)
    converter = DocumentConverter()
    result = converter.convert(str(path))
    document = getattr(result, "document", None)
    if document is None:
        return ""
    if hasattr(document, "export_to_markdown"):
        text = document.export_to_markdown() or ""
    elif hasattr(document, "export_to_text"):
        text = document.export_to_text() or ""
    else:
        text = str(document)
    return text.strip()
