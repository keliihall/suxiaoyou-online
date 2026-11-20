"""Private, non-root Ollama installer and lifecycle for Linux tenant engines.

The desktop API and provider stay intact. Installation never writes /usr,
executes a shell installer, follows archive links outside the private root, or
passes cloud capabilities to the Ollama server process.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit

import anyio
import httpx

from app.ollama.manager import OllamaManager, _platform_key, _find_free_port, _is_port_free

MAX_ARCHIVE_BYTES = 2 * 1024 ** 3
MAX_EXPANDED_BYTES = 10 * 1024 ** 3
ALLOWED_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


def allowed_download_url(url):
    try:
        parsed = urlsplit(str(url))
        return (parsed.scheme == "https" and parsed.hostname in ALLOWED_DOWNLOAD_HOSTS
                and not parsed.username and not parsed.password and parsed.port in {None, 443})
    except ValueError:
        return False


def extract_private_archive(archive: Path, destination: Path):
    """Stream to a private unpublished stage; never materialize a second tar."""
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise RuntimeError("Ollama extraction requires a fresh empty private stage")
    process = None
    package = None
    try:
        if archive.name.endswith(".tar.zst"):
            zstd = shutil.which("zstd")
            if not zstd:
                raise RuntimeError("This runtime image requires the zstd extraction tool")
            process = subprocess.Popen([zstd, "-d", "-c", str(archive)], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
            package = tarfile.open(fileobj=process.stdout, mode="r|")
        else:
            package = tarfile.open(archive, "r|gz")
        count, total = 0, 0
        for item in package:
            count += 1
            total += item.size
            if count > 100000 or total > MAX_EXPANDED_BYTES:
                raise RuntimeError("Ollama release exceeds the extraction budget")
            name = PurePosixPath(item.name)
            if (name.is_absolute() or ".." in name.parts or "\\" in item.name
                    or not (item.isfile() or item.isdir() or item.issym() or item.islnk())):
                raise RuntimeError("Unsafe Ollama archive member")
            if item.issym() or item.islnk():
                target = PurePosixPath(item.linkname)
                if target.is_absolute() or ".." in target.parts or "\\" in item.linkname:
                    raise RuntimeError("Unsafe Ollama archive link")
            # Never fill the shared daemon disk. Reject this staged install
            # before writing a member that would consume its last 256 MiB.
            if item.isfile() and shutil.disk_usage(destination).free < item.size + 256 * 1024 ** 2:
                raise RuntimeError("Insufficient private Ollama installation disk space")
            # Re-check against previously extracted symlinks for every entry.
            package.extract(item, destination, filter="data")
        if process is not None and process.wait(timeout=30):
            raise RuntimeError("The Ollama compressed release is invalid")
    except BaseException:
        # Nothing is published until the complete stage validates. The caller
        # supplies a fresh staging directory, never an existing installation.
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(mode=0o700)
        raise
    finally:
        if package is not None:
            package.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


class PrivateOllamaManager(OllamaManager):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self._lifecycle = asyncio.Lock()
        self._download = asyncio.Lock()

    @property
    def binary_path(self):
        return self.binary_dir / "bin/ollama"

    @property
    def is_binary_installed(self):
        path = self.binary_path
        return (not self.binary_dir.is_symlink() and not path.parent.is_symlink()
            and not path.is_symlink() and path.is_file() and os.access(path, os.X_OK))

    @property
    def is_running(self):
        return self._process is not None and self._process.returncode is None

    def environment(self):
        home = Path(os.environ["HOME"])
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
            "TMPDIR": str(self.data_dir / "tmp"), "LANG": "C.UTF-8",
            "XDG_CACHE_HOME": str(home / ".cache"),
            "OLLAMA_HOST": f"127.0.0.1:{self._port}", "OLLAMA_MODELS": str(self.models_dir),
            "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_MAX_QUEUE": "4", "OLLAMA_KEEP_ALIVE": "1m", "OLLAMA_CONTEXT_LENGTH": "32768"}

    async def download_binary(self):
        async with self._download:
            if self.is_binary_installed:
                yield {"status": "done"}
                return
            key = _platform_key()
            if key not in {"linux-arm64", "linux-amd64"}:
                yield {"status": "error", "message": "Cloud Ollama requires a Linux runtime"}
                return
            version = os.environ.get("CLOUD_OLLAMA_RELEASE", "latest")
            if version != "latest" and not re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\d+)?", version):
                yield {"status": "error", "message": "Invalid operator Ollama release version"}
                return
            extension = os.environ.get("CLOUD_OLLAMA_ARCHIVE", "tar.zst")
            if extension not in {"tar.zst", "tgz"}:
                yield {"status": "error", "message": "Invalid operator Ollama release archive"}
                return
            prefix = "latest/download" if version == "latest" else "download/" + version
            source = f"https://github.com/ollama/ollama/releases/{prefix}/ollama-{key}.{extension}"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".ollama-install-", dir=self.data_dir))
            archive = stage / ("release." + extension)
            extracted = stage / "root"
            extracted.mkdir(mode=0o700)
            try:
                digest = hashlib.sha256()
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=300) as client:
                    url = source
                    for hop in range(6):
                        if not allowed_download_url(url):
                            raise RuntimeError("Ollama download left the approved HTTPS release origins")
                        async with client.stream("GET", url) as response:
                            if response.is_redirect:
                                url = str(response.url.join(response.headers.get("location", "")))
                                continue
                            response.raise_for_status()
                            total = int(response.headers.get("content-length", 0))
                            if total > MAX_ARCHIVE_BYTES:
                                raise RuntimeError("Ollama release exceeds the download budget")
                            completed = 0
                            with archive.open("xb") as handle:
                                async for chunk in response.aiter_bytes(1024 * 1024):
                                    completed += len(chunk)
                                    if completed > MAX_ARCHIVE_BYTES:
                                        raise RuntimeError("Ollama release exceeds the download budget")
                                    handle.write(chunk)
                                    digest.update(chunk)
                                    yield {"status": "downloading", "completed": completed, "total": total}
                            break
                    else:
                        raise RuntimeError("Ollama release redirected too many times")
                yield {"status": "extracting"}
                extraction = asyncio.create_task(asyncio.to_thread(extract_private_archive, archive, extracted))
                try:
                    await asyncio.shield(extraction)
                except asyncio.CancelledError:
                    with anyio.CancelScope(shield=True):
                        await extraction
                    raise
                binary = extracted / "bin/ollama"
                if not binary.is_file() or binary.is_symlink():
                    raise RuntimeError("Official Ollama release has no executable in bin/ollama")
                binary.chmod(0o755)
                if self.binary_dir.is_symlink():
                    raise RuntimeError("Ollama install destination must not be a symbolic link")
                if self.binary_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, self.binary_dir)
                extracted.rename(self.binary_dir)
                (self.binary_dir / "release-provenance.json").write_text(json.dumps({
                    "source": source, "sha256": digest.hexdigest(), "verified": "official HTTPS origins",
                    "vendor_checksum": "not supplied"}))
                yield {"status": "done"}
            except asyncio.CancelledError:
                raise
            except Exception:
                yield {"status": "error", "message": "Ollama 下载或安全解压失败，请检查节点网络、镜像依赖和磁盘"}
            finally:
                with anyio.CancelScope(shield=True):
                    await asyncio.to_thread(shutil.rmtree, stage, ignore_errors=True)

    async def start(self):
        async with self._lifecycle:
            if self.is_running:
                return self.base_url
            if not self.is_binary_installed:
                raise RuntimeError("Ollama is not installed in this tenant")
            if not _is_port_free(self._port):
                self._port = _find_free_port()
            for directory in (self.models_dir, self.data_dir / "tmp"):
                if directory.is_symlink():
                    raise RuntimeError("Ollama private paths must not be symbolic links")
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self.data_dir / "ollama.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                self._process = await asyncio.create_subprocess_exec(str(self.binary_path), "serve",
                    cwd=self.data_dir, env=self.environment(), stdout=descriptor, stderr=descriptor)
            finally:
                os.close(descriptor)
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    for _ in range(30):
                        if not self.is_running:
                            raise RuntimeError("Ollama process exited during startup")
                        try:
                            if (await client.get(self.base_url + "/api/tags")).status_code == 200:
                                return self.base_url
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(.5)
                raise RuntimeError("Ollama did not become ready")
            except BaseException:
                await self._stop_owned()
                raise

    async def _stop_owned(self):
        if self.is_running:
            with suppress(ProcessLookupError):
                self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 8)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self._process.kill()
                await self._process.wait()
        self._process = None

    async def stop(self):
        async with self._lifecycle:
            await self._stop_owned()

    async def get_version(self):
        if self.is_running:
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                with suppress(httpx.HTTPError, ValueError):
                    result = await client.get(self.base_url + "/api/version")
                    if result.status_code == 200:
                        return result.json().get("version")
        return None

    async def uninstall(self, delete_models=False):
        # Serialize against installation/start, and only delete this tenant's
        # explicitly owned trees. Never follow a top-level symlink target.
        async with self._download, self._lifecycle:
            await self._stop_owned()
            result = {"binary": False, "models": False, "freed_bytes": 0}
            for key, directory in (("binary", self.binary_dir), ("models", self.models_dir)):
                if key == "models" and not delete_models:
                    continue
                if directory.is_symlink():
                    raise RuntimeError("Ollama removal refuses a symbolic-link root")
                if directory.exists():
                    result["freed_bytes"] += await asyncio.to_thread(lambda: sum(
                        item.lstat().st_size for item in directory.rglob("*") if item.is_file() and not item.is_symlink()))
                    await asyncio.to_thread(shutil.rmtree, directory)
                    result[key] = True
            return result


def install_private_ollama():
    from app.ollama import manager
    manager.OllamaManager = PrivateOllamaManager
