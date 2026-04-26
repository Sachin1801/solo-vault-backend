from pathlib import Path

from app.types import PipelineJob


def parse_code_file(job: PipelineJob, local_path: str) -> str:
    text = Path(local_path).read_text(encoding="utf-8", errors="replace")
    return f"# File: {job.file_name}\n# Language: {job.subkind}\n\n{text}"
