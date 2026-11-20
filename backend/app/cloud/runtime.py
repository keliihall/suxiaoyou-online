"""Supervise the complete client engine in one isolated container per user.

Only the control plane can access Docker or decrypt engine credentials. Tenant
volumes are named Docker volumes, never host bind mounts. Original application
singletons consequently remain process-local to their owning cloud account.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text, update

from app.cloud.database import get_db
from app.cloud.identity import require_user
from app.cloud.models import TenantRuntime, User, utcnow
from app.cloud.secrets import ModelSecretVault

router = APIRouter()
_locks: dict[str, asyncio.Lock] = {}
_LABEL = "io.suxiaoyou.cloud.owner"
_GENERATION = "io.suxiaoyou.cloud.generation"
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}


class RuntimeUnavailable(RuntimeError):
    """Controlled message: Docker errors may contain internal environment data."""


class RuntimeSupervisor:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self._vault = None

    @property
    def vault(self):
        # Provision secrets only after an authorized runtime operation.
        if self._vault is None:
            self._vault = ModelSecretVault(self.settings)
        return self._vault

    async def _docker(self, *args: str, env: dict | None = None,
                      missing_ok: bool = False, timeout: float = 30) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "--host", self.settings.runtime_docker_host, *args,
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError:
            raise RuntimeUnavailable("云端运行服务尚未安装或无法连接") from None
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError):
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            if missing_ok and process.returncode == 1:
                return None
            raise RuntimeUnavailable("云端运行服务操作失败，请检查服务配置")
        return stdout.decode("utf-8")

    @asynccontextmanager
    async def _lock(self, user_id: str):
        # A session lock spans committed metadata and Docker operations. A row
        # transaction cannot do this: inference needs to see the capability
        # while the original engine is starting and fetching its model list.
        key = int.from_bytes(hashlib.sha256(user_id.encode()).digest()[:8], "big", signed=True)
        if self.database.engine.dialect.name == "postgresql":
            async with self.database.engine.connect() as connection:
                await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
                try:
                    yield
                finally:
                    # A transaction-scoped lock also releases on cancellation
                    # and pool rollback; no session lock can leak into a pool.
                    await connection.rollback()
        else:
            # SQLite is a single-control-process development configuration.
            async with _locks.setdefault(user_id, asyncio.Lock()):
                yield

    async def record(self, user_id: str) -> TenantRuntime | None:
        async with self.database.sessions() as db:
            return await db.get(TenantRuntime, user_id)

    async def _inspect(self, record: TenantRuntime) -> dict | None:
        output = await self._docker("container", "inspect", record.container_name, missing_ok=True)
        if output is None:
            return None
        try:
            info = json.loads(output)[0]
            labels = info["Config"]["Labels"] or {}
            owner = hashlib.sha256(record.user_id.encode()).hexdigest()
            if labels.get(_LABEL) != owner:
                raise RuntimeUnavailable("运行实例归属校验失败")
            return info
        except (KeyError, ValueError, IndexError, TypeError):
            raise RuntimeUnavailable("运行实例状态无效") from None

    @staticmethod
    def _port(info: dict) -> int | None:
        bindings = info.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp") or []
        for binding in bindings:
            if binding.get("HostIp") == "127.0.0.1":
                port = int(binding["HostPort"])
                if 0 < port < 65536:
                    return port
        return None

    def _base(self, record, port: int | None) -> str | None:
        if self.settings.runtime_network_mode:
            return f"http://{record.container_name}:8080"
        return f"http://127.0.0.1:{port}" if port else None

    async def _healthy(self, base: str | None) -> bool:
        if not base:
            return False
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                response = await client.get(base + "/livez")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def status(self, user_id: str) -> dict:
        if not self.settings.full_runtime_enabled:
            return {"status": "unavailable", "ready": False, "workspace": "/tenant/workspaces"}
        record = await self.record(user_id)
        status, message = "stopped", ""
        if record:
            status, message = record.state, record.error
            if status == "running":
                try:
                    info = await self._inspect(record)
                    status = "ready" if (info and info["State"]["Running"]
                        and info["Config"]["Labels"].get(_GENERATION) == str(record.generation)
                        and await self._healthy(self._base(record, self._port(info)))) else "failed"
                except RuntimeUnavailable as exc:
                    status, message = "unavailable", str(exc)
        result = {"status": status, "ready": status == "ready", "workspace": "/tenant/workspaces"}
        if message:
            result["error"] = message
        return result

    async def start(self, user_id: str, *, restart: bool = False) -> dict:
        if not self.settings.full_runtime_enabled:
            raise RuntimeUnavailable("完整云端运行环境尚未启用")
        async with self._lock(user_id):
            if not restart and (await self.status(user_id))["ready"]:
                return await self.status(user_id)
            record = await self.record(user_id)
            owner = hashlib.sha256(user_id.encode()).hexdigest()
            name, volume = f"sxy-user-{owner[:32]}", f"sxy-data-{owner[:32]}"
            if record:
                info = await self._inspect(record)
                if info:
                    await self._docker("stop", "--time", "30", record.container_name, timeout=45)
                    await self._docker("rm", "--force", record.container_name)
            # Never adopt an existing unlabelled volume with potentially foreign
            # data. There is no user-controlled container name or bind path.
            volume_info = await self._docker("volume", "inspect", volume, missing_ok=True)
            if volume_info:
                if (json.loads(volume_info)[0].get("Labels") or {}).get(_LABEL) != owner:
                    raise RuntimeUnavailable("运行存储归属校验失败")
            else:
                await self._docker("volume", "create", "--label", f"{_LABEL}={owner}", volume)
            capability, engine_token = secrets.token_urlsafe(48), "suxiaoyou_st_" + secrets.token_urlsafe(48)
            async with self.database.sessions() as db:
                user = await db.get(User, user_id)
                if not user or not user.is_active:
                    raise RuntimeUnavailable("账号已停用")
                record = await db.get(TenantRuntime, user_id)
                generation = record.generation + 1 if record else 1
                if not record:
                    record = TenantRuntime(user_id=user_id)
                    db.add(record)
                record.generation = generation
                record.capability_hash = hashlib.sha256(capability.encode()).hexdigest()
                record.capability_encrypted = self.vault.seal(capability)
                record.engine_token_encrypted = self.vault.seal(engine_token)
                record.container_name = name
                record.container_id, record.host_port = None, None
                record.state, record.error, record.updated_at = "starting", "", utcnow()
                await db.commit()
            try:
                network_args = ["--publish", "127.0.0.1::8080"]
                if self.settings.runtime_network_mode:
                    network = f"sxy-net-{owner[:32]}"
                    network_info = await self._docker("network", "inspect", network, missing_ok=True)
                    if network_info:
                        network_info = json.loads(network_info)[0]
                        if (network_info.get("Labels") or {}).get(_LABEL) != owner:
                            raise RuntimeUnavailable("运行网络归属校验失败")
                    else:
                        await self._docker("network", "create", "--label", f"{_LABEL}={owner}", network)
                        network_info = {}
                    control = self.settings.runtime_control_container or os.uname().nodename
                    if not any(control in {cid, entry.get("Name")} or cid.startswith(control)
                        for cid, entry in network_info.get("Containers", {}).items()):
                        await self._docker("network", "connect", "--alias", "suxiaoyou-control", network, control)
                    network_args = ["--network", network]
                child_env = {**os.environ, "CLOUD_ENGINE_AUTH_TOKEN": engine_token,
                    "CLOUD_ENGINE_CAPABILITY": capability,
                    "CLOUD_ENGINE_GATEWAY_URL": self.settings.runtime_gateway_url,
                    "CLOUD_ENGINE_ALLOWED_ORIGIN": self.settings.allowed_origin,
                    "SUXIAOYOU_OAUTH_CALLBACK_ORIGIN": self.settings.allowed_origin,
                    "CLOUD_ENGINE_MAX_UPLOAD_BYTES": str(min(self.settings.max_upload_bytes,
                        max(1, self.settings.runtime_max_body_bytes - 1024 * 1024)))}
                office = getattr(self, "office_gateway", None)
                if office:
                    child_env["CLOUD_ENGINE_OFFICE_URL"] = self.settings.runtime_office_url
                    child_env["CLOUD_ENGINE_OFFICE_PUBLIC_KEY"] = office.public_key_b64
                seccomp = str(Path(__file__).with_name("assets") / "seccomp-runtime.json")
                await self._docker("run", "--detach", "--init", "--name", name,
                    "--label", f"{_LABEL}={owner}", "--label", f"{_GENERATION}={generation}",
                    "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                    "--security-opt", "seccomp=" + seccomp,
                    "--security-opt", "systempaths=unconfined",
                    "--memory", self.settings.runtime_memory, "--cpus", str(self.settings.runtime_cpus),
                    "--pids-limit", str(self.settings.runtime_pids), "--user", "10001:10001",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                    *network_args, "--mount", f"type=volume,source={volume},target=/tenant",
                    "--env", "CLOUD_ENGINE_AUTH_TOKEN", "--env", "CLOUD_ENGINE_CAPABILITY",
                    "--env", "CLOUD_ENGINE_GATEWAY_URL", "--env", "CLOUD_ENGINE_ALLOWED_ORIGIN",
                    "--env", "SUXIAOYOU_OAUTH_CALLBACK_ORIGIN",
                    "--env", "CLOUD_ENGINE_MAX_UPLOAD_BYTES",
                    *(["--env", "CLOUD_ENGINE_OFFICE_URL", "--env", "CLOUD_ENGINE_OFFICE_PUBLIC_KEY"] if office else []),
                    self.settings.runtime_image, env=child_env, timeout=60)
                deadline, port = time.monotonic() + self.settings.runtime_start_timeout, None
                while time.monotonic() < deadline:
                    info = await self._inspect(record)
                    if not info or not info["State"]["Running"]:
                        raise RuntimeUnavailable("用户运行实例启动失败")
                    port = self._port(info)
                    if await self._healthy(self._base(record, port)):
                        break
                    await asyncio.sleep(.5)
                else:
                    raise RuntimeUnavailable("用户运行实例启动超时")
                async with self.database.sessions() as db:
                    await db.execute(update(TenantRuntime).where(TenantRuntime.user_id == user_id,
                        TenantRuntime.generation == generation).values(
                        state="running", host_port=port, container_id=info["Id"], updated_at=utcnow()))
                    await db.commit()
            except (RuntimeUnavailable, TimeoutError) as exc:
                message = str(exc) if isinstance(exc, RuntimeUnavailable) else "用户运行实例启动超时"
                async with self.database.sessions() as db:
                    await db.execute(update(TenantRuntime).where(TenantRuntime.user_id == user_id,
                        TenantRuntime.generation == generation).values(state="failed", error=message, updated_at=utcnow()))
                    await db.commit()
                with suppress(RuntimeUnavailable, TimeoutError):
                    # Retain only a stopped failed instance for operator logs.
                    # A later start verifies ownership and replaces it.
                    await self._docker("stop", "--time", "5", name)
                raise RuntimeUnavailable(message) from None
        return await self.status(user_id)

    async def stop(self, user_id: str) -> dict:
        async with self._lock(user_id):
            record = await self.record(user_id)
            if record:
                # Revoke inference before attempting Docker shutdown.
                async with self.database.sessions() as db:
                    await db.execute(update(TenantRuntime).where(TenantRuntime.user_id == user_id).values(
                        state="stopped", host_port=None, updated_at=utcnow()))
                    await db.commit()
                if await self._inspect(record):
                    await self._docker("stop", "--time", "30", record.container_name, timeout=45)
                    await self._docker("rm", "--force", record.container_name)
        return await self.status(user_id)

    async def endpoint(self, user_id: str) -> tuple[str, str]:
        record = await self.record(user_id)
        if not self.settings.full_runtime_enabled or not record or record.state != "running":
            raise RuntimeUnavailable("请先启动云端运行环境")
        info = await self._inspect(record)
        if (not info or not info["State"]["Running"]
                or info["Config"]["Labels"].get(_GENERATION) != str(record.generation)):
            raise RuntimeUnavailable("用户运行实例未就绪")
        port = self._port(info)
        if not self.settings.runtime_network_mode and (not port or port != record.host_port):
            raise RuntimeUnavailable("用户运行实例端口校验失败")
        return self._base(record, port), self.vault.unseal(record.engine_token_encrypted)


def supervisor(request: Request) -> RuntimeSupervisor:
    return request.app.state.runtime_supervisor


@router.get("/runtime/status")
async def runtime_status(request: Request, user: User = Depends(require_user)):
    return await supervisor(request).status(user.id)


@router.post("/runtime/start")
async def runtime_start(request: Request, user: User = Depends(require_user)):
    try:
        return await supervisor(request).start(user.id)
    except RuntimeUnavailable as exc:
        raise HTTPException(503, detail={"code": "runtime_unavailable", "message": str(exc)}) from None


@router.post("/runtime/stop")
async def runtime_stop(request: Request, user: User = Depends(require_user)):
    try:
        return await supervisor(request).stop(user.id)
    except RuntimeUnavailable as exc:
        raise HTTPException(503, detail={"code": "runtime_unavailable", "message": str(exc)}) from None


@router.post("/runtime/restart")
async def runtime_restart(request: Request, user: User = Depends(require_user)):
    try:
        return await supervisor(request).start(user.id, restart=True)
    except RuntimeUnavailable as exc:
        raise HTTPException(503, detail={"code": "runtime_unavailable", "message": str(exc)}) from None


async def proxy_engine(request: Request, path: str):
    if path.startswith("/api/cloud/") or path == "/api/cloud":
        raise HTTPException(404)
    async with request.app.state.database.sessions() as db:
        user = await require_user(request, db)
        user_id = user.id
    try:
        base, token = await supervisor(request).endpoint(user_id)
    except RuntimeUnavailable as exc:
        raise HTTPException(503, detail={"code": "runtime_unavailable", "message": str(exc)}) from None
    # Never forward a cloud cookie, client bearer, or query session token. The
    # browser has access only to its own container through this authenticated URL.
    query = [(k, v) for k, v in request.query_params.multi_items() if k not in {"token", "access_token", "session_token"}]
    headers = {k: v for k, v in request.headers.items()
        if k.lower() not in _HOP | {"authorization", "cookie", "origin", "x-csrf-token", "accept-encoding"}}
    headers["Authorization"] = f"Bearer {token}"
    client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(connect=10, read=None, write=120, pool=10))
    # Stream both file uploads and SSE responses. Actual body bytes are bounded
    # by the outer ASGI middleware before they reach this private connection.
    async def body():
        async for chunk in request.stream():
            yield chunk
    try:
        outgoing = client.build_request(request.method, base + path, params=query, headers=headers, content=body())
        upstream = await client.send(outgoing, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(503, detail={"code": "runtime_unavailable", "message": "用户运行实例连接失败"}) from None
    response_headers = {k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP | {"set-cookie", "content-encoding"}}

    async def content():
        try:
            last_check = 0.0
            async for chunk in upstream.aiter_bytes():
                if time.monotonic() - last_check > 3:
                    async with request.app.state.database.sessions() as db:
                        await require_user(request, db)
                    last_check = time.monotonic()
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
    return StreamingResponse(content(), status_code=upstream.status_code, headers=response_headers)
