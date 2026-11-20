"""Platform-signed rendering from pinned, credential-free Docker jobs.

The original desktop signature remains untouched. This is a separate cloud
trust root: the operator approves an immutable image, its golden-vector and
isolation probes must pass, and the platform signs each exact render result.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, Depends, HTTPException, Request

from app.cloud.database import get_db
from app.cloud.runtime_identity import get_runtime_principal
from app.office_rendering.models import LIBREOFFICE_PARAMETERS_VERSION, RenderManifest, RenderRequest, RendererDescriptor, canonical_json_bytes

router = APIRouter()
MAX_SOURCE_BYTES = 75 * 1024 * 1024
MAX_BODY_BYTES = 105 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 180 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")


class OfficeGatewayError(RuntimeError):
    pass


def validate_artifact_bytes(manifest: RenderManifest, files: dict) -> None:
    """Validate only bounded framing/hashes in the privileged controller.

    Full PDF/PNG parsing stays inside the isolated renderer and tenant engine;
    no user-controlled XML/PDF/image parser runs in the control plane here.
    """
    artifacts = (manifest.pdf, *manifest.pages)
    if not isinstance(files, dict) or set(files) != {item.filename for item in artifacts}:
        raise OfficeGatewayError("Office output inventory mismatch")
    total = 0
    for artifact in artifacts:
        encoded = files[artifact.filename]
        if not isinstance(encoded, str) or len(encoded) > MAX_JSON_BYTES:
            raise OfficeGatewayError("Office output exceeds its budget")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise OfficeGatewayError("Office output encoding is invalid") from exc
        total += len(content)
        if (total > MAX_OUTPUT_BYTES or len(content) != artifact.size_bytes
                or hashlib.sha256(content).hexdigest() != artifact.sha256):
            raise OfficeGatewayError("Office output integrity mismatch")
        if artifact is manifest.pdf:
            if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
                raise OfficeGatewayError("Office PDF framing is invalid")
        elif (len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n"
                or content[12:16] != b"IHDR"
                or struct.unpack(">II", content[16:24]) != (artifact.width_px, artifact.height_px)):
            raise OfficeGatewayError("Office page framing is invalid")


class OfficeGateway:
    def __init__(self, settings):
        self.settings = settings
        self._key = self._load_key(Path(os.path.abspath(settings.data_dir)))
        public = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key_b64 = base64.b64encode(public).decode("ascii")
        self.key_id = hashlib.sha256(public).hexdigest()
        self._descriptor = None
        self._initialization = asyncio.Lock()
        self._slots = asyncio.Semaphore(2)

    @staticmethod
    def _load_key(data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data_fd = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

        def private(metadata, *, directory):
            if (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != (0o700 if directory else 0o600)
                    or not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode))):
                raise OfficeGatewayError("Office signing key storage must be private and service-owned")

        try:
            metadata = os.fstat(data_fd)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise OfficeGatewayError("Office data root must be service-owned and not externally writable")
            try:
                os.mkdir("secrets", mode=0o700, dir_fd=data_fd)
            except FileExistsError:
                pass
            directory_fd = os.open("secrets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=data_fd)
        finally:
            os.close(data_fd)

        def read_key():
            descriptor = os.open("office-signing.key", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                private(os.fstat(descriptor), directory=False)
                raw = os.read(descriptor, 33)
                if len(raw) != 32:
                    raise OfficeGatewayError("Office signing key contents are invalid")
                return Ed25519PrivateKey.from_private_bytes(raw)
            finally:
                os.close(descriptor)

        try:
            private(os.fstat(directory_fd), directory=True)
            try:
                return read_key()
            except FileNotFoundError:
                pass
            temporary = ".office-signing." + secrets.token_hex(16) + ".tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(Ed25519PrivateKey.generate().private_bytes(
                        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, "office-signing.key", src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd, follow_symlinks=False)
                    os.fsync(directory_fd)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary, dir_fd=directory_fd)
            return read_key()
        finally:
            os.close(directory_fd)

    def sign(self, payload: dict) -> dict:
        return {"payload": payload, "key_id": self.key_id,
                "signature": base64.b64encode(self._key.sign(canonical_json_bytes(payload))).decode("ascii")}

    async def _command(self, *args: str, payload: bytes = b"", output_limit: int = MAX_JSON_BYTES, timeout: float = 130):
        process = await asyncio.create_subprocess_exec(
            "docker", "--host", self.settings.runtime_docker_host, *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        async def read(stream, limit):
            total, chunks = 0, []
            while block := await stream.read(65536):
                total += len(block)
                if total > limit:
                    raise OfficeGatewayError("Office process output exceeds its budget")
                chunks.append(block)
            return b"".join(chunks)

        async def write():
            if payload:
                process.stdin.write(payload)
                await process.stdin.drain()
            process.stdin.close()

        tasks = [asyncio.create_task(write()), asyncio.create_task(read(process.stdout, output_limit)),
                 asyncio.create_task(read(process.stderr, 16384)), asyncio.create_task(process.wait())]
        try:
            result = await asyncio.wait_for(asyncio.gather(*tasks), timeout)
            if process.returncode:
                raise OfficeGatewayError("Office process failed safely")
            return result[1]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()

    async def _job(self, image_id: str, *, image_ref: str | None = None, probe: bool = False, body: dict | None = None):
        if not _IMAGE.fullmatch(image_id):
            raise OfficeGatewayError("Office image identity is invalid")
        reference = image_ref or image_id
        if reference != image_id and (reference.startswith("-") or not reference.endswith("@" + image_id)):
            raise OfficeGatewayError("Office repository digest is invalid")
        name = "sxy-office-" + secrets.token_hex(16)
        try:
            output = await self._command(
                "run", "--rm", "--init", "--interactive", "--name", name,
                "--network", "none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true", "--memory", "1g", "--cpus", "1",
                "--pids-limit", "96", "--user", "10001:10001", "--workdir", "/work",
                "--tmpfs", "/work:rw,nosuid,nodev,size=512m,mode=1777",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--env", "HOME=/work/home", "--env", "TMPDIR=/work/tmp",
                "--entrypoint", "python", reference, "-m", "app.cloud.office_job",
                *(("--probe",) if probe else ()),
                payload=canonical_json_bytes(body) if body is not None else b"",
            )
            return json.loads(output)
        finally:
            # Killing the CLI alone is insufficient: explicitly kill/remove
            # the job container and all of its cgroup descendants on every exit.
            with suppress(Exception):
                await asyncio.shield(self._command("rm", "--force", name, output_limit=4096, timeout=10))

    async def descriptor(self) -> dict:
        async with self._initialization:
            if self._descriptor is None:
                if not self.settings.full_runtime_enabled:
                    raise OfficeGatewayError("The full cloud runtime is disabled")
                metadata = json.loads(await self._command("image", "inspect", "--format", "{{json .}}", self.settings.runtime_image,
                                                         output_limit=65536, timeout=15))
                image = metadata["Id"]
                if not _IMAGE.fullmatch(image):
                    raise OfficeGatewayError("Office image is unavailable")
                reference = await self._retain_image(image)
                proof = await self._job(image, image_ref=reference, probe=True)
                build, sandbox = proof.get("descriptor", {}), proof.get("sandbox", {})
                expected = {"schema_version", "pipeline_id", "font_digest", "soffice_sha256",
                            "pdftoppm_sha256", "golden_source_sha256", "golden_pixels"}
                if (set(build) != expected or build["schema_version"] != 1
                        or build["pipeline_id"] != "suxiaoyou-linux-cloud-office-v1"
                        or any(not isinstance(build[key], str) or not _SHA.fullmatch(build[key])
                               for key in ("font_digest", "soffice_sha256", "pdftoppm_sha256", "golden_source_sha256"))
                        or not isinstance(build["golden_pixels"], list) or not 1 <= len(build["golden_pixels"]) <= 10
                        or any(not isinstance(value, str) or not _SHA.fullmatch(value) for value in build["golden_pixels"])
                        or proof.get("probe_passed") is not True
                        or sandbox != {"uid": 10001, "root_read_only": True, "network_denied": True}):
                    raise OfficeGatewayError("The Office image failed its golden/isolation contract")
                self._descriptor = {"schema_version": 1, "purpose": "descriptor", "image_id": image, "image_ref": reference,
                    "renderer": {"renderer_id": "suxiaoyou-cloud-office",
                        "renderer_version": "image-" + image.removeprefix("sha256:"),
                        "font_digest": build["font_digest"], "quality": "authoritative"},
                    "build": build, "sandbox": sandbox,
                    "parameters_version": LIBREOFFICE_PARAMETERS_VERSION, "parameters": {"dpi": 144}}
            return self.sign(self._descriptor)

    async def _retain_image(self, image: str) -> str:
        # A mutable runtime tag can move while this API still serves a pinned
        # descriptor. Keep its image reachable until an operator retires that
        # renderer version; a containerd index may otherwise disappear on tag
        # replacement even while older tenant containers continue running.
        retained = "suxiaoyou-office-renderer:sha-" + image.removeprefix("sha256:")
        await self._command("image", "tag", image, retained, output_limit=4096, timeout=15)
        pinned = json.loads(await self._command("image", "inspect", "--format", "{{json .}}", retained,
                                               output_limit=65536, timeout=15))
        if pinned.get("Id") != image:
            raise OfficeGatewayError("The retained Office image identity changed")
        # OCI-index IDs need a repository digest to run on containerd stores.
        # Classic Docker config IDs remain runnable directly and are retained
        # by the same verified local tag.
        return next((item for item in pinned.get("RepoDigests", [])
                     if isinstance(item, str) and item.endswith("@" + image)), image)

    async def render(self, body: dict) -> dict:
        if (not isinstance(body, dict) or set(body) != {"source", "document_format", "parameters_version", "parameters"}
                or body["document_format"] not in {"docx", "xlsx", "pptx"}
                or body["parameters_version"] != LIBREOFFICE_PARAMETERS_VERSION
                or body["parameters"] != {"dpi": 144} or not isinstance(body["source"], str)):
            raise OfficeGatewayError("The Office request contract is invalid")
        source = base64.b64decode(body["source"], validate=True)
        if not source or len(source) > MAX_SOURCE_BYTES:
            raise OfficeGatewayError("The Office source exceeds its budget")
        signed_descriptor = await self.descriptor()
        descriptor = signed_descriptor["payload"]
        async with self._slots:
            result = await self._job(descriptor["image_id"], image_ref=descriptor["image_ref"], body=body)
        expected_identity = {key: descriptor["build"][key] for key in (
            "schema_version", "pipeline_id", "font_digest", "soffice_sha256", "pdftoppm_sha256")}
        if not isinstance(result, dict) or result.get("identity") != expected_identity:
            raise OfficeGatewayError("The rendered image identity changed")
        manifest = RenderManifest.from_dict(result["manifest"])
        request = RenderRequest(workspace_root=Path("/"), source_path=Path("/source." + body["document_format"]),
            document_format=body["document_format"], source_sha256=hashlib.sha256(source).hexdigest(),
            parameters_version=body["parameters_version"], parameters=body["parameters"])
        if (manifest.source_sha256 != request.source_sha256 or manifest.document_format != request.document_format
                or manifest.parameters_version != request.parameters_version or manifest.parameters_sha256 != request.parameters_sha256
                or manifest.font_digest != descriptor["build"]["font_digest"] or manifest.quality != "approximate"):
            raise OfficeGatewayError("The rendered source/parameter contract changed")
        validate_artifact_bytes(manifest, result["files"])
        trusted_manifest = RenderManifest.for_request(request, RendererDescriptor(**descriptor["renderer"]),
                                                      manifest.pages, pdf=manifest.pdf)
        response = self.sign({"schema_version": 1, "purpose": "render", "image_id": descriptor["image_id"],
                              "manifest": trusted_manifest.to_dict()})
        response["files"] = result["files"]
        return response


@router.get("/internal/office/descriptor")
async def office_descriptor(request: Request, db=Depends(get_db)):
    await get_runtime_principal(request, db)
    try:
        return await request.app.state.office_gateway.descriptor()
    except (OfficeGatewayError, OSError, ValueError, TimeoutError):
        raise HTTPException(503, detail={"code": "office_renderer_unavailable", "message": "云端文档渲染服务未就绪"}) from None


@router.post("/internal/office/render")
async def office_render(request: Request, db=Depends(get_db)):
    await get_runtime_principal(request, db)
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise HTTPException(413, detail={"code": "office_source_too_large", "message": "文档超过渲染大小限制"})
        chunks.append(chunk)
    try:
        return await request.app.state.office_gateway.render(json.loads(b"".join(chunks)))
    except (OfficeGatewayError, OSError, ValueError, KeyError, TypeError, TimeoutError):
        raise HTTPException(503, detail={"code": "office_render_failed", "message": "文档渲染未通过完整性或运行校验"}) from None
