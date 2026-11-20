"""Disposable read-only document parser. No application credentials inherited."""
import contextlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if __name__ == "__main__":
    with contextlib.suppress(ImportError, ValueError, OSError):
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    try:
        header = json.loads(sys.stdin.buffer.readline(2000))
        data = sys.stdin.buffer.read(50 * 1024 * 1024 + 1)
        if len(data) > 50 * 1024 * 1024:
            raise ValueError("file limit")
        from app.cloud.storage import extract_text
        result = {"text": extract_text(data, header["filename"], min(header["limit"], 60000))}
    except Exception:
        result = {"error": "文档无法解析，可能格式损坏、加密或包含不支持的内容"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
