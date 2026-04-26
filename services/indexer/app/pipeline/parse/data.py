import csv
import json
from pathlib import Path

import yaml

from app.types import PipelineJob


def _rows_to_lines(headers: list[str], rows: list[list[str]]) -> str:
    lines: list[str] = []
    for row in rows:
        values = [row[i] if i < len(row) else "" for i in range(len(headers))]
        lines.append(", ".join(f"{h}={v}" for h, v in zip(headers, values)))
    return "\n".join(lines)


def parse_data(_: PipelineJob, local_path: str) -> str:
    path = Path(local_path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = list(csv.reader(f))
        if not reader:
            return ""
        headers = reader[0]
        sample = reader[1:101]
        return f"Columns: {', '.join(headers)}\n\nSample rows:\n{_rows_to_lines(headers, sample)}"
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            keys = list(data.keys())
            sample = {k: data[k] for k in keys[:3]}
            return f"JSON keys: {', '.join(keys)}\nSample: {json.dumps(sample, ensure_ascii=False)}"
        return json.dumps(data[:3] if isinstance(data, list) else data, ensure_ascii=False)
    if suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            keys = list(data.keys())
            sample = {k: data[k] for k in keys[:3]}
            return f"YAML keys: {', '.join(keys)}\nSample: {json.dumps(sample, ensure_ascii=False)}"
        return str(data)
    if suffix == ".toml":
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")
