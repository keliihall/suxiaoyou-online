#!/usr/bin/env python3
"""Deploy one reviewed cloud source release as the dedicated rootless user.

This standalone script is intentionally specific to this host and release. It
never installs OS packages, changes system services, seeds application accounts,
imports local development data, or prints generated configuration/credentials.
Failures retain private logs and the incomplete release for operator review.
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import ssl
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timezone

APP = Path("/srv/suxiaoyou")
SOURCE = APP / "incoming/release.tar.gz"
EXPECTED_SHA256 = "298a514e8ef9f02f07e6305126cf5584db884885956033de8df6c72c8e167619"
VERSION = "20261115-2144"
ORIGIN = "https://192.168.1.211:8443"
HOST_IP = "192.168.1.211"
DEPLOY_USER = "suxiaoyou"
DEPLOY_UID = 1001
PROJECT = "suxiaoyou-cloud"
SOCKET = Path("/run/user/1001/docker.sock")
SHARED = APP / "shared"
RELEASE = APP / "releases" / VERSION
ENV_FILE = SHARED / ".env.cloud"
CONFIG = SHARED / "config" / VERSION
TLS = SHARED / "tls"
LOG = SHARED / "logs" / (VERSION + ".log")
API_IMAGE = "suxiaoyou-cloud-api:" + VERSION
WEB_IMAGE = "suxiaoyou-cloud-web:" + VERSION
RUNTIME_IMAGE = "suxiaoyou-online-runtime:" + VERSION
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_SOURCE = 512 * 1024 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
BASE_METADATA = Path(__file__).resolve().with_name("base-image-digests.json")
# Filled only from the independently verified official-index/amd64 evidence.
EXPECTED_BASE_METADATA_SHA256 = "94196786fae5ff446469732f381a91f40f64c37e6d6eac2c21ad79f63827d5d1"
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
BASE_SOURCES = {
    "docker:28-cli": "docker",
    "python:3.13-slim-bookworm": "python",
    "python:3.12-slim-bookworm": "python",
    "node:22-bookworm-slim": "node",
    "postgres:17-alpine": "postgres",
    "nginx:1.28-alpine": "nginx",
}
DOCKERFILE_BASES = {
    "api": ("docker:28-cli", "python:3.13-slim-bookworm"),
    "web": ("node:22-bookworm-slim", "node:22-bookworm-slim"),
    "runtime": ("node:22-bookworm-slim", "python:3.12-slim-bookworm"),
}


class DeployError(Exception):
    """Only fixed, non-sensitive operational messages may be displayed."""


def progress(message: str) -> None:
    print(message, flush=True)


def private_directory(path: Path, mode: int = 0o700) -> None:
    if path.is_symlink():
        raise DeployError("A managed directory is a symbolic link; stopped.")
    if not path.exists():
        path.mkdir(mode=mode)
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != mode):
        raise DeployError("A managed directory has unexpected ownership or permissions; stopped.")


def private_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as handle:
        # Preserve reviewed source modes despite the process's private umask;
        # image COPY inputs must remain readable by production UID 10001.
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def regular_bytes(path: Path, maximum: int, *, private: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > maximum
                or metadata.st_uid != os.getuid() or (private and stat.S_IMODE(metadata.st_mode) != 0o600)):
            raise DeployError("A deployment input has unsafe type, owner, size or permissions; stopped.")
        content = handle.read(maximum + 1)
        if len(content) > maximum or len(content) != metadata.st_size:
            raise DeployError("A deployment input exceeded its verified size; stopped.")
        return content


def validate_package(path: Path, expected_hash: str = EXPECTED_SHA256) -> tuple[dict, list[tuple[str, bytes, int]]]:
    compressed = regular_bytes(path, MAX_ARCHIVE, private=True)
    if hashlib.sha256(compressed).hexdigest() != expected_hash:
        raise DeployError("Source package SHA256 does not match the reviewed release; stopped.")
    # Bound decompression before parsing any tar metadata or allocating file payloads.
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        raw = stream.read(MAX_SOURCE + 16 * 1024 * 1024 + 1)
    if len(raw) > MAX_SOURCE + 16 * 1024 * 1024:
        raise DeployError("Source package exceeded its expansion budget; stopped.")
    contents, modes, total = {}, {}, 0
    prefix = "suxiaoyou-release/"
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            relative = member.name.removeprefix(prefix)
            components = PurePosixPath(relative)
            if (not member.name.startswith(prefix) or not relative or components.is_absolute()
                    or relative != components.as_posix() or ".." in components.parts
                    or not member.isfile() or member.issym() or member.islnk() or member.mode not in (0o644, 0o755)
                    or relative in contents or member.size < 0 or member.size > MAX_SOURCE
                    or "\\" in relative or "\x00" in relative):
                raise DeployError("Source archive contains an unsafe or duplicate entry; stopped.")
            total += member.size
            if total > MAX_SOURCE or len(contents) >= 20000:
                raise DeployError("Source package exceeded its inventory budget; stopped.")
            handle = archive.extractfile(member)
            if handle is None:
                raise DeployError("Source package contains an unreadable entry; stopped.")
            contents[relative], modes[relative] = handle.read(), member.mode
    manifest_name = "RELEASE-MANIFEST.json"
    manifest_raw = contents.pop(manifest_name, b"")
    modes.pop(manifest_name, None)
    if not manifest_raw or len(manifest_raw) > MAX_MANIFEST:
        raise DeployError("Source package manifest is missing or exceeds its budget; stopped.")
    manifest = json.loads(manifest_raw)
    if (manifest.get("format_version") != 1 or manifest.get("application") != "suxiaoyou-online"
            or not isinstance(manifest.get("files"), list) or manifest.get("source_file_count") != len(contents)):
        raise DeployError("Source package manifest has an unsupported format; stopped.")
    expected, result = set(), []
    for entry in manifest["files"]:
        name = entry.get("path")
        if (not isinstance(name, str) or name in expected or name not in contents
                or entry.get("size") != len(contents[name])
                or entry.get("sha256") != hashlib.sha256(contents[name]).hexdigest()
                or entry.get("mode") != f"{modes[name]:04o}"):
            raise DeployError("Source file does not match its embedded manifest; stopped.")
        expected.add(name)
        result.append((name, contents[name], modes[name]))
    if expected != set(contents) or manifest.get("source_bytes") != sum(len(value) for value in contents.values()):
        raise DeployError("Source package and manifest inventories differ; stopped.")
    canonical = (json.dumps({"files": manifest["files"]}, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if manifest.get("source_sha256") != hashlib.sha256(canonical).hexdigest():
        raise DeployError("Source inventory checksum is invalid; stopped.")
    required = {"compose.cloud.yaml", "deploy/cloud/nginx.conf", "deploy/cloud/Dockerfile.api",
                "deploy/cloud/Dockerfile.web", "deploy/cloud/Dockerfile.runtime",
                "deploy/cloud/Dockerfile.api.dockerignore", "deploy/cloud/Dockerfile.web.dockerignore",
                "deploy/cloud/Dockerfile.runtime.dockerignore", ".dockerignore", ".env.cloud.example",
                "backend/requirements.txt", "backend/requirements-cloud.txt", "frontend/package-lock.json"}
    if not required.issubset(expected) or not any(name.startswith("backend/app/data/") for name in expected):
        raise DeployError("Source package is missing required production build inputs; stopped.")
    result.append((manifest_name, manifest_raw, 0o644))
    return manifest, result


def extract_verified(files: list[tuple[str, bytes, int]]) -> None:
    if RELEASE.exists() or RELEASE.is_symlink():
        raise DeployError("This release directory already exists; inspect it before retrying.")
    RELEASE.mkdir(mode=0o700)
    for name, content, mode in files:
        destination = RELEASE / name
        parent = RELEASE
        for component in PurePosixPath(name).parts[:-1]:
            parent = parent / component
            if not parent.exists():
                parent.mkdir(mode=0o755)
                parent.chmod(0o755)
        private_write(destination, content, mode)


def load_base_images() -> tuple[dict[str, str], bytes]:
    """Trust only the adjacent, reviewed evidence for six fixed official tags."""
    # This evidence contains public registry digests, not credentials.
    raw = regular_bytes(BASE_METADATA, 512 * 1024)
    if hashlib.sha256(raw).hexdigest() != EXPECTED_BASE_METADATA_SHA256:
        raise DeployError("Base-image evidence does not match its reviewed SHA256; stopped.")
    metadata = json.loads(raw)
    if (set(metadata) != {"format_version", "platform", "verified_at_utc", "images"}
            or metadata.get("format_version") != 1 or metadata.get("platform") != "linux/amd64"
            or not isinstance(metadata.get("images"), list) or len(metadata["images"]) != 6):
        raise DeployError("Base-image evidence has an unsupported format or platform; stopped.")
    verified_at = datetime.fromisoformat(metadata["verified_at_utc"].replace("Z", "+00:00"))
    if verified_at.tzinfo is None or verified_at.utcoffset() != timezone.utc.utcoffset(verified_at):
        raise DeployError("Base-image evidence must record its UTC verification time; stopped.")
    resolved = {}
    for image in metadata["images"]:
        if set(image) != {"source", "mirror_repository", "amd64_digest", "official_index_digest",
                          "mirror_verified_sha256", "official_tag_source_url", "verification"}:
            raise DeployError("Base-image evidence contains unsupported fields; stopped.")
        source = image.get("source")
        if source not in BASE_SOURCES or source in resolved:
            raise DeployError("Base-image evidence contains an unknown or duplicate source; stopped.")
        repository, tag = source.split(":", 1)
        expected_mirror = "docker.m.daocloud.io/library/" + BASE_SOURCES[source]
        official_path = "/v2/repositories/library/" + repository + "/tags/" + tag
        expected_official = {"https://registry.hub.docker.com" + official_path, "https://hub.docker.com" + official_path}
        if (image.get("mirror_repository") != expected_mirror or image.get("official_tag_source_url") not in expected_official
                or not isinstance(image.get("verification"), dict) or not image["verification"]
                or any(not isinstance(image.get(field), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image[field])
                       for field in ("amd64_digest", "official_index_digest", "mirror_verified_sha256"))
                or image["amd64_digest"] != image["mirror_verified_sha256"]):
            raise DeployError("Base-image evidence failed its official-source and mirror-digest checks; stopped.")
        resolved[source] = expected_mirror + "@" + image["amd64_digest"]
    if set(resolved) != set(BASE_SOURCES):
        raise DeployError("Base-image evidence is incomplete; stopped.")
    return resolved, raw


def configure_build_copies(images: dict[str, str], metadata_raw: bytes, source_hash: str) -> None:
    """Adapt reviewed build instructions outside the immutable source context."""
    build = CONFIG / "build"
    private_directory(build)
    notes = []
    for target, expected_bases in DOCKERFILE_BASES.items():
        filename = "Dockerfile." + target
        original = regular_bytes(RELEASE / "deploy/cloud" / filename, 65536)
        text = original.decode("utf-8")
        expression = re.compile(r"^FROM ([^\s]+)( AS [A-Za-z0-9_-]+)?$", re.MULTILINE)
        found = tuple(match.group(1) for match in expression.finditer(text))
        if found != expected_bases:
            raise DeployError("A reviewed Dockerfile has unexpected base-image instructions; stopped.")
        rewritten = expression.sub(lambda match: "FROM " + images[match.group(1)] + (match.group(2) or ""), text)
        pip_instruction = "RUN python -m pip install --require-hashes -r "
        expected_pip_count = 0 if target == "web" else 1
        if rewritten.count(pip_instruction) != expected_pip_count:
            raise DeployError("A reviewed Dockerfile has unexpected Python dependency instructions; stopped.")
        if expected_pip_count:
            rewritten = rewritten.replace(pip_instruction,
                f"RUN PIP_INDEX_URL={PIP_INDEX} PIP_EXTRA_INDEX_URL= python -m pip install --require-hashes -r ", 1)
        copy_bytes = rewritten.encode("utf-8")
        private_write(build / filename, copy_bytes, 0o644)
        ignore_name = filename + ".dockerignore"
        ignore = regular_bytes(RELEASE / "deploy/cloud" / ignore_name, 65536)
        private_write(build / ignore_name, ignore, 0o644)
        notes.append({"target": target, "original_dockerfile_sha256": hashlib.sha256(original).hexdigest(),
                      "adapted_dockerfile_sha256": hashlib.sha256(copy_bytes).hexdigest(),
                      "dockerignore_sha256": hashlib.sha256(ignore).hexdigest()})
    private_write(CONFIG / "base-image-digests.json", metadata_raw)
    private_write(CONFIG / "build-recipe.json", (json.dumps({
        "source_archive_sha256": EXPECTED_SHA256, "source_sha256": source_hash,
        "base_image_evidence_sha256": EXPECTED_BASE_METADATA_SHA256, "platform": "linux/amd64",
        "base_images": images, "pip_index_url": PIP_INDEX, "python_require_hashes": True,
        "source_context_modified": False, "dockerfiles": notes,
    }, indent=2, sort_keys=True) + "\n").encode())


def command(arguments: list[str], label: str, *, capture: bool = False, timeout: int = 7200) -> bytes:
    with LOG.open("ab") as log:
        log.write(("\n--- " + label + " ---\n").encode())
        log.flush()
        try:
            result = subprocess.run(arguments, cwd=RELEASE, env=command_environment(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE if capture else log, stderr=log, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DeployError("An operation could not complete; inspect the private deployment log.") from None
    if result.returncode:
        raise DeployError("An operation failed; inspect the private deployment log.")
    return result.stdout if capture else b""


def command_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in list(environment):
        if (name.startswith(("COMPOSE_", "SUXIAOYOU_", "NEXT_PUBLIC_"))
                or name in {"DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG",
                            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}):
            environment.pop(name, None)
    environment.update(DOCKER_HOST="unix://" + str(SOCKET), XDG_RUNTIME_DIR="/run/user/1001",
                       DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1001/bus", NO_PROXY="127.0.0.1,localhost," + HOST_IP)
    return environment


def compose(*arguments: str) -> list[str]:
    return ["docker", "compose", "--project-name", PROJECT, "--env-file", str(ENV_FILE),
            "-f", str(RELEASE / "compose.cloud.yaml"), "-f", str(CONFIG / "compose.override.yaml"), *arguments]


def configure_environment(socket_gid: int = 0) -> None:
    if ENV_FILE.exists() or ENV_FILE.is_symlink():
        # Stable DB volumes require the same password. Never silently reset it.
        raise DeployError("An existing shared environment needs operator review; no credentials were replaced.")
    existing = command(["docker", "volume", "ls", "--filter", "label=com.docker.compose.project=" + PROJECT,
                        "--format", "{{.Name}}"], "Check existing project volumes", capture=True)
    if existing.strip():
        raise DeployError("Existing project volumes have no reviewed shared environment; stopped.")
    content = (f"CLOUD_DB_PASSWORD={secrets.token_hex(32)}\nCLOUD_HTTP_PORT=8443\n"
               f"SUXIAOYOU_CLOUD_ALLOWED_ORIGIN={ORIGIN}\nSUXIAOYOU_CLOUD_ENVIRONMENT=production\n"
               "SUXIAOYOU_CLOUD_COOKIE_SECURE=true\nSUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER=false\n"
               f"CLOUD_DOCKER_SOCKET={SOCKET}\nCLOUD_DOCKER_GID={socket_gid}\n"
               f"CLOUD_NGINX_CONFIG={CONFIG / 'nginx.conf'}\nSUXIAOYOU_CLOUD_STORAGE_BACKEND=local\n"
               "SUXIAOYOU_CLOUD_DEFAULT_TOKEN_LIMIT=1000000\nSUXIAOYOU_CLOUD_MAX_UPLOAD_BYTES=52428800\n")
    private_write(ENV_FILE, content.encode())


def configure_tls() -> None:
    certificate, key = TLS / "cert.pem", TLS / "key.pem"
    if certificate.exists() or certificate.is_symlink() or key.exists() or key.is_symlink():
        raise DeployError("Existing TLS material needs operator review; no certificate was replaced.")
    command(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes", "-days", "365",
             "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=" + HOST_IP,
             "-addext", "subjectAltName=IP:" + HOST_IP], "Generate deployment-only TLS certificate", timeout=120)
    for path in (certificate, key):
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_uid != os.getuid():
            raise DeployError("TLS material has unexpected ownership or type; stopped.")
        path.chmod(0o444)
    command(["openssl", "x509", "-checkip", HOST_IP, "-noout", "-in", str(certificate)], "Verify certificate IP SAN", timeout=30)


def configure_proxy(images: dict[str, str]) -> None:
    base = (RELEASE / "deploy/cloud/nginx.conf").read_text()
    if base.count("listen 8080;") != 1 or "Strict-Transport-Security" in base:
        raise DeployError("The reviewed nginx base no longer matches the TLS adaptation; stopped.")
    tls = ("listen 8443 ssl;\n        ssl_certificate /etc/nginx/tls/cert.pem;\n"
           "        ssl_certificate_key /etc/nginx/tls/key.pem;\n        ssl_protocols TLSv1.2 TLSv1.3;")
    private_write(CONFIG / "nginx.conf", base.replace("listen 8080;", tls).encode(), 0o644)
    override = f"""services:
  db:
    image: {images['postgres:17-alpine']}
    platform: linux/amd64
  migrate:
    image: {API_IMAGE}
    platform: linux/amd64
    build:
      dockerfile: {CONFIG / 'build/Dockerfile.api'}
  api:
    image: {API_IMAGE}
    platform: linux/amd64
    build:
      dockerfile: {CONFIG / 'build/Dockerfile.api'}
    environment:
      SUXIAOYOU_CLOUD_RUNTIME_IMAGE: {RUNTIME_IMAGE}
  worker:
    image: {API_IMAGE}
    platform: linux/amd64
    build:
      dockerfile: {CONFIG / 'build/Dockerfile.api'}
  web:
    image: {WEB_IMAGE}
    platform: linux/amd64
    build:
      dockerfile: {CONFIG / 'build/Dockerfile.web'}
  runtime-image:
    image: {RUNTIME_IMAGE}
    platform: linux/amd64
    build:
      dockerfile: {CONFIG / 'build/Dockerfile.runtime'}
  proxy:
    image: {images['nginx:1.28-alpine']}
    platform: linux/amd64
    ports: !override
      - \"{HOST_IP}:8443:8443\"
    volumes:
      - {TLS / 'cert.pem'}:/etc/nginx/tls/cert.pem:ro
      - {TLS / 'key.pem'}:/etc/nginx/tls/key.pem:ro
"""
    private_write(CONFIG / "compose.override.yaml", override.encode(), 0o600)


def probe_socket() -> None:
    base = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--pids-limit", "64", "--memory", "128m", "--cpus", "0.5",
            "--user", "10001:10001", "--mount", f"type=bind,source={SOCKET},target=/var/run/docker.sock"]
    # stat is allowed without socket access; determine its actual namespace GID.
    raw = command([*base, API_IMAGE, "python", "-c", "import os; print(os.stat('/var/run/docker.sock').st_gid)"],
                  "Inspect socket GID inside the production API image", capture=True, timeout=60)
    if not raw.strip().isdigit() or not 0 <= int(raw) <= 2**32 - 2:
        raise DeployError("The API image reported an invalid socket group; stopped.")
    actual_gid = int(raw)
    info = json.loads(command([*base, "--group-add", str(actual_gid), API_IMAGE, "docker", "--host",
                              "unix:///var/run/docker.sock", "info", "--format", "{{json .}}"],
                             "Probe API UID 10001 access to the rootless daemon", capture=True, timeout=60))
    if (not any("rootless" in value for value in info.get("SecurityOptions", []))
            or str(info.get("CgroupVersion")) != "2" or info.get("CgroupDriver") != "systemd"):
        raise DeployError("The API socket probe did not reach the expected rootless cgroup-v2 daemon; stopped.")
    if actual_gid != 0:
        content = regular_bytes(ENV_FILE, 16384, private=True)
        if content.count(b"CLOUD_DOCKER_GID=0\n") != 1:
            raise DeployError("The private socket group configuration changed unexpectedly; stopped.")
        temporary = SHARED / (".env.cloud.socket-" + secrets.token_hex(8))
        private_write(temporary, content.replace(b"CLOUD_DOCKER_GID=0\n", f"CLOUD_DOCKER_GID={actual_gid}\n".encode()))
        os.replace(temporary, ENV_FILE)
    progress(f"Verified API UID 10001 rootless socket access; container socket GID {actual_gid}.")


def wait_ready() -> None:
    context = ssl.create_default_context(cafile=str(TLS / "cert.pem"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    for _ in range(60):
        try:
            with opener.open(ORIGIN + "/readyz", timeout=5) as response:
                ready = json.loads(response.read(4096))
                if response.status == 200 and ready.get("status") == "ready" and ready.get("mode") == "cloud":
                    return
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise DeployError("HTTPS readiness did not pass; current release pointer was not changed.")


def deploy() -> None:
    os.umask(0o077)
    if os.getuid() != DEPLOY_UID or pwd.getpwuid(os.getuid()).pw_name != DEPLOY_USER:
        raise DeployError("Run only as the dedicated suxiaoyou UID 1001; root deployment is refused.")
    if len(sys.argv) != 1:
        raise DeployError("This fixed reviewed release script accepts no arguments.")
    if APP.is_symlink() or not APP.is_dir() or APP.stat().st_uid != os.getuid() or APP.stat().st_mode & 0o022:
        raise DeployError("The application root has unexpected ownership or permissions; stopped.")
    if not stat.S_ISSOCK(SOCKET.lstat().st_mode) or SOCKET.stat().st_uid != os.getuid():
        raise DeployError("The dedicated rootless Docker socket is unavailable or has the wrong owner.")
    if RELEASE.exists() or RELEASE.is_symlink():
        raise DeployError("This release directory already exists; inspect it before retrying.")
    for path in (SHARED, APP / "releases", SHARED / "logs", SHARED / "config", TLS):
        private_directory(path)
    lock = os.open(SHARED / "deploy.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if CONFIG.exists() or CONFIG.is_symlink():
            raise DeployError("This release configuration already exists; inspect it before retrying.")
        private_directory(CONFIG)
        private_write(LOG, b"Su Xiaoyou deployment log; no expanded environment is recorded.\n")
        progress("Verifying reviewed source package and every manifest checksum.")
        manifest, files = validate_package(SOURCE)
        images, metadata_raw = load_base_images()
        extract_verified(files)
        progress(f"Verified and extracted {len(files)} source entries for {VERSION}.")
        for image in (API_IMAGE, WEB_IMAGE, RUNTIME_IMAGE):
            probe = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                   env=command_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=30, check=False)
            if probe.returncode != 1:
                raise DeployError("A version image already exists or image inspection failed; tag replacement is refused.")
        configure_environment()
        configure_tls()
        configure_build_copies(images, metadata_raw, manifest["source_sha256"])
        configure_proxy(images)
        command(compose("config", "--quiet"), "Validate Compose configuration", timeout=60)
        progress("Building the full tenant runtime and signed Office renderer image.")
        command(compose("--profile", "build-runtime", "build", "runtime-image"), "Build versioned full runtime image")
        progress("Building versioned API/worker and Web images.")
        command(compose("build", "api", "web"), "Build versioned API and Web images")
        probe_socket()
        command(compose("config", "--quiet"), "Validate final probed socket group", timeout=60)
        command(compose("run", "--rm", "--no-deps", "--entrypoint", "nginx", "proxy", "-t"),
                "Validate TLS nginx configuration", timeout=120)
        progress("Starting the stable Compose project and applying versioned migrations.")
        command(compose("up", "-d", "--wait", "--wait-timeout", "240"), "Start cloud project", timeout=360)
        wait_ready()
        current = APP / "current"
        if current.exists() and not current.is_symlink():
            raise DeployError("The current release pointer is not a symlink; automatic replacement is refused.")
        temporary = APP / (".current-" + secrets.token_hex(8))
        temporary.symlink_to(RELEASE)
        os.replace(temporary, current)
        private_write(CONFIG / "deployment-result.json", (json.dumps({
            "version": VERSION, "origin": ORIGIN, "source_archive_sha256": EXPECTED_SHA256,
            "source_sha256": manifest["source_sha256"], "ready": True,
            "base_image_evidence_sha256": EXPECTED_BASE_METADATA_SHA256, "platform": "linux/amd64",
            "build_recipe": str(CONFIG / "build-recipe.json"),
            "admin_seeded": False, "model_seeded": False,
        }, sort_keys=True, indent=2) + "\n").encode())
        progress(f"Deployment ready at {ORIGIN}; current now points to {VERSION}. No account or model was seeded.")
    finally:
        os.close(lock)


def main() -> None:
    try:
        deploy()
    except DeployError as exc:
        print("Deployment stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # A subprocess exception or JSON/parser error must never dump env/argv.
        print("Deployment stopped safely; inspect the private deployment log and managed paths.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
