#!/usr/bin/env python3
"""Continue this frozen first deployment after its Debian HTTP timeout.

Only the external runtime Dockerfile's package transport is adapted. The pinned
master/resume, source release, credentials and TLS files stay intact. Only the
frozen master's nonsecret Docker socket GID discovery may adapt the environment.
There is no automatic retry, registry import, account seed or Web login here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import stat
import sys
from types import ModuleType

APP = Path("/srv/suxiaoyou")
RESUME = APP / "incoming/resume-release.py"
RESUME_SHA256 = "117bc36b3e762683415a172430815742b592b4a1f42fb44de7fd79ecdb2d5356"
APT_OPTIONS = "-o Acquire::ForceIPv4=true -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"


class ContinueError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContinueError(message)


def resume_module() -> ModuleType:
    descriptor = os.open(RESUME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1
                and not stat.S_IMODE(info.st_mode) & 0o022 and info.st_size <= 128 * 1024,
                "The pinned resume has unsafe state.")
        raw = handle.read(128 * 1024 + 1)
    require(hashlib.sha256(raw).hexdigest() == RESUME_SHA256, "The pinned resume changed.")
    module = ModuleType("suxiaoyou_frozen_apt_resume")
    module.__file__ = str(RESUME)
    exec(compile(raw, str(RESUME), "exec"), module.__dict__)
    return module


def unchanged_inputs(resume, master) -> dict[Path, bytes]:
    """Keep private bytes in memory only; never publish secret values or hashes."""
    result = {master.ENV_FILE: resume.read_file(master.ENV_FILE, 16384, private=True)}
    for name in ("cert.pem", "key.pem"):
        path = master.TLS / name
        result[path] = resume.read_file(path, 16384)
        require(stat.S_IMODE(path.lstat().st_mode) == 0o444, "TLS file permissions changed.")
    values = {}
    for line in result[master.ENV_FILE].decode("utf-8").splitlines():
        require("=" in line, "The private environment format changed.")
        name, value = line.split("=", 1)
        require(name not in values, "The private environment contains duplicate names.")
        values[name] = value
    expected = {
        "CLOUD_HTTP_PORT": "8443", "SUXIAOYOU_CLOUD_ALLOWED_ORIGIN": master.ORIGIN,
        "SUXIAOYOU_CLOUD_ENVIRONMENT": "production", "SUXIAOYOU_CLOUD_COOKIE_SECURE": "true",
        "SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER": "false", "CLOUD_DOCKER_SOCKET": str(master.SOCKET),
        "CLOUD_DOCKER_GID": "0", "CLOUD_NGINX_CONFIG": str(master.CONFIG / "nginx.conf"),
        "SUXIAOYOU_CLOUD_STORAGE_BACKEND": "local", "SUXIAOYOU_CLOUD_DEFAULT_TOKEN_LIMIT": "1000000",
        "SUXIAOYOU_CLOUD_MAX_UPLOAD_BYTES": "52428800",
    }
    require(set(values) == set(expected) | {"CLOUD_DB_PASSWORD"}
            and all(values.get(name) == value for name, value in expected.items())
            and re.fullmatch(r"[0-9a-f]{64}", values.get("CLOUD_DB_PASSWORD", "")),
            "Production environment flags changed; no credentials were modified.")
    return result


def verify_source(resume, master, recipe: dict) -> dict:
    # The original archive was fully verified by both frozen predecessors. Check
    # the canonical extracted inventory and its files once, without decompressing
    # or reimporting the already verified source/base archives again.
    raw = resume.read_file(master.RELEASE / "RELEASE-MANIFEST.json", master.MAX_MANIFEST)
    manifest = resume.decode(raw)
    require(manifest.get("format_version") == 1 and manifest.get("application") == "suxiaoyou-online"
            and isinstance(manifest.get("files"), list), "The extracted source inventory is invalid.")
    canonical = (json.dumps({"files": manifest["files"]}, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    require(hashlib.sha256(canonical).hexdigest() == recipe.get("source_sha256") == manifest.get("source_sha256"),
            "The extracted source inventory changed.")
    expected, total = {"RELEASE-MANIFEST.json"}, 0
    for item in manifest["files"]:
        name = item.get("path")
        require(isinstance(name, str) and name not in expected and not name.startswith("/")
                and ".." not in PurePosixPath(name).parts and "\\" not in name
                and name == PurePosixPath(name).as_posix(), "An immutable source path is unsafe.")
        path = master.RELEASE / name
        content = resume.read_file(path, master.MAX_SOURCE)
        require(len(content) == item.get("size") and hashlib.sha256(content).hexdigest() == item.get("sha256")
                and f"{stat.S_IMODE(path.lstat().st_mode):04o}" == item.get("mode"),
                "An immutable source file changed.")
        expected.add(name)
        total += len(content)
    actual = set()
    for current, directories, filenames in os.walk(master.RELEASE, followlinks=False):
        require(all(not (Path(current) / name).is_symlink() for name in directories + filenames),
                "A symbolic link appeared in the immutable source.")
        actual.update((Path(current) / name).relative_to(master.RELEASE).as_posix() for name in filenames)
    require(actual == expected and manifest.get("source_file_count") == len(expected) - 1
            and manifest.get("source_bytes") == total, "The immutable source inventory changed.")
    return manifest


def verify_state(resume, master) -> tuple[dict, dict]:
    for path in (APP / "current", master.CONFIG / "deployment-result.json"):
        require(not path.exists() and not path.is_symlink(), "An active release/result exists; continuation refused.")
    recipe = resume.decode(resume.read_file(master.CONFIG / "build-recipe.json", 512 * 1024, private=True))
    images, evidence_raw = master.load_base_images()
    require(recipe.get("source_archive_sha256") == master.EXPECTED_SHA256
            and recipe.get("base_image_evidence_sha256") == master.EXPECTED_BASE_METADATA_SHA256
            and recipe.get("platform") == "linux/amd64" and recipe.get("base_images") == images
            and recipe.get("pip_index_url") == master.PIP_INDEX and recipe.get("python_require_hashes") is True
            and recipe.get("source_context_modified") is False and "apt_transport_continuation" not in recipe,
            "The existing build recipe changed or was already continued.")
    require(resume.read_file(master.CONFIG / "base-image-digests.json", 512 * 1024, private=True) == evidence_raw,
            "The saved official base evidence changed.")
    source_manifest = verify_source(resume, master, recipe)
    captured, writer = {}, master.private_write
    try:
        master.private_write = lambda path, content, mode=0o600: captured.update({path: content})
        master.configure_proxy(images)
    finally:
        master.private_write = writer
    require(len(captured) == 2 and all(resume.read_file(path, 65536) == value for path, value in captured.items()),
            "The existing proxy or Compose configuration changed.")
    audit = recipe.get("offline_resume", {})
    require(audit.get("master_deployer_sha256") == resume.MASTER_SHA256
            and audit.get("offline_manifest_sha256") == resume.BASE_MANIFEST_SHA256
            and audit.get("archive", {}).get("sha256") == resume.BASE_ARCHIVE_SHA256
            and audit.get("effective_base_images") == resume.BASE_TAGS
            and audit.get("source_context_modified") is False
            and audit.get("credentials_modified_by_resume") is False,
            "The prior offline-resume audit is missing or changed.")
    original_notes = {entry["target"]: entry for entry in recipe.get("dockerfiles", [])}
    resumed_notes = {entry["target"]: entry for entry in audit.get("dockerfiles", [])}
    require(set(original_notes) == set(resumed_notes) == set(master.DOCKERFILE_BASES),
            "The external Dockerfile inventory changed.")
    from_expression = re.compile(r"^FROM ([^\s]+)( AS [A-Za-z0-9_-]+)?$", re.MULTILINE)
    for target, bases in master.DOCKERFILE_BASES.items():
        path = master.CONFIG / "build" / ("Dockerfile." + target)
        current = resume.read_file(path, 65536)
        ignore = resume.read_file(path.with_name(path.name + ".dockerignore"), 65536)
        require(resumed_notes[target].get("before_sha256") == original_notes[target].get("adapted_dockerfile_sha256")
                and hashlib.sha256(current).hexdigest() == resumed_notes[target].get("after_sha256")
                and hashlib.sha256(ignore).hexdigest() == original_notes[target].get("dockerignore_sha256")
                and ignore == resume.read_file(master.RELEASE / "deploy/cloud" / (path.name + ".dockerignore"), 65536)
                and tuple(match.group(1) for match in from_expression.finditer(current.decode()))
                    == tuple(resume.BASE_TAGS[base] for base in bases),
                "An external Dockerfile, base mapping or context allowlist changed.")
    # Reuse the already verified config/diff-ID evidence. Checking all local tag
    # descriptors here requires no registry traffic or second 600MB Docker save.
    official = {item["source"]: item for item in resume.decode(evidence_raw)["images"]}
    found = set()
    for item in audit.get("verified_local_images", []):
        source, tag = item.get("source"), item.get("local_tag")
        require(source in resume.BASE_TAGS and source not in found and tag == resume.BASE_TAGS[source],
                "The prior local-base inventory changed.")
        expected = official[source]
        require(item.get("raw_saved_config_sha256_verified") is True
                and item.get("config_digest") == expected["verification"]["config_blob_sha256"]
                and item.get("official_manifest_digest") == expected["amd64_digest"],
                "The prior local-base verification evidence changed.")
        actual = resume.decode(master.command(["docker", "image", "inspect", "--format", "{{json .}}", tag],
                                              "Verify unchanged loaded base descriptor", capture=True, timeout=30))
        require(actual.get("Id") == item.get("inspect_image_id") and actual.get("Os") == "linux"
                and actual.get("Architecture") == "amd64" and tag in actual.get("RepoTags", [])
                and actual.get("RootFS", {}).get("Type") == "layers"
                and actual.get("RootFS", {}).get("Layers") == item.get("all_rootfs_diff_ids"),
                "A verified local base image changed since the offline resume.")
        found.add(source)
    require(found == set(resume.BASE_TAGS), "The verified local base inventory is incomplete.")
    return recipe, source_manifest


def adapted_runtime(before: bytes) -> bytes:
    text = before.decode("utf-8")
    old = "RUN apt-get update \\\n    && apt-get install -y --no-install-recommends \\\n"
    new = ("RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' "
           "/etc/apt/sources.list.d/debian.sources \\\n"
           f"    && apt-get {APT_OPTIONS} update \\\n"
           f"    && apt-get {APT_OPTIONS} install -y --no-install-recommends \\\n")
    require(text.count(old) == 1 and text.count("apt-get update") == 1 and text.count("apt-get install") == 1,
            "The runtime package instruction no longer matches the reviewed adaptation.")
    return text.replace(old, new, 1).encode()


def probe_socket_retaining_credentials(resume, master, before: bytes) -> tuple[bytes, int]:
    require(resume.read_file(master.ENV_FILE, 16384, private=True) == before,
            "The private environment changed before the socket probe.")
    master.probe_socket()
    after = resume.read_file(master.ENV_FILE, 16384, private=True)
    matches = re.findall(rb"(?m)^CLOUD_DOCKER_GID=([0-9]+)$", after)
    require(len(matches) == 1 and 0 <= int(matches[0]) <= 2**32 - 2,
            "The discovered Docker socket group is invalid.")
    gid = int(matches[0])
    require(before.count(b"CLOUD_DOCKER_GID=0\n") == 1
            and after == before.replace(b"CLOUD_DOCKER_GID=0\n", f"CLOUD_DOCKER_GID={gid}\n".encode(), 1),
            "The socket probe changed more than the permitted nonsecret group number.")
    return after, gid


def continue_release() -> None:
    os.umask(0o077)
    require(os.getuid() == os.geteuid() == 1001 and pwd.getpwuid(1001).pw_name == "suxiaoyou"
            and len(sys.argv) == 1, "Run only as suxiaoyou UID 1001 with no arguments.")
    resume = resume_module()
    master = resume.master_module()
    for path in (APP, APP / "incoming"):
        resume.private_directory(path, strict=False)
    for path in (master.SHARED, master.SHARED / "config", master.CONFIG, master.CONFIG / "build", master.RELEASE, master.TLS):
        resume.private_directory(path)
    lock = os.open(master.SHARED / "deploy.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, "The deployment lock has unsafe state.")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        preserved = unchanged_inputs(resume, master)
        master.progress("Verifying the failed offline resume and unchanged production inputs.")
        recipe, manifest = verify_state(resume, master)
        running = master.command(["docker", "ps", "--filter", "label=com.docker.compose.project=" + master.PROJECT,
                                  "--format", "{{.ID}}"], "Check no existing project workloads", capture=True, timeout=30)
        require(not running.strip(), "Project workloads already run; continuation refused.")
        for image in (master.API_IMAGE, master.WEB_IMAGE, master.RUNTIME_IMAGE):
            matches = master.command(["docker", "image", "ls", "--filter", "reference=" + image, "--format", "{{.ID}}"],
                                     "Check no published version image", capture=True, timeout=30)
            require(not matches.strip(), "A versioned output image exists; replacement refused.")
        builder = master.command(["docker", "buildx", "inspect"], "Check embedded Docker builder", capture=True, timeout=30)
        require(re.search(rb"(?m)^Driver:\s+docker\s*$", builder) is not None, "The builder cannot use the verified local bases.")
        path = master.CONFIG / "build/Dockerfile.runtime"
        before = resume.read_file(path, 65536)
        after = adapted_runtime(before)
        recipe["apt_transport_continuation"] = {
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "master_deployer_sha256": resume.MASTER_SHA256, "offline_resume_sha256": RESUME_SHA256,
            "target": "runtime", "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "reason": "Debian HTTP package download connection timed out; verified HTTPS mirror package probe succeeded.",
            "apt_source_replacement": {"from": "http://deb.debian.org/debian", "to": "https://mirrors.tuna.tsinghua.edu.cn/debian"},
            "apt_options": APT_OPTIONS, "debian_signature_verification_unchanged": True,
            "python_hash_requirements_unchanged": True, "source_context_modified": False,
            "environment_modified": False, "tls_modified": False, "credentials_modified": False,
            "socket_gid_probe_pending": True,
            "permitted_environment_change": "CLOUD_DOCKER_GID=0 to the frozen master's observed namespace GID only",
        }
        # Publish audit first; partial adaptation or a second run is never hidden.
        resume.atomic_write(master, master.CONFIG / "build-recipe.json",
                            (json.dumps(recipe, sort_keys=True, indent=2) + "\n").encode(), 0o600)
        resume.atomic_write(master, path, after, 0o644)
        master.command(master.compose("config", "--quiet"), "Validate continued Compose configuration", timeout=60)
        master.progress("Building runtime with verified HTTPS Debian transport and fixed package signatures.")
        master.command(master.compose("--profile", "build-runtime", "build", "runtime-image"), "Continue runtime build with HTTPS Debian mirror")
        master.progress("Building the unchanged API and Web release.")
        master.command(master.compose("build", "api", "web"), "Continue API and Web builds")
        env_after, socket_gid = probe_socket_retaining_credentials(resume, master, preserved[master.ENV_FILE])
        preserved[master.ENV_FILE] = env_after
        recipe["apt_transport_continuation"].update(socket_gid_probe_pending=False,
                                                  docker_socket_gid=socket_gid,
                                                  environment_modified=(socket_gid != 0))
        resume.atomic_write(master, master.CONFIG / "build-recipe.json",
                            (json.dumps(recipe, sort_keys=True, indent=2) + "\n").encode(), 0o600)
        master.command(master.compose("config", "--quiet"), "Validate final production configuration", timeout=60)
        master.command(master.compose("run", "--rm", "--no-deps", "--entrypoint", "nginx", "proxy", "-t"),
                       "Validate continued TLS proxy configuration", timeout=120)
        master.progress("Starting the stable cloud project and explicit migration job.")
        master.command(master.compose("up", "-d", "--wait", "--wait-timeout", "240"), "Start continued cloud project", timeout=360)
        master.wait_ready()
        require(all(resume.read_file(path, 16384, private=(path == master.ENV_FILE)) == content
                    for path, content in preserved.items()), "A preserved private input changed; current was not published.")
        current = APP / "current"
        require(not current.exists() and not current.is_symlink(), "The current pointer appeared during continuation.")
        temporary = APP / (".current-continue-" + secrets.token_hex(8))
        temporary.symlink_to(master.RELEASE)
        os.replace(temporary, current)
        master.private_write(master.CONFIG / "deployment-result.json", (json.dumps({
            "version": master.VERSION, "origin": master.ORIGIN, "source_archive_sha256": master.EXPECTED_SHA256,
            "source_sha256": manifest["source_sha256"], "ready": True,
            "base_image_evidence_sha256": master.EXPECTED_BASE_METADATA_SHA256, "platform": "linux/amd64",
            "build_recipe": str(master.CONFIG / "build-recipe.json"), "admin_seeded": False, "model_seeded": False,
        }, sort_keys=True, indent=2) + "\n").encode())
        master.progress("Deployment continued and HTTPS-ready; administrator/model seeding remains pending.")
    finally:
        os.close(lock)


def main() -> int:
    try:
        continue_release()
    except ContinueError as error:
        print("Continuation stopped: " + str(error), file=sys.stderr)
        return 1
    except BaseException:
        print("Continuation stopped safely; inspect the private deployment log and state before another attempt.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
