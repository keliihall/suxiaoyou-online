"""Credential-free, one-shot Linux Office renderer executed by the platform.

The controller runs this entry point with no network, a read-only image, a
bounded /work tmpfs, a non-root user and a dedicated container cgroup. It never
receives a tenant volume, inference capability, provider key or signing key.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

from app.office_rendering.libreoffice import (
    LIBREOFFICE_PARAMETERS_VERSION, LibreOfficeRenderLimits,
    LibreOfficeRenderProvider, discover_libreoffice_toolchain,
)
from app.office_rendering.models import RenderRequest
from app.office_rendering.runtime import fingerprint_font_environment

ROOT = Path("/opt/backend/cloud-office")
MAX_SOURCE_BYTES = 75 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_INPUT_BYTES = 105 * 1024 * 1024
PIPELINE_ID = "suxiaoyou-linux-cloud-office-v1"
PARAMETERS = {"dpi": 144}


def _identity() -> tuple[dict, object]:
    toolchain = discover_libreoffice_toolchain(platform_name="linux")
    if not toolchain.available:
        raise ValueError("Office rendering tools are unavailable")
    fonts = fingerprint_font_environment(roots=(Path("/usr/share/fonts"),))
    return {"schema_version": 1, "pipeline_id": PIPELINE_ID,
            "font_digest": fonts, "soffice_sha256": toolchain.soffice.sha256,
            "pdftoppm_sha256": toolchain.pdftoppm.sha256}, toolchain


async def _render(source: bytes, document_format: str, directory: Path):
    identity, toolchain = _identity()
    expected = json.loads((ROOT / "descriptor.json").read_bytes()) if (ROOT / "descriptor.json").exists() else None
    if expected is not None and any(identity[key] != expected[key] for key in identity):
        raise ValueError("The immutable renderer identity changed")
    provider = LibreOfficeRenderProvider(
        font_digest=identity["font_digest"], toolchain=toolchain,
        platform_name="linux", limits=LibreOfficeRenderLimits(
            timeout_seconds=90, max_source_bytes=MAX_SOURCE_BYTES,
            max_pdf_bytes=64 * 1024 * 1024, max_output_bytes=MAX_OUTPUT_BYTES,
            max_pages=200, max_page_pixels=50_000_000,
        ),
    )
    source_path = directory / f"source.{document_format}"
    source_path.write_bytes(source)
    source_path.chmod(0o600)
    output = directory / "o"
    output.mkdir(mode=0o700)
    request = RenderRequest(
        workspace_root=directory, source_path=source_path,
        document_format=document_format, source_sha256=hashlib.sha256(source).hexdigest(),
        parameters_version=LIBREOFFICE_PARAMETERS_VERSION, parameters=PARAMETERS,
    )
    manifest = await provider.render(request, output)
    if _identity()[0] != identity:
        raise ValueError("The renderer changed during conversion")
    return identity, manifest, output


async def write_descriptor() -> None:
    """Build a fixed golden vector into the immutable image, never at runtime."""
    from docx import Document

    ROOT.mkdir(parents=True, exist_ok=True)
    source = ROOT / "golden.docx"
    document = Document()
    document.add_heading("苏小有 Office 云端验证", 0)
    document.add_paragraph("Cloud Office rendering golden vector. 中文字体与表格验证。")
    table = document.add_table(rows=2, cols=2)
    for cell, text in zip((cell for row in table.rows for cell in row.cells), ("项目", "状态", "真实渲染", "通过")):
        cell.text = text
    document.save(source)
    with tempfile.TemporaryDirectory(prefix="office-build-") as temporary:
        identity, manifest, _ = await _render(source.read_bytes(), "docx", Path(temporary))
    identity.update({"golden_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                     "golden_pixels": [page.pixel_sha256 for page in manifest.pages]})
    (ROOT / "descriptor.json").write_text(json.dumps(identity, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _sandbox_probe() -> dict:
    if os.getuid() != 10001:
        raise ValueError("The renderer must run as its isolated unprivileged identity")
    # /tenant is owned by uid 10001 in the image. This tests read-only mount
    # enforcement rather than merely a root-owned file's Unix permissions.
    probe = Path("/tenant/.office-write-probe")
    try:
        probe.write_bytes(b"must not write")
    except OSError:
        read_only = True
    else:
        probe.unlink()
        read_only = False
    connection = socket.socket()
    connection.settimeout(.2)
    try:
        connection.connect(("1.1.1.1", 443))
    except OSError:
        network_denied = True
    else:
        network_denied = False
    finally:
        connection.close()
    if not read_only or not network_denied:
        raise ValueError("The renderer container isolation probe failed")
    return {"uid": 10001, "root_read_only": True, "network_denied": True}


async def probe() -> dict:
    isolation = _sandbox_probe()
    expected = json.loads((ROOT / "descriptor.json").read_bytes())
    source = (ROOT / "golden.docx").read_bytes()
    if hashlib.sha256(source).hexdigest() != expected["golden_source_sha256"]:
        raise ValueError("The golden source changed")
    # One container handles one request. Short fixed paths also keep
    # LibreOffice's Unix-domain IPC name below the kernel sockaddr limit.
    identity, manifest, _ = await _render(source, "docx", Path("/work"))
    if ([page.pixel_sha256 for page in manifest.pages] != expected["golden_pixels"]
            or any(identity[key] != expected[key] for key in identity)):
        raise ValueError("The golden render changed")
    return {"descriptor": expected, "sandbox": isolation, "probe_passed": True}


async def render_job(payload: dict) -> dict:
    _sandbox_probe()
    if (set(payload) != {"source", "document_format", "parameters_version", "parameters"}
            or payload["document_format"] not in {"docx", "xlsx", "pptx"}
            or payload["parameters_version"] != LIBREOFFICE_PARAMETERS_VERSION
            or payload["parameters"] != PARAMETERS):
        raise ValueError("Unsupported Office render request")
    source = base64.b64decode(payload["source"], validate=True)
    if not source or len(source) > MAX_SOURCE_BYTES:
        raise ValueError("Office input size is outside the limit")
    identity, manifest, output = await _render(source, payload["document_format"], Path("/work"))
    files, total = {}, 0
    for artifact in (manifest.pdf, *manifest.pages):
        content = (output / artifact.filename).read_bytes()
        total += len(content)
        if total > MAX_OUTPUT_BYTES or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ValueError("Office output violates its integrity budget")
        files[artifact.filename] = base64.b64encode(content).decode("ascii")
    return {"identity": identity, "manifest": manifest.to_dict(), "files": files}


def main() -> None:
    os.umask(0o077)
    if "--write-descriptor" in sys.argv:
        asyncio.run(write_descriptor())
        return
    for folder in (Path("/work/home"), Path("/work/tmp")):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["HOME"], os.environ["TMPDIR"] = "/work/home", "/work/tmp"
    if "--probe" in sys.argv:
        response = asyncio.run(probe())
    else:
        content = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(content) > MAX_INPUT_BYTES:
            raise ValueError("Office input exceeds the budget")
        response = asyncio.run(render_job(json.loads(content)))
    sys.stdout.write(json.dumps(response, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No document text, host path, environment value or traceback is sent
        # to the controller. The controller returns a bounded generic error.
        sys.stderr.write("Office rendering failed safely\n")
        raise SystemExit(1)
