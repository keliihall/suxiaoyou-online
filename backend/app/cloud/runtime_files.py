"""Browser file inputs for one isolated original-engine workspace.

The cloud gateway authenticates the user before selecting this engine; the
engine still requires its own private bearer. A declared path is constrained
to the tenant workspace, and filesystem operations use no-follow directory
descriptors so an Agent-created symlink cannot redirect an upload.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
from pathlib import Path, PurePosixPath
import secrets
import stat

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.auth.local import require_local_session
from app.utils.id import generate_ulid


router = APIRouter(dependencies=[Depends(require_local_session)])
MAX_UPLOAD_BYTES = int(os.environ.get("CLOUD_ENGINE_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
_RESERVED = frozenset({".suxiaoyou", ".tmp"})


class DirectoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str | None = None


class CreateDirectoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent: str | None = None
    name: str = Field(min_length=1, max_length=180)


def _root(request: Request) -> Path:
    root = getattr(request.app.state, "cloud_workspace_root", None)
    if not isinstance(root, Path) or not root.is_absolute():
        raise HTTPException(503, "Cloud workspace is unavailable")
    if root.is_symlink() or not root.is_dir():
        raise HTTPException(409, "Cloud workspace identity changed")
    return root


def _components(value: str) -> tuple[str, ...]:
    if (not value or "\\" in value or any(ord(char) < 32 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise HTTPException(422, "Invalid workspace relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in _RESERVED for part in path.parts):
        raise HTTPException(422, "Invalid workspace relative path")
    if len(path.parts) > 32 or any(len(part.encode("utf-8")) > 240 for part in path.parts):
        raise HTTPException(422, "Workspace path is too long")
    return path.parts


def _relative(root: Path, value: str | None) -> tuple[str, ...]:
    if not value or value == str(root):
        return ()
    if value.startswith("/"):
        try:
            value = Path(value).relative_to(root).as_posix()
        except ValueError as exc:
            raise HTTPException(403, "Path is outside the cloud workspace") from exc
    return _components(value)


def _open_directory(root: Path, parts: tuple[str, ...], *, create: bool = False) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {2, 20, 40}:
            raise HTTPException(409, "Workspace directory is unavailable or redirected") from exc
        raise


def _list_directory(root: Path, parts: tuple[str, ...]) -> dict:
    descriptor = _open_directory(root, parts)
    current = root.joinpath(*parts)
    try:
        dirs = []
        for name in os.listdir(descriptor):
            if name.startswith("."):
                continue
            try:
                item = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(item.st_mode):
                dirs.append({"name": name, "path": str(current / name)})
        dirs.sort(key=lambda item: item["name"].casefold())
        parent = str(current.parent) if parts else None
        return {"path": str(current), "current": str(current), "parent": parent,
                "dirs": dirs, "directories": dirs}
    finally:
        os.close(descriptor)


@router.get("/context")
async def workspace_context(request: Request):
    from app.runtime.v11_readiness import v11_runtime_readiness

    root = _root(request)
    return {"workspace_root": str(root), "default_workspace": str(root),
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "office_cloud": getattr(request.app.state, "cloud_office_status", {"ready": False}),
            "capabilities": v11_runtime_readiness(request.app.state)}


@router.post("/directories")
async def list_directories(body: DirectoryBody, request: Request):
    root = _root(request)
    return await asyncio.to_thread(_list_directory, root, _relative(root, body.path))


@router.post("/directories/create", status_code=201)
async def create_directory(body: CreateDirectoryBody, request: Request):
    root = _root(request)
    parent_parts, child_parts = _relative(root, body.parent), _components(body.name)
    if len(child_parts) != 1 or body.name.startswith("."):
        raise HTTPException(422, "Enter one visible directory name")
    descriptor = _open_directory(root, parent_parts)
    try:
        try:
            os.mkdir(child_parts[0], mode=0o700, dir_fd=descriptor)
        except FileExistsError as exc:
            raise HTTPException(409, "A directory or file already has this name") from exc
    finally:
        os.close(descriptor)
    return {"path": str(root.joinpath(*parent_parts, *child_parts))}


@router.post("/upload", status_code=201)
async def upload_file(
    request: Request, file: UploadFile,
    relative_path: str | None = Form(default=None),
    directory: str | None = Form(default=None),
):
    root = _root(request)
    directory_parts = _relative(root, directory)
    # webkitRelativePath supplies the hierarchy for browser directory upload.
    filename_parts = _components(relative_path or (file.filename or "upload"))
    parts = (*directory_parts, *filename_parts)
    if len(parts) > 32:
        raise HTTPException(422, "Workspace path is too long")
    descriptor = _open_directory(root, parts[:-1], create=True)
    temporary = f".cloud-upload-{secrets.token_hex(16)}"
    target_name = parts[-1]
    output_fd = None
    consumed, digest = 0, hashlib.sha256()
    try:
        output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=descriptor)
        while chunk := await file.read(1024 * 1024):
            consumed += len(chunk)
            if consumed > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "File exceeds the cloud upload limit")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                view = view[written:]
        os.fsync(output_fd)
        os.close(output_fd)
        output_fd = None
        try:
            # A hard-link publish atomically refuses an existing name. It
            # cannot overwrite a file that another Agent just changed.
            os.link(temporary, target_name, src_dir_fd=descriptor,
                    dst_dir_fd=descriptor, follow_symlinks=False)
        except FileExistsError as exc:
            raise HTTPException(409, "A file already has this name in the workspace") from exc
        os.unlink(temporary, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        if output_fd is not None:
            os.close(output_fd)
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)
        await file.close()
    path = root.joinpath(*parts)
    return {"file_id": generate_ulid(), "name": path.name, "path": str(path),
            "size": consumed, "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "source": "uploaded", "content_hash": digest.hexdigest()}
