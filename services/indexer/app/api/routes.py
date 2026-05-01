from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket
from pydantic import BaseModel, Field

from app.config import settings
from app.db.connection import execute_query, execute_write
from app.notify.websocket import ws_progress_handler
from app.types import EntryKind, PipelineJob
from app.workers.pipeline_task import run_pipeline

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class IndexRequest(BaseModel):
    entry_id: str
    user_id: str
    project_id: str | None = None
    s3_key: str
    bucket: str
    file_name: str
    mime: str
    kind: str
    subkind: str
    size_bytes: int
    title: str
    tags: list[str] = Field(default_factory=list)
    classifier_confidence: float = 1.0
    pinned: bool = False
    memory_type: str = ""


@router.post("/index")
def enqueue_index(request: IndexRequest) -> dict[str, str]:
    try:
        kind = EntryKind(request.kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid entry kind") from exc

    if request.classifier_confidence < settings.classifier_confidence_threshold:
        kind = EntryKind.UNSORTED

    job_id = request.entry_id
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    job = PipelineJob(
        job_id=job_id,
        entry_id=request.entry_id,
        user_id=request.user_id,
        project_id=request.project_id,
        s3_key=request.s3_key,
        bucket=request.bucket,
        file_name=request.file_name,
        mime=request.mime,
        kind=kind,
        subkind=request.subkind,
        size_bytes=request.size_bytes,
        title=request.title,
        tags=request.tags,
        classifier_confidence=request.classifier_confidence,
        pinned=request.pinned,
        memory_type=request.memory_type,
        created_at=now_iso,
    )

    execute_write(
        """
        INSERT INTO vault_entries (
            user_id, project_id, entry_id, title, file_name, file_size,
            mime_type, s3_key, kind, subkind, index_status, classifier_confidence,
            pinned, memory_type, tags, metadata, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, '{}'::jsonb, NOW())
        ON CONFLICT (entry_id) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            project_id = EXCLUDED.project_id,
            title = EXCLUDED.title,
            file_name = EXCLUDED.file_name,
            file_size = EXCLUDED.file_size,
            mime_type = EXCLUDED.mime_type,
            s3_key = EXCLUDED.s3_key,
            kind = EXCLUDED.kind,
            subkind = EXCLUDED.subkind,
            index_status = 'pending',
            classifier_confidence = EXCLUDED.classifier_confidence,
            pinned = EXCLUDED.pinned,
            memory_type = EXCLUDED.memory_type,
            tags = EXCLUDED.tags,
            updated_at = NOW()
        """,
        (
            request.user_id,
            request.project_id,
            request.entry_id,
            request.title,
            request.file_name,
            request.size_bytes,
            request.mime,
            request.s3_key,
            kind.value,
            request.subkind,
            request.classifier_confidence,
            request.pinned,
            request.memory_type,
            request.tags,
        ),
    )

    run_pipeline.delay(job.to_dict())
    return {"job_id": job_id, "status": "queued"}


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    rows = execute_query(
        """
        SELECT entry_id, index_status, chunk_count, chunker_version
        FROM vault_entries
        WHERE entry_id = %s
        LIMIT 1
        """,
        (job_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Job not found")

    row = rows[0]
    status = row["index_status"]
    return {
        "job_id": job_id,
        "status": status,
        "entry_id": row["entry_id"],
        "step": status,
        "progress_pct": _status_to_progress(status),
        "chunk_count": row["chunk_count"] or 0,
        "chunker_version": row["chunker_version"] or "",
    }


def _status_to_progress(status: str) -> int:
    status_map = {
        "pending": 0,
        "running": 50,
        "completed": 100,
        "indexed": 100,
        "failed": 0,
        "deleted": 0,
    }
    return status_map.get(status, 0)


@router.websocket("/ws/{entry_id}")
async def ws_progress(websocket: WebSocket, entry_id: str) -> None:
    await ws_progress_handler(websocket, entry_id)
