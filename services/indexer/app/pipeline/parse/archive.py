import shutil
import tempfile
import zipfile
from pathlib import Path

from app.types import EntryKind, PipelineJob


def _infer_kind(path: Path) -> EntryKind:
    suffix = path.suffix.lower()
    if suffix in {".pdf", ".docx"}:
        return EntryKind.DOCUMENT
    if suffix in {".md", ".txt", ".rtf"}:
        return EntryKind.NOTE
    if suffix in {".csv", ".json", ".yaml", ".yml", ".toml"}:
        return EntryKind.DATA
    if suffix in {".html", ".htm"}:
        return EntryKind.WEB
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".heic", ".svg"}:
        return EntryKind.IMAGE
    if suffix in {".zip"}:
        return EntryKind.ARCHIVE
    if suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rb", ".c", ".cpp", ".h", ".sql", ".sh", ".lua", ".rs", ".kt", ".swift"}:
        return EntryKind.CODE
    return EntryKind.UNSORTED


def parse_archive(job: PipelineJob, local_path: str) -> str:
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"archive_{job.entry_id}_"))
    outputs: list[str] = []
    try:
        with zipfile.ZipFile(local_path) as zf:
            zf.extractall(tmp_dir)
        members = [p for p in tmp_dir.rglob("*") if p.is_file()]
        count = 0
        for member in members:
            relative = member.relative_to(tmp_dir).as_posix()
            if relative.startswith("__MACOSX") or member.name == ".DS_Store" or member.name.startswith("."):
                continue
            if member.stat().st_size > 10 * 1024 * 1024:
                continue
            count += 1
            if count > 100:
                break
            kind = _infer_kind(member)
            stub = PipelineJob(
                job_id=f"{job.job_id}::{relative}",
                entry_id=f"{job.entry_id}::{relative}",
                user_id=job.user_id,
                project_id=job.project_id,
                s3_key=job.s3_key,
                bucket=job.bucket,
                file_name=member.name,
                mime=job.mime,
                kind=kind,
                subkind=member.suffix.lower().lstrip("."),
                size_bytes=member.stat().st_size,
                title=job.title,
                tags=list(job.tags),
                classifier_confidence=job.classifier_confidence,
                pinned=job.pinned,
                memory_type=job.memory_type,
            )
            from app.pipeline.parse import parse

            outputs.append(f"## {relative}\n{parse(stub, str(member))}")
        return "\n\n---\n\n".join(outputs)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
