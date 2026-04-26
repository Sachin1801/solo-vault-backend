import re

import tiktoken

from app.types import ChunkResult, EntryKind, PipelineError, PipelineJob

CHUNK_SIZE = 500
OVERLAP = 50
ENC = tiktoken.get_encoding("cl100k_base")


def chunk_sliding(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[ChunkResult]:
    tokens = ENC.encode(text)
    chunks: list[ChunkResult] = []
    i = 0
    idx = 0
    step = max(1, chunk_size - overlap)
    while i < len(tokens):
        window = tokens[i : i + chunk_size]
        chunks.append(ChunkResult(chunk_index=idx, content=ENC.decode(window), token_count=len(window)))
        i += step
        idx += 1
    return chunks


def _split_code_blocks(text: str) -> list[str]:
    pattern = re.compile(r"(?im)^(def |class |function |fn |func |sub |procedure )")
    starts = [m.start() for m in pattern.finditer(text)]
    if not starts:
        return [text]
    blocks: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks or [text]


def chunk_code(text: str) -> list[ChunkResult]:
    results: list[ChunkResult] = []
    idx = 0
    for block in _split_code_blocks(text):
        tcount = len(ENC.encode(block))
        if tcount <= CHUNK_SIZE:
            results.append(ChunkResult(chunk_index=idx, content=block, token_count=tcount))
            idx += 1
            continue
        for part in chunk_sliding(block, chunk_size=CHUNK_SIZE, overlap=0):
            part.chunk_index = idx
            results.append(part)
            idx += 1
    return results


def chunk_document(text: str) -> list[ChunkResult]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    results: list[ChunkResult] = []
    current: list[str] = []
    idx = 0
    for p in paragraphs:
        candidate = ("\n\n".join(current + [p])).strip()
        if len(ENC.encode(candidate)) <= CHUNK_SIZE:
            current.append(p)
            continue
        if current:
            content = "\n\n".join(current).strip()
            tokens = ENC.encode(content)
            results.append(ChunkResult(chunk_index=idx, content=content, token_count=len(tokens)))
            idx += 1
            current = []
        if len(ENC.encode(p)) > CHUNK_SIZE:
            for part in chunk_sliding(p):
                part.chunk_index = idx
                results.append(part)
                idx += 1
        else:
            current = [p]
    if current:
        content = "\n\n".join(current).strip()
        tokens = ENC.encode(content)
        results.append(ChunkResult(chunk_index=idx, content=content, token_count=len(tokens)))
    return results


def chunk_data(text: str) -> list[ChunkResult]:
    if not text.startswith("Columns:"):
        return chunk_sliding(text)
    parts = text.split("\n\n", maxsplit=1)
    schema = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    rows = [line for line in body.splitlines() if line.strip()]
    results: list[ChunkResult] = []
    results.append(ChunkResult(chunk_index=0, content=schema, token_count=len(ENC.encode(schema))))
    idx = 1
    batch: list[str] = []
    for row in rows:
        candidate = "\n".join(batch + [row]).strip()
        if len(ENC.encode(candidate)) <= CHUNK_SIZE:
            batch.append(row)
            continue
        if batch:
            content = "\n".join(batch)
            results.append(ChunkResult(chunk_index=idx, content=content, token_count=len(ENC.encode(content))))
            idx += 1
        batch = [row]
    if batch:
        content = "\n".join(batch)
        results.append(ChunkResult(chunk_index=idx, content=content, token_count=len(ENC.encode(content))))
    return results


def chunk_single(text: str) -> list[ChunkResult]:
    tokens = ENC.encode(text)
    return [ChunkResult(chunk_index=0, content=text, token_count=len(tokens))]


CHUNK_DISPATCH = {
    EntryKind.DOCUMENT: chunk_document,
    EntryKind.NOTE: chunk_document,
    EntryKind.WEB: chunk_document,
    EntryKind.CODE: chunk_code,
    EntryKind.SNIPPET: chunk_code,
    EntryKind.DATA: chunk_data,
    EntryKind.CONFIG: chunk_data,
    EntryKind.IMAGE: chunk_single,
    EntryKind.DESIGN: chunk_single,
}


def chunk(job: PipelineJob, extracted_text: str) -> list[ChunkResult]:
    if not extracted_text.strip():
        raise PipelineError("Cannot chunk empty text", "EMPTY_TEXT")
    fn = CHUNK_DISPATCH.get(job.kind, chunk_sliding)
    results = fn(extracted_text)
    for i, r in enumerate(results):
        r.chunk_index = i
    return results
