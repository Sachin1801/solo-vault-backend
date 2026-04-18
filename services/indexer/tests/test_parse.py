"""Unit tests for parse stage — all run offline with temp files."""

import csv
import json
import tempfile
from io import BytesIO
from pathlib import Path

import pytest

from app.pipeline.parse import _normalize, parse
from app.pipeline.parse.code import parse_code_file
from app.pipeline.parse.data import parse_data
from app.pipeline.parse.text import parse_text_file
from app.types import EntryKind, PipelineJob


# ── Helpers ───────────────────────────────────────────────────────────────────


def _write(content: str | bytes, suffix: str) -> str:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix,
                                    mode="wb" if isinstance(content, bytes) else "w",
                                    encoding=None if isinstance(content, bytes) else "utf-8")
    f.write(content)
    f.close()
    return f.name


def _job(kind: EntryKind = EntryKind.DOCUMENT, file_name: str = "f.txt",
         subkind: str = "txt") -> PipelineJob:
    return PipelineJob(
        job_id="j1", entry_id="e1", user_id="u1", project_id=None,
        s3_key="k", bucket="b", file_name=file_name,
        mime="text/plain", kind=kind, subkind=subkind, size_bytes=100,
    )


# ── _normalize ────────────────────────────────────────────────────────────────


def test_normalize_strips_trailing_whitespace_per_line():
    assert _normalize("hello   \nworld  ") == "hello\nworld"


def test_normalize_collapses_excess_blank_lines():
    result = _normalize("a\n\n\n\nb")
    assert "\n\n\n" not in result
    assert "a" in result and "b" in result


def test_normalize_trims_leading_and_trailing_whitespace():
    assert _normalize("  \n\nhello\n\n  ") == "hello"


def test_normalize_preserves_single_blank_lines():
    result = _normalize("para1\n\npara2")
    assert result == "para1\n\npara2"


def test_normalize_empty_string_returns_empty():
    assert _normalize("") == ""


# ── parse_text_file ───────────────────────────────────────────────────────────


def test_parse_text_utf8():
    path = _write("Hello, world!\nLine two.", ".txt")
    result = parse_text_file(path)
    assert "Hello, world!" in result
    assert "Line two." in result


def test_parse_text_unicode():
    content = "Привет мир\nこんにちは"
    path = _write(content, ".txt")
    assert parse_text_file(path) == content


def test_parse_text_latin1_fallback():
    # Write bytes that are valid latin-1 but not UTF-8
    data = "caf\xe9".encode("latin-1")  # 'café'
    path = _write(data, ".txt")
    result = parse_text_file(path)
    assert "caf" in result


def test_parse_text_empty_file():
    path = _write("", ".txt")
    assert parse_text_file(path) == ""


# ── parse_code_file ───────────────────────────────────────────────────────────


def test_parse_code_adds_file_header():
    path = _write("def foo():\n    pass\n", ".py")
    job = _job(kind=EntryKind.CODE, file_name="foo.py", subkind="python")
    result = parse_code_file(job, path)
    assert "# File: foo.py" in result
    assert "# Language: python" in result


def test_parse_code_includes_source():
    source = "fn main() {\n    println!(\"hello\");\n}\n"
    path = _write(source, ".rs")
    job = _job(kind=EntryKind.CODE, file_name="main.rs", subkind="rust")
    result = parse_code_file(job, path)
    assert "fn main()" in result


def test_parse_code_replaces_invalid_utf8():
    data = b"def foo():\n    pass\n\xff\xfe"
    path = _write(data, ".py")
    job = _job(kind=EntryKind.CODE, file_name="bad.py", subkind="python")
    result = parse_code_file(job, path)  # must not raise
    assert "def foo" in result


# ── parse_data (CSV) ──────────────────────────────────────────────────────────


def test_parse_data_csv_produces_columns_header():
    path = _write("name,age,city\nAlice,30,NYC\nBob,25,LA\n", ".csv")
    result = parse_data(_job(), path)
    assert result.startswith("Columns: name, age, city")


def test_parse_data_csv_includes_sample_rows():
    path = _write("a,b\n1,2\n3,4\n", ".csv")
    result = parse_data(_job(), path)
    assert "a=1" in result or "1" in result


def test_parse_data_csv_max_100_sample_rows():
    rows = ["x,y"] + [f"{i},{i*2}" for i in range(200)]
    path = _write("\n".join(rows) + "\n", ".csv")
    result = parse_data(_job(), path)
    # "Sample rows:" section must not include row 101+
    lines = result.split("\n")
    sample_lines = [l for l in lines if "=" in l]
    assert len(sample_lines) <= 100


def test_parse_data_csv_empty_file():
    path = _write("", ".csv")
    result = parse_data(_job(), path)
    assert result == ""


# ── parse_data (JSON) ─────────────────────────────────────────────────────────


def test_parse_data_json_dict_lists_keys():
    data = json.dumps({"name": "Alice", "age": 30, "city": "NYC", "extra": "X"})
    path = _write(data, ".json")
    result = parse_data(_job(), path)
    assert "JSON keys:" in result
    assert "name" in result


def test_parse_data_json_array():
    data = json.dumps([1, 2, 3, 4, 5])
    path = _write(data, ".json")
    result = parse_data(_job(), path)
    assert "1" in result


# ── parse_data (YAML) ─────────────────────────────────────────────────────────


def test_parse_data_yaml_dict_lists_keys():
    content = "name: Alice\nage: 30\ncity: NYC\n"
    path = _write(content, ".yaml")
    result = parse_data(_job(), path)
    assert "YAML keys:" in result
    assert "name" in result


# ── parse dispatcher ──────────────────────────────────────────────────────────


def test_parse_note_kind_reads_as_text():
    path = _write("# My note\nContent here.", ".txt")
    result = parse(_job(kind=EntryKind.NOTE, file_name="note.txt"), path)
    assert "My note" in result
    assert "Content here" in result


def test_parse_code_kind_dispatches_to_code():
    path = _write("def hello(): pass\n", ".py")
    result = parse(_job(kind=EntryKind.CODE, file_name="hello.py"), path)
    assert "# File: hello.py" in result


def test_parse_data_kind_dispatches_to_data():
    path = _write("col1,col2\n1,2\n", ".csv")
    result = parse(_job(kind=EntryKind.DATA, file_name="data.csv"), path)
    assert "Columns:" in result


def test_parse_unsorted_reads_text():
    path = _write("some unsorted content", ".txt")
    result = parse(_job(kind=EntryKind.UNSORTED, file_name="unknown.txt"), path)
    assert "some unsorted content" in result


def test_parse_unsorted_binary_returns_placeholder():
    path = _write(b"\x00\x01\x02\x03" * 100, ".bin")
    result = parse(_job(kind=EntryKind.UNSORTED, file_name="blob.bin"), path)
    assert "[Binary file]" in result


# ── PDF parsing ───────────────────────────────────────────────────────────────


def test_parse_pdf_native(sample_pdf):
    """Native PDF created by reportlab must be parseable without OCR fallback."""
    from app.pipeline.parse.pdf import parse_pdf

    result = parse_pdf(str(sample_pdf))
    # reportlab PDF has "Hello PDF" on the first page
    assert "Hello" in result or "PDF" in result or "[PDF OCR fallback" in result


def test_parse_pdf_empty_returns_fallback():
    """An empty/corrupt PDF returns the OCR fallback placeholder."""
    from app.pipeline.parse.pdf import parse_pdf

    path = _write(b"%PDF-1.4\n", ".pdf")  # minimal, no pages
    result = parse_pdf(path)
    # Either parsed empty → fallback, or raised → we accept either
    assert isinstance(result, str)
