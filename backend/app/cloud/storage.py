"""Private immutable file storage. Public APIs expose file IDs, never host paths."""
from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import sys
import tempfile
import zipfile

from app.cloud.config import CloudSettings


ALLOWED_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".pdf", ".docx", ".xlsx", ".pptx"}


class StorageError(ValueError):
    pass


def safe_filename(value: str) -> str:
    name = value.replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."} or len(name) > 180 or any(ord(c) < 32 for c in name):
        raise StorageError("文件名无效")
    if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise StorageError("支持 TXT、Markdown、CSV、JSON、PDF、DOCX、XLSX 和 PPTX 文件")
    return name


class ObjectStore:
    def __init__(self, settings: CloudSettings):
        self.settings = settings
        self.root = (settings.data_dir / "objects").resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        parts = PurePosixPath(key).parts
        if len(parts) != 3 or any(not p or p in {".", ".."} for p in parts):
            raise StorageError("非法文件标识")
        if any(not all(c.isalnum() or c in "-_." for c in p) for p in parts):
            raise StorageError("非法文件标识")
        result = self.root.joinpath(*parts)
        # Reject symlink components rather than following a pre-existing alias.
        cursor = self.root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise StorageError("文件存储路径不可用")
        if not result.resolve().is_relative_to(self.root):
            raise StorageError("文件存储路径不可用")
        return result

    def _s3(self):
        import boto3
        return boto3.client("s3", endpoint_url=self.settings.s3_endpoint_url or None,
                            region_name=self.settings.s3_region or None)

    async def put(self, user_id: str, workspace_id: str, data: bytes) -> tuple[str, str]:
        key = f"{user_id}/{workspace_id}/{secrets.token_hex(20)}"
        checksum = hashlib.sha256(data).hexdigest()
        if self.settings.storage_backend == "s3":
            await asyncio.to_thread(self._s3().put_object, Bucket=self.settings.s3_bucket, Key=key,
                                    Body=data, ContentType="application/octet-stream",
                                    Metadata={"sha256": checksum})
        else:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Key is server-generated and immutable. Never overwrite a prior asset.
            with path.open("xb") as handle:
                handle.write(data)
            path.chmod(0o600)
        return key, checksum

    async def get(self, key: str) -> bytes:
        if self.settings.storage_backend == "s3":
            def read():
                response = self._s3().get_object(Bucket=self.settings.s3_bucket, Key=key)
                try:
                    return response["Body"].read(self.settings.max_upload_bytes + 1)
                finally:
                    response["Body"].close()
            data = await asyncio.to_thread(read)
        else:
            path = self._path(key)
            if not path.is_file():
                raise StorageError("文件不存在")
            if path.stat().st_size > self.settings.max_upload_bytes:
                raise StorageError("文件超过读取限制")
            data = await asyncio.to_thread(path.read_bytes)
        if len(data) > self.settings.max_upload_bytes:
            raise StorageError("文件超过读取限制")
        return data

    async def delete(self, key: str) -> None:
        if self.settings.storage_backend == "s3":
            await asyncio.to_thread(self._s3().delete_object, Bucket=self.settings.s3_bucket, Key=key)
        else:
            await asyncio.to_thread(self._path(key).unlink, missing_ok=True)


def _check_office_archive(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 3000 or sum(e.file_size for e in entries) > 40_000_000:
            raise StorageError("文档解压后超过处理限制")
        for entry in entries:
            name = entry.filename.lower()
            if entry.flag_bits & 1 or "vbaproject" in name or "embeddings/" in name:
                raise StorageError("不支持加密、宏或嵌入对象文档")
            if entry.file_size > max(1, entry.compress_size) * 500:
                raise StorageError("文档压缩比例超过处理限制")


def extract_text(data: bytes, filename: str, limit: int = 60000) -> str:
    """Read-only supported document extraction; no macros, execution or writes."""
    suffix = Path(filename).suffix.lower()
    if suffix in {".docx", ".xlsx", ".pptx"}:
        _check_office_archive(data)
    stream = io.BytesIO(data)
    if suffix == ".pdf":
        from pypdf import PdfReader
        document = PdfReader(stream)
        if document.is_encrypted or len(document.pages) > 150:
            raise StorageError("不支持加密 PDF 或超过 150 页的 PDF")
        parts, size = [], 0
        for page in document.pages:
            text = page.extract_text() or ""
            parts.append(text[:max(0, limit-size)])
            size += len(parts[-1])
            if size >= limit:
                break
        return "\n".join(parts)[:limit]
    if suffix == ".docx":
        from docx import Document
        document = Document(stream)
        chunks = [p.text for p in document.paragraphs]
        chunks.extend("\t".join(cell.text for cell in row.cells)
                      for table in document.tables for row in table.rows)
        return "\n".join(chunks)[:limit]
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        workbook = load_workbook(stream, read_only=True, data_only=True, keep_links=False)
        chunks, size = [], 0
        try:
            for sheet in workbook.worksheets[:20]:
                chunks.append(f"工作表：{sheet.title}")
                for row in sheet.iter_rows(max_row=2000, max_col=60, values_only=True):
                    line = "\t".join("" if v is None else str(v) for v in row)
                    chunks.append(line[:max(0, limit-size)])
                    size += len(chunks[-1])
                    if size >= limit:
                        return "\n".join(chunks)[:limit]
        finally:
            workbook.close()
        return "\n".join(chunks)[:limit]
    if suffix == ".pptx":
        from pptx import Presentation
        document = Presentation(stream)
        if len(document.slides) > 150:
            raise StorageError("演示文稿超过 150 页")
        return "\n".join(shape.text for slide in document.slides for shape in slide.shapes
                         if shape.has_text_frame)[:limit]
    if suffix not in ALLOWED_SUFFIXES:
        raise StorageError("不支持的文件格式")
    try:
        return data.decode("utf-8-sig")[:limit]
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030")[:limit]
        except UnicodeDecodeError as exc:
            raise StorageError("文本文件编码无法识别") from exc


def media_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


async def extract_text_isolated(data: bytes, filename: str, limit: int = 60000) -> str:
    """Bound lifetime/resource use and keep cloud credentials out of parsers.

    This is process/resource isolation, not a claim of a proven hostile-code
    sandbox. Cloud mode never exposes arbitrary scripts or macros.
    """
    script = Path(__file__).with_name("document_reader.py")
    with tempfile.TemporaryDirectory(prefix="suxiaoyou-parse-") as directory:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(script), cwd=directory,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": directory,
                 "TMPDIR": directory, "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        payload = json.dumps({"filename": filename, "limit": limit}).encode() + b"\n" + data
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout=30)
            if process.returncode or len(stdout) > 500000:
                raise StorageError("文档解析超过资源限制")
            result = json.loads(stdout)
            if "error" in result:
                raise StorageError(result["error"])
            return result["text"]
        except (asyncio.CancelledError, TimeoutError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
