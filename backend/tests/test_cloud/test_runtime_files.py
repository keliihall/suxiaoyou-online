"""The browser bridge cannot turn a user path into a tenant escape."""

import hashlib
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from app.cloud import runtime_files
from app.cloud.runtime_entry import _AuthenticatedLoopback, _bootstrap


@pytest.fixture
def runtime_app(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    app = FastAPI()
    app.state.cloud_workspace_root = root

    @app.middleware("http")
    async def test_auth(request: Request, call_next):
        if request.headers.get("authorization") == "Bearer test":
            request.state.source = "local"
        return await call_next(request)

    app.include_router(runtime_files.router, prefix="/files")
    return app, root


@pytest.fixture
async def client(runtime_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime_app[0]), base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as client:
        yield client


async def test_directory_tree_creation_and_root_boundary(client, runtime_app):
    root = runtime_app[1]
    listing = (await client.post("/files/directories", json={})).json()
    assert listing["path"] == str(root)
    assert listing["parent"] is None
    made = await client.post("/files/directories/create", json={"name": "项目材料"})
    assert made.status_code == 201
    listing = (await client.post("/files/directories", json={})).json()
    assert listing["dirs"] == [{"name": "项目材料", "path": str(root / "项目材料")}]
    assert (await client.post("/files/directories", json={"path": str(root.parent)})).status_code == 403
    assert (await client.post("/files/directories", json={"path": "../state"})).status_code == 422
    assert (await client.post("/files/directories/create", json={"name": "a/b"})).status_code == 422


async def test_upload_keeps_hierarchy_and_original_attachment_contract(client, runtime_app):
    root, data = runtime_app[1], "真实文件内容".encode()
    response = await client.post("/files/upload", files={"file": ("说明.txt", data)},
                                 data={"relative_path": "资料/子目录/说明.txt"})
    assert response.status_code == 201
    metadata = response.json()
    target = root / "资料/子目录/说明.txt"
    assert target.read_bytes() == data
    assert metadata["path"] == str(target)
    assert metadata["name"] == "说明.txt"
    assert metadata["size"] == len(data)
    assert metadata["source"] == "uploaded"
    assert metadata["content_hash"] == hashlib.sha256(data).hexdigest()
    assert metadata["file_id"]
    duplicate = await client.post("/files/upload", files={"file": ("说明.txt", b"overwrite")},
                                  data={"relative_path": "资料/子目录/说明.txt"})
    assert duplicate.status_code == 409
    assert target.read_bytes() == data


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "a/../escape.txt", "a\\b.txt", ".suxiaoyou/state.json"])
async def test_upload_rejects_paths(client, path):
    response = await client.post("/files/upload", files={"file": ("safe.txt", b"x")}, data={"relative_path": path})
    assert response.status_code == 422


async def test_symlink_cannot_redirect_directory_or_upload(client, runtime_app):
    root = runtime_app[1]
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert (await client.post("/files/directories", json={"path": str(root / "linked")})).status_code == 409
    response = await client.post("/files/upload", files={"file": ("no.txt", b"x")}, data={"relative_path": "linked/no.txt"})
    assert response.status_code == 409
    assert list(outside.iterdir()) == []


async def test_oversize_upload_leaves_no_partial_file(client, runtime_app, monkeypatch):
    monkeypatch.setattr(runtime_files, "MAX_UPLOAD_BYTES", 3)
    response = await client.post("/files/upload", files={"file": ("too-big.txt", b"1234")})
    assert response.status_code == 413
    assert list(runtime_app[1].iterdir()) == []


async def test_bridge_still_requires_original_local_auth(runtime_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime_app[0]), base_url="http://test") as client:
        assert (await client.post("/files/directories", json={})).status_code == 403


async def test_only_supervisor_token_reclassifies_peer():
    seen = []

    async def receiver(scope, receive, send):
        seen.append(scope["client"])

    middleware = _AuthenticatedLoopback(receiver, token="private-token")
    scope = {"type": "http", "client": ("172.20.0.1", 123), "headers": [(b"x-forwarded-for", b"127.0.0.1")]}
    await middleware(scope, None, None)
    assert seen[-1] == ("172.20.0.1", 123)
    scope["headers"] = [(b"authorization", b"Bearer private-token")]
    await middleware(scope, None, None)
    assert seen[-1] == ("127.0.0.1", 0)


def test_bootstrap_uses_tenant_private_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLOUD_ENGINE_TENANT_ROOT", str(tmp_path / "tenant"))
    monkeypatch.setenv("CLOUD_ENGINE_AUTH_TOKEN", "suxiaoyou_st_" + "a" * 43)
    monkeypatch.setenv("CLOUD_ENGINE_CAPABILITY", "b" * 48)
    monkeypatch.setenv("CLOUD_ENGINE_GATEWAY_URL", "http://gateway:8000/internal/v1")
    for name in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "MPLCONFIGDIR", "TMPDIR",
                 "SUXIAOYOU_PRIVATE_DATA_DIR", "SUXIAOYOU_MANAGED_WORKSPACE_ROOT", "CLOUD_ENGINE_WORKSPACE_ROOT"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    previous_umask = os.umask(0o077)
    try:
        state, workspace, *_ = _bootstrap()
        assert Path.cwd() == state
        assert workspace == tmp_path / "tenant/workspaces"
        assert os.environ["SUXIAOYOU_PRIVATE_DATA_DIR"] == str(state)
        assert os.environ["SUXIAOYOU_MANAGED_WORKSPACE_ROOT"] == str(state / "managed-workspaces")
    finally:
        os.umask(previous_umask)
