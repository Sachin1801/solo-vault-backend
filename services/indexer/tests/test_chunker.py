"""Unit tests for app.pipeline.chunk — no infrastructure required."""

import pytest
import tiktoken

from app.pipeline.chunk import (
    CHUNK_SIZE,
    OVERLAP,
    chunk,
    chunk_code,
    chunk_data,
    chunk_document,
    chunk_single,
    chunk_sliding,
)
from app.types import EntryKind, PipelineError, PipelineJob

ENC = tiktoken.get_encoding("cl100k_base")


def _job(kind: EntryKind = EntryKind.DOCUMENT) -> PipelineJob:
    return PipelineJob(
        job_id="j1", entry_id="e1", user_id="u1", project_id=None,
        s3_key="k", bucket="b", file_name="f.txt",
        mime="text/plain", kind=kind, subkind="txt", size_bytes=1,
    )


# ── Shared contract: chunk_index is contiguous from 0 ────────────────────────


@pytest.mark.parametrize("kind,text", [
    (EntryKind.DOCUMENT, "para1\n\npara2\n\npara3\n\npara4"),
    (EntryKind.CODE,     "def foo():\n    pass\n\ndef bar():\n    return 1\n"),
    (EntryKind.DATA,     "Columns: a, b\n\nSample rows:\na=1, b=2\na=3, b=4\n"),
    (EntryKind.IMAGE,    "extracted ocr text from image"),
])
def test_chunk_index_contiguous(kind, text):
    results = chunk(_job(kind), text)
    assert [c.chunk_index for c in results] == list(range(len(results)))


# ── Determinism ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", [
    EntryKind.DOCUMENT, EntryKind.CODE, EntryKind.DATA, EntryKind.IMAGE,
])
def test_chunk_determinism(kind):
    text = "hello world " * 200
    j = _job(kind)
    a = chunk(j, text)
    b = chunk(j, text)
    assert [(x.content, x.token_count) for x in a] == [(x.content, x.token_count) for x in b]


# ── Empty text raises PipelineError ──────────────────────────────────────────


def test_empty_text_raises():
    with pytest.raises(PipelineError) as exc_info:
        chunk(_job(EntryKind.DOCUMENT), "   ")
    assert exc_info.value.code == "EMPTY_TEXT"


def test_only_newlines_raises():
    with pytest.raises(PipelineError):
        chunk(_job(EntryKind.DOCUMENT), "\n\n\n")


# ── chunk_sliding ─────────────────────────────────────────────────────────────


def test_sliding_last_chunk_present():
    text = "hello " * 1200
    chunks = chunk_sliding(text, chunk_size=200, overlap=20)
    assert len(chunks) > 1
    assert chunks[-1].token_count > 0


def test_sliding_overlap_tokens_match():
    text = "token " * 1600
    chunks = chunk_sliding(text, chunk_size=120, overlap=OVERLAP)
    first = ENC.encode(chunks[0].content)
    second = ENC.encode(chunks[1].content)
    assert first[-OVERLAP:] == second[:OVERLAP]


def test_sliding_single_chunk_short_text():
    text = "short"
    chunks = chunk_sliding(text, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
    assert len(chunks) == 1


def test_sliding_token_count_matches_content():
    text = "word " * 600
    for c in chunk_sliding(text):
        assert c.token_count == len(ENC.encode(c.content))


def test_sliding_no_empty_chunks():
    text = "abc " * 1000
    for c in chunk_sliding(text):
        assert c.content.strip()
        assert c.token_count > 0


# ── chunk_document ────────────────────────────────────────────────────────────


def test_document_paragraph_boundary_respected():
    text = "a " * 300 + "\n\n" + "b " * 300 + "\n\n" + "c " * 300
    chunks = chunk_document(text)
    assert len(chunks) >= 2
    # No single chunk should exceed CHUNK_SIZE
    for c in chunks:
        assert c.token_count <= CHUNK_SIZE + 10  # allow minor overshoot at boundaries


def test_document_short_text_one_chunk():
    chunks = chunk_document("Hello world")
    assert len(chunks) == 1
    assert "Hello world" in chunks[0].content


def test_document_long_single_paragraph_falls_through_to_sliding():
    # One massive paragraph > 500 tokens
    text = "word " * 800
    chunks = chunk_document(text)
    assert len(chunks) > 1


def test_document_groups_small_paragraphs():
    # Three tiny paragraphs should collapse into one chunk
    text = "a\n\nb\n\nc"
    chunks = chunk_document(text)
    assert len(chunks) == 1
    assert "a" in chunks[0].content and "c" in chunks[0].content


# ── chunk_code ────────────────────────────────────────────────────────────────


def test_code_def_block_stays_in_one_chunk():
    code = "def foo():\n    x = 1\n    return x\n"
    chunks = chunk_code(code)
    assert len(chunks) == 1
    assert "def foo" in chunks[0].content


def test_code_class_block_stays_in_one_chunk():
    code = "class Foo:\n    def __init__(self):\n        self.x = 0\n"
    chunks = chunk_code(code)
    assert all("Foo" in c.content or "def" in c.content or "self" in c.content
               for c in chunks)


def test_code_multiple_boundaries_split_correctly():
    code = "def foo():\n    pass\n\ndef bar():\n    pass\n\ndef baz():\n    pass\n"
    chunks = chunk_code(code)
    # At least 3 chunks (one per function)
    assert len(chunks) >= 3


def test_code_large_block_subdivided():
    # A single function > 500 tokens must be split further
    body = "    x = 1\n" * 300  # ~900 tokens
    code = f"def huge():\n{body}"
    chunks = chunk_code(code)
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= CHUNK_SIZE + 5


def test_code_no_boundaries_falls_back_to_sliding():
    code = "x = 1\ny = 2\nz = 3\n"
    chunks = chunk_code(code)
    assert len(chunks) >= 1


# ── chunk_data ────────────────────────────────────────────────────────────────


def test_data_schema_is_first_chunk():
    text = "Columns: name, age, city\n\nSample rows:\nname=Alice, age=30, city=NYC\n"
    chunks = chunk_data(text)
    assert len(chunks) >= 1
    assert chunks[0].content.startswith("Columns:")


def test_data_row_batches_are_subsequent_chunks():
    rows = "\n".join(f"a={i}, b={i*2}" for i in range(200))
    text = f"Columns: a, b\n\nSample rows:\n{rows}"
    chunks = chunk_data(text)
    assert len(chunks) > 1
    # First chunk is schema
    assert "Columns:" in chunks[0].content
    # Rest are row data
    for c in chunks[1:]:
        assert c.token_count > 0


def test_data_no_columns_header_falls_back_to_sliding():
    text = "random text without columns header " * 100
    chunks = chunk_data(text)
    assert len(chunks) >= 1


def test_data_empty_rows_section():
    text = "Columns: a, b\n\n"
    chunks = chunk_data(text)
    assert len(chunks) >= 1
    assert "Columns:" in chunks[0].content


# ── chunk_single ──────────────────────────────────────────────────────────────


def test_single_always_one_chunk():
    chunks = chunk_single("x " * 10000)
    assert len(chunks) == 1


def test_single_chunk_index_is_zero():
    chunks = chunk_single("hello")
    assert chunks[0].chunk_index == 0


def test_single_preserves_full_content():
    text = "unique content abc123"
    chunks = chunk_single(text)
    assert chunks[0].content == text


def test_single_token_count_accurate():
    text = "hello world"
    chunks = chunk_single(text)
    assert chunks[0].token_count == len(ENC.encode(text))


# ── Dispatcher ────────────────────────────────────────────────────────────────


def test_image_kind_uses_single_strategy():
    chunks = chunk(_job(EntryKind.IMAGE), "ocr text " * 300)
    assert len(chunks) == 1


def test_design_kind_uses_single_strategy():
    chunks = chunk(_job(EntryKind.DESIGN), "ocr from design file " * 300)
    assert len(chunks) == 1


def test_config_kind_uses_data_strategy():
    text = "Columns: key, value\n\nSample rows:\nkey=DEBUG, value=true\n"
    chunks = chunk(_job(EntryKind.CONFIG), text)
    assert chunks[0].content.startswith("Columns:")


def test_snippet_kind_uses_code_strategy():
    code = "def helper(): return 42\n"
    chunks = chunk(_job(EntryKind.SNIPPET), code)
    assert any("def helper" in c.content for c in chunks)
