from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EntryKind(str, Enum):
    DOCUMENT = "document"
    CODE = "code"
    SNIPPET = "snippet"
    IMAGE = "image"
    DESIGN = "design"
    DATA = "data"
    CONFIG = "config"
    WEB = "web"
    NOTE = "note"
    KEYVALUE = "keyvalue"
    AUDIO = "audio"
    ARCHIVE = "archive"
    UNSORTED = "unsorted"


@dataclass
class PipelineJob:
    job_id: str
    entry_id: str
    user_id: str
    project_id: str | None
    s3_key: str
    bucket: str
    file_name: str
    mime: str
    kind: EntryKind
    subkind: str
    size_bytes: int
    title: str = ""
    tags: list[str] = field(default_factory=list)
    classifier_confidence: float = 1.0
    pinned: bool = False
    memory_type: str = ""
    file_hash: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload


@dataclass
class ChunkResult:
    chunk_index: int
    content: str
    token_count: int


@dataclass
class EmbedResult:
    chunk_index: int
    content: str
    embedding: list[float]
    token_count: int


class PipelineError(Exception):
    def __init__(self, message: str, code: str = "PIPELINE_ERROR"):
        super().__init__(message)
        self.code = code
