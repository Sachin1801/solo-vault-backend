from pathlib import Path


def parse_text_file(local_path: str) -> str:
    path = Path(local_path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")
