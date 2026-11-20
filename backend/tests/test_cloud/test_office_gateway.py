"""Cloud Office trust contracts; actual LibreOffice is exercised in Docker."""

import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
from pypdf import PdfWriter
import pytest

from app.cloud.config import CloudSettings
from app.cloud.office_gateway import OfficeGateway, OfficeGatewayError
from app.cloud.office_provider import CloudOfficeRenderProvider, verify_envelope
from app.office_rendering.errors import RenderContractError, StaleSourceError
from app.office_rendering.models import LIBREOFFICE_PARAMETERS_VERSION, PageArtifact, PdfArtifact, RenderManifest, RenderRequest, RendererDescriptor


@pytest.fixture
def gateway(tmp_path):
    return OfficeGateway(CloudSettings(data_dir=tmp_path / "platform", environment="test"))


def binding():
    return {"schema_version": 1, "purpose": "descriptor", "image_id": "sha256:" + "a" * 64,
        "renderer": {"renderer_id": "suxiaoyou-cloud-office", "renderer_version": "image-" + "a" * 64,
                     "font_digest": "b" * 64, "quality": "authoritative"},
        "sandbox": {"uid": 10001, "root_read_only": True, "network_denied": True},
        "parameters_version": LIBREOFFICE_PARAMETERS_VERSION, "parameters": {"dpi": 144}}


def output(gateway, request, descriptor):
    pixels = bytes((0, 0, 0, 255))
    image = Image.frombytes("RGBA", (1, 1), pixels)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    png = stream.getvalue()
    pdf_stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf_stream)
    pdf = pdf_stream.getvalue()
    manifest = RenderManifest.for_request(request, descriptor,
        [PageArtifact(1, "page-1.png", hashlib.sha256(png).hexdigest(), hashlib.sha256(pixels).hexdigest(), len(png), 1, 1)],
        pdf=PdfArtifact("document.pdf", hashlib.sha256(pdf).hexdigest(), len(pdf), 1))
    result = gateway.sign({"schema_version": 1, "purpose": "render", "image_id": binding()["image_id"], "manifest": manifest.to_dict()})
    result["files"] = {"document.pdf": base64.b64encode(pdf).decode(), "page-1.png": base64.b64encode(png).decode()}
    return result


def test_signing_key_is_private_and_atomic_under_concurrent_start(tmp_path):
    settings = CloudSettings(data_dir=tmp_path / "platform", environment="test")
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: OfficeGateway(settings).public_key_b64, range(20)))
    assert len(set(keys)) == 1
    secret = settings.data_dir / "secrets"
    assert (secret.stat().st_mode & 0o777) == 0o700
    assert (secret / "office-signing.key").stat().st_mode & 0o777 == 0o600
    assert len((secret / "office-signing.key").read_bytes()) == 32
    assert sorted(path.name for path in secret.iterdir()) == ["office-signing.key"]


def test_unsafe_signing_storage_is_rejected(tmp_path):
    data = tmp_path / "platform"
    data.mkdir()
    secrets = data / "secrets"
    secrets.mkdir(mode=0o755)
    with pytest.raises(OfficeGatewayError):
        OfficeGateway(CloudSettings(data_dir=data, environment="test"))
    secrets.rmdir()
    target = tmp_path / "outside"
    target.mkdir()
    secrets.symlink_to(target)
    with pytest.raises(OSError):
        OfficeGateway(CloudSettings(data_dir=data, environment="test"))


def test_signature_covers_purpose_payload_and_trust_root(gateway, tmp_path):
    signed = gateway.sign(binding())
    assert verify_envelope(signed, gateway.public_key_b64, purpose="descriptor") == binding()
    with pytest.raises(RenderContractError):
        verify_envelope(signed, gateway.public_key_b64, purpose="render")
    tampered = copy.deepcopy(signed)
    tampered["payload"]["renderer"]["font_digest"] = "c" * 64
    with pytest.raises(RenderContractError):
        verify_envelope(tampered, gateway.public_key_b64, purpose="descriptor")
    other = OfficeGateway(CloudSettings(data_dir=tmp_path / "other", environment="test"))
    with pytest.raises(RenderContractError):
        verify_envelope(signed, other.public_key_b64, purpose="descriptor")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.docx"
    path.write_bytes(b"source bytes")
    request = RenderRequest(workspace_root=tmp_path, source_path=path, document_format="docx",
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), parameters_version=LIBREOFFICE_PARAMETERS_VERSION,
        parameters={"dpi": 144})
    target = tmp_path / "output"
    target.mkdir()
    return request, target


async def test_valid_signed_render_is_written_for_original_cache(gateway, source, monkeypatch):
    request, target = source
    provider = CloudOfficeRenderProvider(base_url="http://platform/office", capability="private", public_key=gateway.public_key_b64, descriptor=binding())
    response = output(gateway, request, provider.descriptor)

    async def reply(*args, **kwargs):
        return response

    monkeypatch.setattr(provider, "_request", reply)
    manifest = await provider.render(request, target)
    assert manifest.quality == "authoritative"
    assert {path.name for path in target.iterdir()} == {"document.pdf", "page-1.png"}
    assert hashlib.sha256((target / "page-1.png").read_bytes()).hexdigest() == manifest.pages[0].sha256


@pytest.mark.parametrize("mutation", ["signature", "bytes", "image", "source"])
async def test_untrusted_output_never_reaches_cache(gateway, source, monkeypatch, mutation):
    request, target = source
    provider = CloudOfficeRenderProvider(base_url="http://platform/office", capability="private", public_key=gateway.public_key_b64, descriptor=binding())
    response = output(gateway, request, provider.descriptor)
    if mutation == "signature":
        response["signature"] = base64.b64encode(b"x" * 64).decode()
    elif mutation == "bytes":
        response["files"]["page-1.png"] = base64.b64encode(b"tampered").decode()
    elif mutation == "image":
        changed = dict(response["payload"], image_id="sha256:" + "c" * 64)
        response = {**gateway.sign(changed), "files": response["files"]}

    async def reply(*args, **kwargs):
        if mutation == "source":
            request.source_path.write_bytes(b"concurrently changed")
        return response

    monkeypatch.setattr(provider, "_request", reply)
    with pytest.raises((RenderContractError, StaleSourceError)):
        await provider.render(request, target)
    assert list(target.iterdir()) == []


async def test_jobs_use_fixed_image_and_no_credentials_or_tenant_mount(gateway, monkeypatch):
    calls = []

    async def command(*args, **kwargs):
        calls.append(args)
        return b"{}"

    monkeypatch.setattr(gateway, "_command", command)
    image = "sha256:" + "d" * 64
    await gateway._job(image, probe=True)
    command = calls[0]
    assert image in command and gateway.settings.runtime_image not in command
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "--cap-drop=ALL" in command
    assert "--mount" not in command and "CLOUD_ENGINE_CAPABILITY" not in command
    assert calls[-1][:2] == ("rm", "--force")


async def test_descriptor_retains_verified_image_before_runtime_tag_moves(gateway, monkeypatch):
    image, calls = "sha256:" + "d" * 64, []
    reference = "suxiaoyou-office-renderer@" + image
    async def command(*args, **kwargs):
        calls.append(args)
        return json.dumps({"Id": image, "RepoDigests": [reference]}).encode()
    monkeypatch.setattr(gateway, "_command", command)
    assert await gateway._retain_image(image) == reference
    assert calls[0] == ("image", "tag", image, "suxiaoyou-office-renderer:sha-" + "d" * 64)
    assert gateway.settings.runtime_image not in calls[0]


async def test_retained_renderer_identity_mismatch_is_rejected(gateway, monkeypatch):
    async def command(*args, **kwargs):
        return json.dumps({"Id": "sha256:" + "b" * 64}).encode()
    monkeypatch.setattr(gateway, "_command", command)
    with pytest.raises(OfficeGatewayError, match="identity changed"):
        await gateway._retain_image("sha256:" + "d" * 64)


def test_cloud_imports_only_renderer_contracts():
    script = "import sys; import app.cloud.main; from app.office_rendering import RenderManifest; assert 'fontTools' not in sys.modules; assert 'app.office_rendering.probe' not in sys.modules; assert 'app.office_rendering.libreoffice' not in sys.modules"
    subprocess.run([sys.executable, "-c", script], check=True, cwd=Path(__file__).parents[2])
