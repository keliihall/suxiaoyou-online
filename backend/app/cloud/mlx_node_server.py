"""Exclusive Mac inference node. Run under a dedicated OS account for production.

Only the process created here and cache entries in the operator-selected private
root are manageable. No PID scan, arbitrary shell, installer or target URL is
accepted from the network. TLS is mandatory in the provided CLI.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import hmac
import ipaddress
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import stat
import sys

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.rapid_mlx.catalog import RAPID_MLX_ALIAS_REPOS, bare_rapid_mlx_model

MAX_BODY = 24 * 1024 * 1024
# Operator-only low-resource smoke model. Runtime callers still cannot supply
# arbitrary repositories; the full original catalog remains available to the
# operator's explicit --model allowlist.
NODE_MODELS = {**RAPID_MLX_ALIAS_REPOS,
    "qwen2.5-0.5b": "mlx-community/Qwen2.5-0.5B-Instruct-4bit"}
_ALIAS = re.compile(r"[a-z][a-z0-9._-]{0,79}\Z")
_REPO_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
MAX_APPROVED_MODELS = 128


def validate_model_mapping(alias, repo):
    """Operator data are model identifiers, never paths, revisions or options."""
    if (not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or ".." in alias
            or not isinstance(repo, str) or len(repo.split("/")) != 2
            or any(not _REPO_PART.fullmatch(part) or ".." in part or part.endswith(".git")
                   for part in repo.split("/"))):
        raise ValueError("Use a safe model alias and a HuggingFace owner/repository ID")


def custom_model_mappings(values):
    if not isinstance(values, dict) or len(values) > 64:
        raise ValueError("At most 64 operator model mappings are supported")
    result = {}
    for alias, repo in values.items():
        validate_model_mapping(alias, repo)
        if alias in NODE_MODELS or alias == "default":
            raise ValueError("An operator mapping cannot replace a bundled model alias")
        result[alias] = repo
    return result


def parse_model_repos(entries):
    values = {}
    for entry in entries or []:
        if not isinstance(entry, str) or entry.count("=") != 1:
            raise ValueError("Use --model-repo alias=owner/repository")
        alias, repo = entry.split("=", 1)
        if alias in values:
            raise ValueError("Duplicate operator model alias")
        values[alias] = repo
    return custom_model_mappings(values)


def approved_model_catalog(payload):
    """Validate a catalog supplied by the authenticated, pinned operator node."""
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_APPROVED_MODELS:
        raise ValueError("Invalid node model catalog")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"alias", "repo"}:
            raise ValueError("Invalid approved model entry")
        alias, repo = row["alias"], row["repo"]
        validate_model_mapping(alias, repo)
        if alias in result:
            raise ValueError("Duplicate approved model alias")
        result[alias] = repo
    return result


def _private_dir(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Node directory must be a private absolute non-symlink path")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Node directory must be service-owned with mode 0700")
    return path.resolve()


@dataclass(frozen=True)
class NodeSettings:
    root: Path
    token: str
    inference_port: int = 18081
    models: tuple[str, ...] = ("qwen3.5-4b",)
    binary: str | None = None
    cache_memory_mb: int = 256
    custom_models: dict[str, str] = field(default_factory=dict)


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="qwen3.5-4b", min_length=1, max_length=200)
    port: int = Field(default=18080, ge=1024, le=65535)


class CachedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aliases: list[str] = Field(default_factory=list, max_length=100)


class RemoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1, max_length=200)


class ExclusiveNode:
    def __init__(self, settings: NodeSettings):
        if (len(settings.token) < 32 or not 1024 <= settings.inference_port <= 65535
                or not 64 <= settings.cache_memory_mb <= 16384):
            raise ValueError("A strong node token and unprivileged inference port are required")
        custom = custom_model_mappings(settings.custom_models)
        available = {**NODE_MODELS, **custom}
        if not settings.models or set(settings.models) - set(available):
            raise ValueError("Node model allowlist must contain known Rapid-MLX aliases")
        aliases = tuple(dict.fromkeys((*settings.models, *custom)))
        if len(aliases) > MAX_APPROVED_MODELS:
            raise ValueError("Too many approved node models")
        self.model_repos = {alias: available[alias] for alias in aliases}
        self.settings = settings
        self.root = _private_dir(settings.root)
        self.home = _private_dir(self.root / "home")
        self.hf = _private_dir(self.root / "huggingface")
        self.cache = _private_dir(self.hf / "hub")
        self.tmp = _private_dir(self.root / "tmp")
        self.process = None
        self.current_model = settings.models[0]
        self.lock = asyncio.Lock()
        self._root_lock = None

    def acquire(self):
        descriptor = os.open(self.root / ".node.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise RuntimeError("This private node root is already in use") from None
        self._root_lock = descriptor

    def release(self):
        if self._root_lock is not None:
            os.close(self._root_lock)
            self._root_lock = None

    @property
    def supported(self):
        return sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}

    @property
    def binary(self):
        configured = self.settings.binary
        found = configured or shutil.which("rapid-mlx")
        if not found:
            return None
        path = Path(found)
        return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None

    @property
    def alive(self):
        return self.process is not None and self.process.returncode is None

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.settings.inference_port}/v1"

    def alias(self, value):
        bare = bare_rapid_mlx_model(value)
        alias = next((key for key, repo in self.model_repos.items()
                      if bare == key or bare == repo), None)
        if alias is None:
            raise HTTPException(400, "This model is not approved for this node")
        return alias

    def environment(self):
        # Do not inherit cloud/vendor/SSH credentials or the user's HF cache.
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home), "TMPDIR": str(self.tmp), "LANG": "en_US.UTF-8",
            "XDG_CACHE_HOME": str(self.home / ".cache"), "HF_HOME": str(self.hf),
            "HF_HUB_CACHE": str(self.cache), "HUGGINGFACE_HUB_CACHE": str(self.cache),
            "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
            "RAPID_MLX_TELEMETRY": "0", "DO_NOT_TRACK": "1"}

    async def healthy(self):
        if not self.alive:
            return False
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                result = await client.get(self.base_url + "/models")
                return result.status_code == 200
        except httpx.HTTPError:
            return False

    async def status(self):
        running = await self.healthy()
        return {"configured": True, "platform_supported": self.supported,
            "binary_installed": self.binary is not None, "running": running,
            "process_running": self.alive, "port": self.settings.inference_port,
            "base_url": "/v1" if running else None, "version": None,
            "current_model": self.current_model, "executable_path": None,
            "install_commands": [], "allowed_models": list(self.model_repos),
            "unavailable_reason": "" if running else (
                "unsupported_platform" if not self.supported else "binary_not_installed" if not self.binary
                else "loading" if self.alive else "stopped")}

    async def start(self, body: StartBody):
        alias = self.alias(body.model)
        # Original UI sends its conventional port. It cannot choose a host port;
        # the only alternative accepted is the operator-fixed node port.
        if body.port not in {18080, self.settings.inference_port}:
            raise HTTPException(400, "The node inference port is fixed by the operator")
        async with self.lock:
            if not self.supported or not self.binary:
                raise HTTPException(503, "Apple Silicon macOS and an installed rapid-mlx binary are required")
            if self.alive and self.current_model == alias:
                return await self.status()
            await self._stop()
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", self.settings.inference_port))
            except OSError:
                raise HTTPException(409, "The fixed inference port is occupied by an unmanaged process") from None
            finally:
                probe.close()
            # Repo is fixed by the allowlist, not shell-interpolated input.
            log_fd = os.open(self.root / "rapid-mlx.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                self.process = await asyncio.create_subprocess_exec(self.binary, "serve", self.model_repos[alias],
                    "--host", "127.0.0.1", "--port", str(self.settings.inference_port),
                    "--cache-memory-mb", str(self.settings.cache_memory_mb),
                    "--max-concurrent-requests", "4", "--max-tokens", "8192",
                    cwd=str(self.root), env=self.environment(), stdout=log_fd, stderr=log_fd)
            finally:
                os.close(log_fd)
            self.current_model = alias
            # Model download/load may take minutes. Status reports process alive
            # separately; it only reports running after an actual models probe.
            await asyncio.sleep(0)
            return await self.status()

    async def _stop(self):
        process = self.process
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=8)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        self.process = None

    async def stop(self):
        async with self.lock:
            await self._stop()
            return await self.status()

    def cache_path(self, alias):
        repo = self.model_repos[self.alias(alias)]
        path = self.cache / ("models--" + repo.replace("/", "--"))
        if path.is_symlink():
            raise HTTPException(409, "Node cache contains an unsafe symbolic link")
        return path

    def cached(self, aliases):
        return {alias: self.cache_path(alias).is_dir() and (self.cache_path(alias) / "snapshots").is_dir()
                for alias in aliases}

    def _remove_cache(self, alias):
        path = self.cache_path(alias)
        if not path.exists():
            return 0
        # HF snapshots contain symlinks to blobs within this same repo cache.
        # shutil.rmtree does not follow them. Reject the top-level directory
        # symlink above and count only non-symlink regular owned cache files.
        size = sum(item.lstat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())
        shutil.rmtree(path)
        return size

    async def remove(self, alias):
        selected = self.alias(alias)
        async with self.lock:
            if self.alive and self.model_repos[selected] == self.model_repos[self.current_model]:
                raise HTTPException(409, "Stop this model before removing its node cache")
            await asyncio.to_thread(self._remove_cache, selected)
            return {"cached": self.cached([alias])}

    async def uninstall(self, delete_models):
        async with self.lock:
            await self._stop()
            removed, freed = [], 0
            if delete_models:
                for alias in self.model_repos:
                    if self.cache_path(alias).exists():
                        freed += await asyncio.to_thread(self._remove_cache, alias)
                        removed.append(alias)
            # Never uninstall an operator-managed Python or Homebrew binary.
            return {"stopped": True, "removed_models": removed, "freed_bytes": freed,
                    "errors": [], "binary_install_commands": []}


async def bounded_json(request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BODY:
            raise HTTPException(413, "Request exceeds the node payload budget")
    import json
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "An object is required")
    return value


def create_node_app(settings: NodeSettings):
    node = ExclusiveNode(settings)
    @asynccontextmanager
    async def lifespan(app):
        node.acquire()
        try:
            yield
        finally:
            await node.stop()
            node.release()
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.node = node

    async def authorized(request: Request):
        supplied = request.headers.get("authorization", "")
        expected = "Bearer " + settings.token
        if len(supplied) > 1024 or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(401, "Invalid node credential")

    @app.get("/health")
    async def health():
        return {"service": "suxiaoyou-mlx-node"}

    @app.get("/rapid-mlx/status", dependencies=[Depends(authorized)])
    async def status():
        return await node.status()

    @app.get("/rapid-mlx/catalog", dependencies=[Depends(authorized)])
    async def catalog():
        return {"models": [{"alias": alias, "repo": repo} for alias, repo in node.model_repos.items()]}

    @app.post("/rapid-mlx/start", dependencies=[Depends(authorized)])
    async def start(body: StartBody):
        return await node.start(body)

    @app.post("/rapid-mlx/stop", dependencies=[Depends(authorized)])
    async def stop():
        return await node.stop()

    @app.post("/rapid-mlx/cached", dependencies=[Depends(authorized)])
    async def cached(body: CachedBody):
        return {"cached": node.cached(body.aliases)}

    @app.post("/rapid-mlx/remove", dependencies=[Depends(authorized)])
    async def remove(body: RemoveBody):
        return await node.remove(body.alias)

    @app.delete("/rapid-mlx/uninstall", dependencies=[Depends(authorized)])
    async def uninstall(delete_models: bool = True):
        return await node.uninstall(delete_models)

    async def inference(request, path):
        if not await node.healthy():
            raise HTTPException(503, "The node model is not ready")
        body = None
        if request.method == "POST":
            from app.cloud.inference import ChatCompletionRequest
            raw = await bounded_json(request)
            try:
                validated = ChatCompletionRequest.model_validate(raw)
            except ValueError:
                raise HTTPException(400, "Invalid chat completion payload") from None
            if node.model_repos[node.alias(validated.model)] != node.model_repos[node.current_model]:
                raise HTTPException(409, "The requested model is not running on this node")
            body = validated.model_dump(exclude_none=True)
            body["model"] = node.model_repos[node.current_model]
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(600, connect=5))
        try:
            response = await client.send(client.build_request(request.method, node.base_url + path, json=body), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502, "The node model connection failed") from None
        if response.status_code != 200:
            await response.aclose()
            await client.aclose()
            raise HTTPException(502, "The node model rejected the request")
        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()
        return StreamingResponse(chunks(), media_type="text/event-stream" if body and body.get("stream") else "application/json")

    @app.get("/v1/models", dependencies=[Depends(authorized)])
    async def models(request: Request):
        if not node.alive:
            raise HTTPException(503, "The node model is not ready")
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                response = await client.get(node.base_url + "/models")
                response.raise_for_status()
                data = response.json()
            current_repo = node.model_repos[node.current_model]
            allowed = {alias for alias, repo in node.model_repos.items() if repo == current_repo} | {current_repo}
            rows = [{"id": row["id"] if len(row["id"]) <= 120 else node.current_model,
                     "object": "model", "created": 0, "owned_by": "exclusive-mac-node"}
                    for row in data.get("data", [])[:MAX_APPROVED_MODELS]
                    if isinstance(row, dict) and row.get("id") in allowed]
            return {"object": "list", "data": rows}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise HTTPException(503, "The node model is not ready") from None

    @app.post("/v1/chat/completions", dependencies=[Depends(authorized)])
    async def completions(request: Request):
        return await inference(request, "/chat/completions")
    return app


def initialize_node(root: Path, hostname: str):
    """Create private node credentials without printing their values."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    root = _private_dir(root)
    paths = [root / name for name in ("token", "tls.key", "tls.crt")]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError("Node credential files already exist; explicit operator rotation is required")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    try:
        san = x509.IPAddress(ipaddress.ip_address(hostname))
    except ValueError:
        san = x509.DNSName(hostname)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365)).add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).sign(key, hashes.SHA256()))
    values = [secrets.token_urlsafe(48).encode(),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM)]
    for path, value in zip(paths, values):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "serve"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--hostname", default="localhost")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18443)
    parser.add_argument("--inference-port", type=int, default=18081)
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--model-repo", action="append", default=[], metavar="ALIAS=OWNER/REPO")
    parser.add_argument("--binary")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "init":
        initialize_node(args.root, args.hostname)
        print("Created private node credential files")
        return
    root = _private_dir(args.root)
    descriptor = os.open(root / "token", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as handle:
        metadata = os.fstat(handle.fileno())
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("Node token must be a service-owned 0600 file")
        token = handle.read(1024).strip()
    import uvicorn
    uvicorn.run(create_node_app(NodeSettings(root=root, token=token, inference_port=args.inference_port,
        models=tuple(args.models or ["qwen3.5-4b"]), binary=args.binary,
        custom_models=parse_model_repos(args.model_repo))), host=args.host, port=args.port,
        ssl_keyfile=str(root / "tls.key"), ssl_certfile=str(root / "tls.crt"), proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
