import re
from pathlib import Path

from app.config import settings
from app.pipeline.parse.archive import parse_archive
from app.pipeline.parse.code import parse_code_file
from app.pipeline.parse.data import parse_data
from app.pipeline.parse.docling_adapter import parse_with_docling
from app.pipeline.parse.docx import parse_docx
from app.pipeline.parse.image import parse_image
from app.pipeline.parse.pdf import parse_pdf
from app.pipeline.parse.text import parse_text_file
from app.pipeline.parse.web import parse_web
from app.types import EntryKind, PipelineJob


def _normalize(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def parse_document(_: PipelineJob, local_path: str) -> str:
    if settings.parser_prefer_docling:
        docling_text = parse_with_docling(local_path)
        if docling_text:
            return docling_text

    suffix = Path(local_path).suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(local_path)
    if suffix == ".docx":
        return parse_docx(local_path)
    return parse_text_file(local_path)


def parse_code(job: PipelineJob, local_path: str) -> str:
    return parse_code_file(job, local_path)


def parse_text(_: PipelineJob, local_path: str) -> str:
    return parse_text_file(local_path)


def parse_unsorted(_: PipelineJob, local_path: str) -> str:
    try:
        return Path(local_path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "[Binary file]"


def parse(job: PipelineJob, local_path: str) -> str:
    dispatch = {
        EntryKind.DOCUMENT: parse_document,
        EntryKind.CODE: parse_code,
        EntryKind.SNIPPET: parse_code,
        EntryKind.IMAGE: parse_image,
        EntryKind.DESIGN: parse_image,
        EntryKind.DATA: parse_data,
        EntryKind.CONFIG: parse_data,
        EntryKind.WEB: lambda _, path: parse_web(path),
        EntryKind.NOTE: parse_text,
        EntryKind.KEYVALUE: parse_text,
        EntryKind.ARCHIVE: parse_archive,
        EntryKind.UNSORTED: parse_unsorted,
    }
    fn = dispatch.get(job.kind, parse_unsorted)
    return _normalize(fn(job, local_path))
