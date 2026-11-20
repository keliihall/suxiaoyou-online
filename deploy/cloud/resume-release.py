#!/usr/bin/env python3
"""Resume the reviewed first deployment using verified, locally loaded bases.

Run only after the operator stops this deployment's interrupted build/prefetch.
This script never loads archives, resets credentials, changes the frozen source
release, stops workloads, seeds accounts, or edits the pinned master deployer.
Only its existing external build copies and auditable recipe are adapted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import sys
import tarfile
from types import ModuleType

APP = Path("/srv/suxiaoyou")
INCOMING = APP / "incoming"
MASTER = INCOMING / "deploy-release-pinned.py"
MASTER_SHA256 = "4fd0363428249a2de992a884f48f703d4a59ddd1e83b1ab2e84a62df67f18ad8"
VERSION = "20261115-2144"
ORIGIN = "https://192.168.1.211:8443"
BASE_MANIFEST = INCOMING / "sxy-amd64-bases.manifest.json"
BASE_ARCHIVE = INCOMING / "sxy-amd64-bases.tar.gz"
BASE_MANIFEST_SHA256 = "bfb688893150077f5d4d51d041b37b26ba0c97816d5be38c5ac93df6f4cbc53c"
BASE_ARCHIVE_SHA256 = "256bdb7df470594204a2c7adc83f1c1b8e29790328c9aa18227061e391fbee72"
BASE_TAGS = {
    "node:22-bookworm-slim": "suxiaoyou-base-node:20261115-amd64",
    "python:3.12-slim-bookworm": "suxiaoyou-base-python312:20261115-amd64",
    "python:3.13-slim-bookworm": "suxiaoyou-base-python313:20261115-amd64",
    "docker:28-cli": "suxiaoyou-base-docker28:20261115-amd64",
}


class ResumeError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ResumeError(message)


def private_directory(path: Path, *, strict: bool = True) -> None:
    metadata = path.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == 1001
            and not stat.S_IMODE(metadata.st_mode) & 0o022
            and (not strict or stat.S_IMODE(metadata.st_mode) == 0o700),
            "A deployment directory has unexpected ownership or permissions.")


def read_file(path: Path, maximum: int, *, private: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and metadata.st_uid == os.getuid()
                and not stat.S_IMODE(metadata.st_mode) & 0o022 and 0 <= metadata.st_size <= maximum
                and (not private or stat.S_IMODE(metadata.st_mode) == 0o600),
                "A deployment input has unsafe ownership, type, permissions or size.")
        content = handle.read(maximum + 1)
        require(len(content) == metadata.st_size and len(content) <= maximum,
                "A deployment input changed or exceeded its budget.")
        return content


def unique_object(pairs):
    value = {}
    for name, item in pairs:
        require(name not in value, "A deployment manifest contains duplicate JSON keys.")
        value[name] = item
    return value


def decode(raw: bytes):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda _value: require(False, "Non-finite manifest values are refused."))


def master_module() -> ModuleType:
    raw = read_file(MASTER, 128 * 1024)
    require(hashlib.sha256(raw).hexdigest() == MASTER_SHA256, "The pinned master deployer changed.")
    module = ModuleType("suxiaoyou_frozen_resume_master")
    module.__file__ = str(MASTER)
    exec(compile(raw, str(MASTER), "exec"), module.__dict__)
    require(module.APP == APP and module.VERSION == VERSION and module.ORIGIN == ORIGIN
            and module.DEPLOY_UID == 1001 and module.DEPLOY_USER == "suxiaoyou"
            and module.RELEASE == APP / "releases" / VERSION
            and module.CONFIG == APP / "shared/config" / VERSION
            and module.PROJECT == "suxiaoyou-cloud"
            and module.SOCKET == Path("/run/user/1001/docker.sock"),
            "The pinned master does not match this deployment.")
    return module


def check_initial_state(module) -> tuple[dict, dict[str, str]]:
    require(not (APP / "current").exists() and not (APP / "current").is_symlink(),
            "A current deployment already exists; this first-deployment resume is refused.")
    require(not (module.CONFIG / "deployment-result.json").exists()
            and not (module.CONFIG / "deployment-result.json").is_symlink(),
            "A deployment result already exists; resume is refused.")
    env_raw = read_file(module.ENV_FILE, 16384, private=True)
    values = {}
    for line in env_raw.decode("utf-8").splitlines():
        require("=" in line, "The private environment format changed.")
        name, value = line.split("=", 1)
        require(name not in values, "The private environment contains duplicate assignments.")
        values[name] = value
    expected = {
        "CLOUD_HTTP_PORT": "8443", "SUXIAOYOU_CLOUD_ALLOWED_ORIGIN": ORIGIN,
        "SUXIAOYOU_CLOUD_ENVIRONMENT": "production", "SUXIAOYOU_CLOUD_COOKIE_SECURE": "true",
        "SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER": "false", "CLOUD_DOCKER_SOCKET": str(module.SOCKET),
        "CLOUD_DOCKER_GID": "0", "CLOUD_NGINX_CONFIG": str(module.CONFIG / "nginx.conf"),
        "SUXIAOYOU_CLOUD_STORAGE_BACKEND": "local", "SUXIAOYOU_CLOUD_DEFAULT_TOKEN_LIMIT": "1000000",
        "SUXIAOYOU_CLOUD_MAX_UPLOAD_BYTES": "52428800",
    }
    require(set(values) == set(expected) | {"CLOUD_DB_PASSWORD"}
            and all(values.get(name) == value for name, value in expected.items())
            and re.fullmatch(r"[0-9a-f]{64}", values.get("CLOUD_DB_PASSWORD", "")),
            "Production flags or private environment state changed; no credentials were modified.")
    manifest, files = module.validate_package(module.SOURCE)
    for name, content, mode in files:
        path = module.RELEASE / name
        actual = read_file(path, module.MAX_SOURCE)
        require(actual == content and stat.S_IMODE(path.lstat().st_mode) == mode,
                "The extracted immutable source no longer matches its reviewed archive.")
    expected_paths = {name for name, _, _ in files}
    actual_paths = set()
    for current, directories, filenames in os.walk(module.RELEASE, followlinks=False):
        require(all(not (Path(current) / name).is_symlink() for name in directories + filenames),
                "A symbolic link appeared in the immutable source.")
        actual_paths.update((Path(current) / name).relative_to(module.RELEASE).as_posix() for name in filenames)
    require(actual_paths == expected_paths, "Unexpected files appeared in the immutable source context.")
    images, evidence_raw = module.load_base_images()
    evidence = decode(evidence_raw)
    # Render only the known non-secret proxy configuration into memory and
    # compare it, without rewriting source/configuration or running services.
    captured = {}
    writer = module.private_write
    try:
        module.private_write = lambda path, content, mode=0o600: captured.update({path: content})
        module.configure_proxy(images)
    finally:
        module.private_write = writer
    require(len(captured) == 2 and all(read_file(path, 65536) == content for path, content in captured.items()),
            "Existing proxy or Compose configuration no longer matches the reviewed deployment.")
    recipe = decode(read_file(module.CONFIG / "build-recipe.json", 512 * 1024, private=True))
    require(recipe.get("source_archive_sha256") == module.EXPECTED_SHA256
            and recipe.get("source_sha256") == manifest["source_sha256"]
            and recipe.get("base_image_evidence_sha256") == module.EXPECTED_BASE_METADATA_SHA256
            and recipe.get("platform") == "linux/amd64" and recipe.get("base_images") == images
            and recipe.get("pip_index_url") == module.PIP_INDEX and recipe.get("python_require_hashes") is True
            and recipe.get("source_context_modified") is False and "offline_resume" not in recipe,
            "The existing external build recipe changed or was already resumed.")
    require(read_file(module.CONFIG / "base-image-digests.json", 512 * 1024, private=True) == evidence_raw,
            "The saved official base-image evidence changed.")
    return {"source_manifest": manifest, "recipe": recipe, "official_evidence": evidence}, images


def inspect_offline_bases(module, official: dict) -> tuple[dict, dict[str, str]]:
    raw = read_file(BASE_MANIFEST, 512 * 1024)
    require(hashlib.sha256(raw).hexdigest() == BASE_MANIFEST_SHA256,
            "The offline base manifest no longer matches the reviewed SHA256.")
    document = decode(raw)
    require(set(document) == {"format_version", "platform", "created_at_utc", "archive", "images"}
            and document["format_version"] == 1 and document["platform"] == "linux/amd64"
            and isinstance(document["images"], list) and len(document["images"]) == 4,
            "The offline base manifest has an unsupported format.")
    archive = document["archive"]
    require(set(archive) == {"filename", "sha256", "bytes"} and archive["filename"] == BASE_ARCHIVE.name
            and isinstance(archive["bytes"], int) and 0 < archive["bytes"] <= 3 * 1024**3
            and archive["sha256"] == BASE_ARCHIVE_SHA256, "The offline archive metadata is invalid.")
    descriptor = os.open(BASE_ARCHIVE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and not stat.S_IMODE(info.st_mode) & 0o022 and info.st_size == archive["bytes"],
                "The offline archive has unsafe type, permissions, owner or size.")
        require(hashlib.file_digest(handle, "sha256").hexdigest() == archive["sha256"],
                "The actual offline archive does not match its manifest SHA256.")
    official_images = {image["source"]: image for image in official["images"]}
    tags, inspected = {}, {}
    for image in document["images"]:
        require(set(image) == {"source", "repo_tag", "official_manifest_digest", "config_digest", "verification"},
                "The offline image record has unsupported fields.")
        source = image["source"]
        require(source in BASE_TAGS and source not in tags and image["repo_tag"] == BASE_TAGS[source],
                "An offline image has an unknown source, duplicate source or unexpected local tag.")
        original = official_images[source]
        require(image["official_manifest_digest"] == original["amd64_digest"]
                and image["config_digest"] == original["verification"]["config_blob_sha256"]
                and isinstance(image["verification"], dict) and image["verification"],
                "The offline image is not the previously verified official amd64 image.")
        result = decode(module.command(["docker", "image", "inspect", "--format", "{{json .}}", image["repo_tag"]],
                                       "Verify loaded offline base image identity", capture=True, timeout=30))
        # With Docker 29's containerd store Id may be a target descriptor;
        # verify raw configuration bytes through a local Docker save below.
        require(isinstance(result.get("Id"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", result["Id"])
                and result.get("Architecture") == "amd64" and result.get("Os") == "linux"
                and image["repo_tag"] in result.get("RepoTags", []),
                "A loaded offline base image has the wrong architecture or local tag.")
        tags[source] = image["repo_tag"]
        inspected[image["repo_tag"]] = (image, result)
    require(set(tags) == set(BASE_TAGS), "The offline base image inventory is incomplete.")
    verified = verify_saved_configurations(module, inspected)
    return {"created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "offline_manifest_sha256": hashlib.sha256(raw).hexdigest(), "archive": archive,
            "verified_local_images": verified}, tags


def verify_saved_configurations(module, inspected: dict) -> list[dict]:
    """Verify config content independently of classic/containerd image-ID semantics."""
    snapshot = module.CONFIG / (".verify-loaded-bases-" + secrets.token_hex(8) + ".tar")
    module.private_write(snapshot, b"", 0o600)
    try:
        module.command(["docker", "image", "save", "--output", str(snapshot), *sorted(inspected)],
                       "Verify locally loaded base configuration snapshots", timeout=600)
        metadata = snapshot.lstat()
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid() and metadata.st_nlink == 1
                and stat.S_IMODE(metadata.st_mode) == 0o600 and 0 < metadata.st_size <= 3 * 1024**3,
                "The local base verification snapshot has unsafe state or size.")
        with tarfile.open(snapshot, "r:") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            require(len(names) == len(set(names)) and len(names) <= 4096,
                    "The local Docker snapshot has duplicate entries or exceeds its budget.")
            manifest_member = archive.getmember("manifest.json")
            require(manifest_member.isfile() and 0 < manifest_member.size <= 1024 * 1024,
                    "The local Docker snapshot lacks a bounded regular manifest.")
            manifest = decode(archive.extractfile(manifest_member).read())
            require(isinstance(manifest, list) and len(manifest) == len(inspected),
                    "The local Docker snapshot contains an unexpected image inventory.")
            found, verified = set(), []
            for item in manifest:
                repo_tags = item.get("RepoTags")
                require(isinstance(repo_tags, list) and len(repo_tags) == 1 and repo_tags[0] in inspected
                        and repo_tags[0] not in found, "The local Docker snapshot has an unexpected image tag.")
                tag = repo_tags[0]
                image, inspect = inspected[tag]
                config_path = item.get("Config")
                require(isinstance(config_path, str) and not config_path.startswith("/")
                        and ".." not in config_path.split("/") and "\\" not in config_path,
                        "The local Docker snapshot contains an unsafe config path.")
                config_member = archive.getmember(config_path)
                require(config_member.isfile() and 0 < config_member.size <= 2 * 1024 * 1024,
                        "The local Docker snapshot config is not a bounded regular file.")
                config_raw = archive.extractfile(config_member).read()
                require("sha256:" + hashlib.sha256(config_raw).hexdigest() == image["config_digest"],
                        "The locally saved config bytes do not match the official config digest.")
                config = decode(config_raw)
                diff_ids = config.get("rootfs", {}).get("diff_ids")
                require(config.get("architecture") == "amd64" and config.get("os") == "linux"
                        and isinstance(diff_ids, list) and diff_ids
                        and all(isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in diff_ids)
                        and inspect.get("RootFS", {}).get("Type") == "layers"
                        and inspect.get("RootFS", {}).get("Layers") == diff_ids,
                        "The loaded image filesystem IDs differ from the verified official configuration.")
                layers = item.get("Layers")
                require(isinstance(layers, list) and len(layers) == len(diff_ids),
                        "The local Docker snapshot has an unexpected layer inventory.")
                found.add(tag)
                verified.append({"source": image["source"], "local_tag": tag, "inspect_image_id": inspect["Id"],
                                 "config_digest": image["config_digest"], "all_rootfs_diff_ids": diff_ids,
                                 "official_manifest_digest": image["official_manifest_digest"],
                                 "raw_saved_config_sha256_verified": True})
            require(found == set(inspected), "The local Docker snapshot omits an expected image.")
            return verified
    finally:
        snapshot.unlink(missing_ok=True)


def rewrite_external_copies(module, state: dict, mirrors: dict[str, str], tags: dict[str, str], audit: dict) -> None:
    recipe = state["recipe"]
    notes = {item["target"]: item for item in recipe.get("dockerfiles", [])}
    require(set(notes) == set(module.DOCKERFILE_BASES), "The external Dockerfile inventory changed.")
    expression = re.compile(r"^FROM ([^\s]+)( AS [A-Za-z0-9_-]+)?$", re.MULTILINE)
    changes, replacements = [], []
    reverse = {mirrors[source]: tags[source] for source in tags}
    for target, bases in module.DOCKERFILE_BASES.items():
        path = module.CONFIG / "build" / ("Dockerfile." + target)
        before = read_file(path, 65536)
        ignore = read_file(path.with_name(path.name + ".dockerignore"), 65536)
        note = notes[target]
        require(hashlib.sha256(before).hexdigest() == note["adapted_dockerfile_sha256"]
                and hashlib.sha256(ignore).hexdigest() == note["dockerignore_sha256"]
                and ignore == read_file(module.RELEASE / "deploy/cloud" / (path.name + ".dockerignore"), 65536),
                "An existing external Dockerfile or its context allowlist changed.")
        text = before.decode("utf-8")
        require(tuple(match.group(1) for match in expression.finditer(text)) == tuple(mirrors[source] for source in bases),
                "An external Dockerfile has unexpected FROM instructions.")
        after = expression.sub(lambda match: "FROM " + reverse[match.group(1)] + (match.group(2) or ""), text).encode()
        replacements.append((path, after))
        changes.append({"target": target, "before_sha256": hashlib.sha256(before).hexdigest(),
                        "after_sha256": hashlib.sha256(after).hexdigest()})
    audit.update({"effective_base_images": tags, "dockerfiles": changes, "master_deployer_sha256": MASTER_SHA256,
                  "source_context_modified": False, "credentials_modified_by_resume": False})
    # Publish the audit first. A partial application cannot be silently retried.
    recipe["offline_resume"] = audit
    atomic_write(module, module.CONFIG / "build-recipe.json", (json.dumps(recipe, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    for path, content in replacements:
        atomic_write(module, path, content, 0o644)


def atomic_write(module, path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name("." + path.name + ".resume-" + secrets.token_hex(8))
    try:
        module.private_write(temporary, content, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resume() -> None:
    os.umask(0o077)
    require(os.getuid() == 1001 and os.geteuid() == 1001 and pwd.getpwuid(1001).pw_name == "suxiaoyou"
            and len(sys.argv) == 1, "Run only as suxiaoyou UID 1001 with no arguments.")
    for path in (APP, INCOMING):
        private_directory(path, strict=False)
    module = master_module()
    for path in (module.SHARED, module.SHARED / "config", module.CONFIG, module.CONFIG / "build", module.RELEASE, module.TLS):
        private_directory(path)
    lock = os.open(module.SHARED / "deploy.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, "The deployment lock has unsafe state.")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        module.progress("Verifying interrupted release, unchanged production flags and immutable source.")
        state, mirrors = check_initial_state(module)
        running = module.command(["docker", "ps", "--filter", "label=com.docker.compose.project=" + module.PROJECT,
                                  "--format", "{{.ID}}"], "Check absence of existing project workloads", capture=True, timeout=30)
        require(not running.strip(), "Project workloads already run; first-deployment resume is refused.")
        for image in (module.API_IMAGE, module.WEB_IMAGE, module.RUNTIME_IMAGE):
            matches = module.command(["docker", "image", "ls", "--filter", "reference=" + image, "--format", "{{.ID}}"],
                                     "Check version image is still unpublished", capture=True, timeout=30)
            require(not matches.strip(), "A versioned output image already exists; tag replacement is refused.")
        builder = module.command(["docker", "buildx", "inspect"], "Check embedded Docker builder", capture=True, timeout=30)
        require(re.search(rb"(?m)^Driver:\s+docker\s*$", builder) is not None,
                "The selected builder is not the embedded Docker driver; local tags would be unavailable.")
        audit, tags = inspect_offline_bases(module, state["official_evidence"])
        rewrite_external_copies(module, state, mirrors, tags, audit)
        module.command(module.compose("config", "--quiet"), "Validate resumed Compose configuration", timeout=60)
        module.progress("Resuming runtime build from verified, locally loaded official bases.")
        module.command(module.compose("--profile", "build-runtime", "build", "runtime-image"), "Resume runtime build from offline bases")
        module.progress("Building API and Web from verified local bases.")
        module.command(module.compose("build", "api", "web"), "Resume API and Web builds from offline bases")
        module.probe_socket()
        module.command(module.compose("config", "--quiet"), "Validate final socket permissions", timeout=60)
        module.command(module.compose("run", "--rm", "--no-deps", "--entrypoint", "nginx", "proxy", "-t"),
                       "Validate resumed TLS proxy configuration", timeout=120)
        module.progress("Starting the reviewed stable project and its explicit migration job.")
        module.command(module.compose("up", "-d", "--wait", "--wait-timeout", "240"), "Start resumed cloud project", timeout=360)
        module.wait_ready()
        current = APP / "current"
        require(not current.exists() and not current.is_symlink(), "The current pointer appeared during resume; stopped.")
        temporary = APP / (".current-resume-" + secrets.token_hex(8))
        temporary.symlink_to(module.RELEASE)
        os.replace(temporary, current)
        module.private_write(module.CONFIG / "deployment-result.json", (json.dumps({
            "version": VERSION, "origin": ORIGIN, "source_archive_sha256": module.EXPECTED_SHA256,
            "source_sha256": state["source_manifest"]["source_sha256"], "ready": True,
            "base_image_evidence_sha256": module.EXPECTED_BASE_METADATA_SHA256, "platform": "linux/amd64",
            "build_recipe": str(module.CONFIG / "build-recipe.json"), "admin_seeded": False, "model_seeded": False,
        }, sort_keys=True, indent=2) + "\n").encode())
        module.progress("Deployment resumed and HTTPS-ready; no administrator or model was seeded.")
    finally:
        os.close(lock)


def main() -> int:
    try:
        resume()
    except ResumeError as error:
        print("Resume stopped: " + str(error), file=sys.stderr)
        return 1
    except BaseException:
        print("Resume stopped safely; inspect the existing private deployment log and state before retrying.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
