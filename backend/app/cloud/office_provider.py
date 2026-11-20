"""Verify platform render signatures before using original Office precommit.

Cloud authority is supplied by the supervisor's pinned public key and the
platform's immutable, probe-verified image. It is independent of desktop
frozen-release authority; neither the desktop loader nor its gates are changed.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import io
import logging
import os
from pathlib import Path
import stat

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import httpx
from PIL import Image

from app.office_rendering.errors import ProviderUnavailableError, RenderContractError, StaleSourceError
from app.office_rendering.models import RenderManifest, RenderRequest, RendererDescriptor, canonical_json_bytes
from app.office_rendering.provider import ProviderAvailability

MAX_SOURCE_BYTES = 75 * 1024 * 1024
MAX_RESPONSE_BYTES = 180 * 1024 * 1024
logger = logging.getLogger(__name__)


def verify_envelope(envelope: dict, public_key_b64: str, *, purpose: str) -> dict:
    try:
        public = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
        payload = envelope["payload"]
        if (len(public) != 32 or len(signature) != 64 or not isinstance(payload, dict)
                or payload.get("purpose") != purpose or payload.get("schema_version") != 1
                or envelope["key_id"] != hashlib.sha256(public).hexdigest()):
            raise ValueError("Invalid signature contract")
        Ed25519PublicKey.from_public_bytes(public).verify(signature, canonical_json_bytes(payload))
        return payload
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        raise RenderContractError("Cloud Office signature verification failed") from exc


def _read_source(request: RenderRequest) -> bytes:
    root = request.workspace_root.resolve(strict=True)
    source = request.source_path
    try:
        source.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise RenderContractError("Cloud Office source escaped its workspace") from exc
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_BYTES:
            raise RenderContractError("Cloud Office source exceeds its budget")
        chunks, total = [], 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise RenderContractError("Cloud Office source exceeds its budget")
            chunks.append(chunk)
        content = b"".join(chunks)
        after, visible = os.fstat(descriptor), source.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(visible):
            raise StaleSourceError("Cloud Office source changed while reading")
    finally:
        os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != request.source_sha256:
        raise StaleSourceError("Cloud Office source digest changed")
    return content


class CloudOfficeRenderProvider:
    def __init__(self, *, base_url: str, capability: str, public_key: str, descriptor: dict):
        self.base_url = base_url.rstrip("/")
        self._capability, self._public_key, self._binding = capability, public_key, descriptor
        self._descriptor = RendererDescriptor(**descriptor["renderer"])
        if (self._descriptor.quality != "authoritative"
                or descriptor["parameters"] != {"dpi": 144}
                or descriptor["sandbox"] != {"uid": 10001, "root_read_only": True, "network_denied": True}
                or not descriptor["image_id"].startswith("sha256:")):
            raise RenderContractError("Cloud Office deployment contract is invalid")

    @property
    def descriptor(self):
        return self._descriptor

    def availability(self):
        return ProviderAvailability(True)

    @classmethod
    async def connect(cls, *, base_url: str, capability: str, public_key: str):
        envelope = await cls._request(base_url.rstrip("/") + "/descriptor", capability, "GET", limit=1024 * 1024)
        descriptor = verify_envelope(envelope, public_key, purpose="descriptor")
        return cls(base_url=base_url, capability=capability, public_key=public_key, descriptor=descriptor)

    @staticmethod
    async def _request(url: str, capability: str, method: str, *, body: dict | None = None, limit: int = MAX_RESPONSE_BYTES):
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(140, connect=10)) as client:
            async with client.stream(method, url, headers={"Authorization": "Bearer " + capability}, json=body) as response:
                if response.status_code != 200:
                    raise ProviderUnavailableError("Cloud Office rendering is unavailable")
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise RenderContractError("Cloud Office response exceeds its budget")
                    chunks.append(chunk)
        import json
        try:
            return json.loads(b"".join(chunks))
        except (ValueError, TypeError) as exc:
            raise RenderContractError("Cloud Office response is not a valid envelope") from exc

    async def render(self, request: RenderRequest, output_dir: Path) -> RenderManifest:
        if (request.parameters_version != self._binding["parameters_version"]
                or request.parameters_dict() != self._binding["parameters"]):
            raise RenderContractError("Cloud Office parameters differ from the pinned deployment")
        source = await asyncio.to_thread(_read_source, request)
        envelope = await self._request(self.base_url + "/render", self._capability, "POST", body={
            "source": base64.b64encode(source).decode("ascii"), "document_format": request.document_format,
            "parameters_version": request.parameters_version, "parameters": request.parameters_dict()})
        payload = verify_envelope(envelope, self._public_key, purpose="render")
        if payload["image_id"] != self._binding["image_id"]:
            raise RenderContractError("Cloud Office image identity changed")
        manifest = RenderManifest.from_dict(payload["manifest"])
        expected = RenderManifest.for_request(request, self.descriptor, manifest.pages, pdf=manifest.pdf)
        if manifest.to_dict() != expected.to_dict():
            raise RenderContractError("Cloud Office output is bound to a different source or renderer")
        from app.cloud.office_gateway import OfficeGatewayError, validate_artifact_bytes
        try:
            validate_artifact_bytes(manifest, envelope["files"])
        except OfficeGatewayError as exc:
            raise RenderContractError("Cloud Office output integrity verification failed") from exc
        # Repeat the source check after the remote conversion and before any
        # cache output can be installed by the original cache boundary.
        await asyncio.to_thread(_read_source, request)
        if output_dir.is_symlink() or not output_dir.is_dir() or any(output_dir.iterdir()):
            raise RenderContractError("Cloud Office staging output is not empty and private")
        installed = []
        try:
            for artifact in (manifest.pdf, *manifest.pages):
                data = base64.b64decode(envelope["files"][artifact.filename], validate=True)
                if artifact is not manifest.pdf:
                    with Image.open(io.BytesIO(data)) as image:
                        if (image.size != (artifact.width_px, artifact.height_px)
                                or image.width * image.height > 50_000_000):
                            raise RenderContractError("Cloud Office page dimensions are invalid")
                        pixels = image.convert("RGBA").tobytes()
                    if hashlib.sha256(pixels).hexdigest() != artifact.pixel_sha256:
                        raise RenderContractError("Cloud Office page pixels do not match the signed manifest")
                target = output_dir / artifact.filename
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                installed.append(target)
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except BaseException:
            for target in installed:
                target.unlink(missing_ok=True)
            raise
        return manifest


def install_cloud_office_lifespan(app) -> None:
    """Add the cloud trust boundary after the original engine's full startup."""
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            application.state.cloud_office_status = {"ready": False}
            base_url = os.environ.get("CLOUD_ENGINE_OFFICE_URL", "").strip()
            public_key = os.environ.get("CLOUD_ENGINE_OFFICE_PUBLIC_KEY", "").strip()
            capability = os.environ.get("CLOUD_ENGINE_CAPABILITY", "").strip()
            if base_url and public_key and capability:
                try:
                    from app.office_rendering.provider import AdmissionControlledOfficeRenderProvider
                    from app.office_templates.policies import FirstPartyOfficePrecommitPolicyResolver
                    from app.office_validation.runtime import build_office_v11_runtime, install_office_v11_runtime
                    from app.release_readiness import v11_capability_released

                    if not v11_capability_released("office_authoring") or getattr(application.state, "validation_agent_service", None) is None:
                        raise ProviderUnavailableError("Office source gates or validation service are unavailable")
                    provider = await CloudOfficeRenderProvider.connect(base_url=base_url, capability=capability, public_key=public_key)
                    admitted = AdmissionControlledOfficeRenderProvider(provider, max_concurrent_renders=1, admission_timeout_seconds=5)
                    parameters, version = {"dpi": 144}, provider._binding["parameters_version"]
                    private_root = Path(os.environ["SUXIAOYOU_PRIVATE_DATA_DIR"]) / "cloud-office"
                    policies = FirstPartyOfficePrecommitPolicyResolver(
                        registry_root=private_root / "policy-templates", renderer=provider.descriptor,
                        parameters_version=version, parameters_sha256=hashlib.sha256(canonical_json_bytes(parameters)).hexdigest())
                    runtime = build_office_v11_runtime(application.state.session_factory,
                        data_dir=private_root, provider=admitted, policies=policies,
                        parameters_version=version, parameters=parameters)
                    install_office_v11_runtime(application.state, runtime)
                    application.state.cloud_office_status = {"ready": True, "image_id": provider._binding["image_id"]}
                except Exception as exc:
                    # Do not publish partial precommit authority. The original
                    # approximate preview can remain available after failure.
                    logger.warning("Cloud Office trust initialization failed (%s)", type(exc).__name__)
                    application.state.cloud_office_status = {"ready": False, "error": "office_cloud_not_ready"}
            yield

    app.router.lifespan_context = lifespan
