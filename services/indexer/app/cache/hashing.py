import hashlib
from pathlib import Path


def file_hash(local_path: str) -> str:
    digest = hashlib.sha256()
    with Path(local_path).open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def chunk_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
