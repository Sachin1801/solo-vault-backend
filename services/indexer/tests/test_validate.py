"""Unit tests for app.pipeline.validate — S3 head_object is mocked."""

from unittest.mock import patch

import pytest

from app.pipeline.validate import ALLOWED_MIMES, validate
from app.types import EntryKind, PipelineError, PipelineJob

_S3_META = {"ContentLength": 1024, "ContentType": "application/pdf"}


def _job(**overrides) -> PipelineJob:
    base = dict(
        job_id="j1",
        entry_id="e1",
        user_id="u1",
        project_id=None,
        s3_key="test/file.pdf",
        bucket="vault-test",
        file_name="file.pdf",
        mime="application/pdf",
        kind=EntryKind.DOCUMENT,
        subkind="pdf",
        size_bytes=1024,
    )
    base.update(overrides)
    return PipelineJob(**base)


# ── MIME validation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("mime", sorted(ALLOWED_MIMES))
def test_allowed_mimes_pass(mime):
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        validate(_job(mime=mime))  # should not raise


def test_invalid_mime_raises_invalid_type():
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        with pytest.raises(PipelineError) as exc_info:
            validate(_job(mime="application/x-msdownload"))
    assert exc_info.value.code == "INVALID_TYPE"


def test_invalid_mime_with_code_kind_and_code_extension_passes():
    """A .py file sent as CODE kind must pass even if MIME is not in ALLOWED_MIMES."""
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        validate(_job(mime="text/x-python", kind=EntryKind.CODE, file_name="script.py"))


def test_invalid_mime_with_snippet_kind_and_code_extension_passes():
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        validate(_job(mime="application/octet-stream", kind=EntryKind.SNIPPET, file_name="main.go"))


def test_invalid_mime_with_code_kind_but_wrong_extension_raises():
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        with pytest.raises(PipelineError) as exc_info:
            validate(_job(mime="video/mp4", kind=EntryKind.CODE, file_name="video.mp4"))
    assert exc_info.value.code == "INVALID_TYPE"


def test_unsorted_kind_skips_mime_check():
    """UNSORTED entries bypass the MIME allowlist entirely."""
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        validate(_job(mime="application/x-msdownload", kind=EntryKind.UNSORTED))


# ── Size validation ───────────────────────────────────────────────────────────


def test_file_too_large_raises():
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        with pytest.raises(PipelineError) as exc_info:
            validate(_job(size_bytes=51 * 1024 * 1024))
    assert exc_info.value.code == "FILE_TOO_LARGE"


def test_exactly_50mb_passes():
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        validate(_job(size_bytes=50 * 1024 * 1024))  # boundary: exactly 50 MB


def test_zero_size_passes():
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        validate(_job(size_bytes=0))


# ── S3 existence check ────────────────────────────────────────────────────────


def test_s3_object_missing_raises_s3_not_found():
    with patch("app.pipeline.validate.head_object", return_value=None):
        with pytest.raises(PipelineError) as exc_info:
            validate(_job())
    assert exc_info.value.code == "S3_NOT_FOUND"


def test_s3_object_present_passes():
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        validate(_job())  # no exception


# ── Error message content ─────────────────────────────────────────────────────


def test_invalid_type_error_includes_mime():
    bad_mime = "application/x-garbage"
    with patch("app.pipeline.validate.head_object", return_value=_S3_META):
        with pytest.raises(PipelineError) as exc_info:
            validate(_job(mime=bad_mime))
    assert bad_mime in str(exc_info.value)


def test_s3_not_found_error_includes_key():
    with patch("app.pipeline.validate.head_object", return_value=None):
        with pytest.raises(PipelineError) as exc_info:
            validate(_job(s3_key="missing/key.pdf"))
    assert "missing/key.pdf" in str(exc_info.value)
