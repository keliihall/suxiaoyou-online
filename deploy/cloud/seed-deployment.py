#!/usr/bin/env python3
"""Seed the reviewed production release using private JSON on stdin only.

Run as the dedicated rootless deployment user after deployment readiness.
The seed is never written to the release, a file, a log, or a command argument.
Only the two existing deployment-result seed flags are updated after success.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
import ssl
import stat
import subprocess
import sys
from types import ModuleType
import urllib.request


APP = Path("/srv/suxiaoyou")
INCOMING = APP / "incoming"
DEPLOY_SOURCE = INCOMING / "deploy-release-pinned.py"
DEPLOY_SHA256 = "4fd0363428249a2de992a884f48f703d4a59ddd1e83b1ab2e84a62df67f18ad8"
BOOTSTRAP_SOURCE = INCOMING / "bootstrap-seed.py"
BOOTSTRAP_SHA256 = "09f58a3d492d411e3aea4670567dd6eb50f0f13460d758f8c670fcfc103162ef"
DEPLOY_USER = "suxiaoyou"
DEPLOY_UID = 1001
VERSION = "20261115-2144"
ORIGIN = "https://192.168.1.211:8443"
SHARED = APP / "shared"
CONFIG = SHARED / "config" / VERSION
RELEASE = APP / "releases" / VERSION
RESULT_NAME = "deployment-result.json"
MAX_INPUT_BYTES = 64 * 1024


class SeedFailure(Exception):
    pass


def require(condition):
    if not condition:
        raise SeedFailure


def private_directory(path, *, private=True):
    metadata = path.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == DEPLOY_UID
            and not stat.S_IMODE(metadata.st_mode) & 0o022
            and (not private or stat.S_IMODE(metadata.st_mode) == 0o700))


def regular_bytes(path, maximum, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                and metadata.st_uid == DEPLOY_UID
                and 0 < metadata.st_size <= maximum
                and not stat.S_IMODE(metadata.st_mode) & 0o022
                and (not private or stat.S_IMODE(metadata.st_mode) == 0o600))
        content = handle.read(maximum + 1)
    require(len(content) == metadata.st_size and len(content) <= maximum)
    return content


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def invalid_constant(_value):
    raise SeedFailure


def load_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=invalid_constant)


def read_seed():
    require(not sys.stdin.isatty())
    content = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    require(0 < len(content) <= MAX_INPUT_BYTES)
    document = load_json(content)
    require(isinstance(document, dict)
            and set(document) == {"schema_version", "admin", "routes"}
            and type(document["schema_version"]) is int
            and document["schema_version"] == 1
            and isinstance(document["admin"], dict)
            and isinstance(document["routes"], list)
            and 1 <= len(document["routes"]) <= 8)
    # Full credential, model and empty-database validation remains inside the
    # pinned bootstrap code in the actual production API environment.
    return content, len(document["routes"])


def deployment_module():
    raw = regular_bytes(DEPLOY_SOURCE, 128 * 1024)
    require(hashlib.sha256(raw).hexdigest() == DEPLOY_SHA256)
    module = ModuleType("suxiaoyou_reviewed_seed_deployment")
    module.__file__ = str(DEPLOY_SOURCE)
    # Compile exactly the verified bytes rather than reopening a mutable path.
    # __name__ differs from __main__, so no deployment action is invoked.
    exec(compile(raw, str(DEPLOY_SOURCE), "exec"), module.__dict__)
    require(module.APP == APP and module.VERSION == VERSION
            and module.ORIGIN == ORIGIN and module.RELEASE == RELEASE
            and module.CONFIG == CONFIG and module.SHARED == SHARED
            and module.DEPLOY_UID == DEPLOY_UID
            and module.DEPLOY_USER == DEPLOY_USER
            and module.PROJECT == "suxiaoyou-cloud"
            and module.SOCKET == Path("/run/user/1001/docker.sock")
            and callable(module.compose) and callable(module.command_environment))
    return module


def checked_result(module):
    raw = regular_bytes(CONFIG / RESULT_NAME, 64 * 1024, private=True)
    document = load_json(raw)
    require(isinstance(document, dict) and set(document) == {
        "version", "origin", "source_archive_sha256", "source_sha256", "ready",
        "base_image_evidence_sha256", "platform", "build_recipe",
        "admin_seeded", "model_seeded",
    })
    require(document["version"] == VERSION and document["origin"] == ORIGIN
            and document["source_archive_sha256"] == module.EXPECTED_SHA256
            and isinstance(document["source_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", document["source_sha256"])
            and document["base_image_evidence_sha256"] == module.EXPECTED_BASE_METADATA_SHA256
            and document["platform"] == "linux/amd64"
            and document["build_recipe"] == str(CONFIG / "build-recipe.json")
            and document["ready"] is True
            and document["admin_seeded"] is False and document["model_seeded"] is False)
    return raw, document


def require_ready(module):
    # Trust only this deployment's reviewed server certificate, without proxy
    # inheritance, redirects, disabled TLS verification or a public CA change.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    context = ssl.create_default_context(cafile=str(module.TLS / "cert.pem"))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
        NoRedirect())
    with opener.open(ORIGIN + "/readyz", timeout=5) as response:
        require(response.status == 200 and response.geturl() == ORIGIN + "/readyz")
        raw = response.read(4097)
    require(len(raw) <= 4096)
    ready = load_json(raw)
    require(isinstance(ready, dict) and ready.get("status") == "ready"
            and ready.get("mode") == "cloud")


def update_result(original, document):
    # A concurrent/manual state change must never be silently overwritten.
    require(regular_bytes(CONFIG / RESULT_NAME, 64 * 1024, private=True) == original)
    document["admin_seeded"] = True
    document["model_seeded"] = True
    content = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
    directory_fd = os.open(CONFIG, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".deployment-result-seeded-" + secrets.token_hex(12)
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, RESULT_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        created = False
        os.fsync(directory_fd)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


def seed_deployment():
    os.umask(0o077)
    require(os.getuid() == DEPLOY_UID and os.geteuid() == DEPLOY_UID
            and pwd.getpwuid(DEPLOY_UID).pw_name == DEPLOY_USER and len(sys.argv) == 1)
    # The host bootstrap intentionally creates /srv/suxiaoyou as 0750.
    for path in (APP, INCOMING):
        private_directory(path, private=False)
    for path in (SHARED, SHARED / "config", CONFIG, RELEASE):
        private_directory(path)
    require((APP / "current").is_symlink() and (APP / "current").resolve() == RELEASE)
    lock = os.open(SHARED / "deploy.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == DEPLOY_UID
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        module = deployment_module()
        original, document = checked_result(module)
        bootstrap = regular_bytes(BOOTSTRAP_SOURCE, MAX_INPUT_BYTES)
        require(hashlib.sha256(bootstrap).hexdigest() == BOOTSTRAP_SHA256)
        require_ready(module)
        seed_bytes, route_count = read_seed()
        completed = subprocess.run(
            module.compose("exec", "-T", "api", "python", "-c", bootstrap.decode("utf-8")),
            cwd=RELEASE, env=module.command_environment(), input=seed_bytes,
            capture_output=True, timeout=90, check=False,
        )
        # Subprocess output is consumed only for this exact success contract.
        # Never relay even a pinned helper's stderr or an exception's argv/input.
        require(completed.returncode == 0 and not completed.stderr.strip()
                and 0 < len(completed.stdout) <= 4096)
        result = load_json(completed.stdout)
        require(isinstance(result, dict)
                and set(result) == {"status", "admin_count", "route_count"}
                and result["status"] == "created"
                and type(result["admin_count"]) is int and result["admin_count"] == 1
                and type(result["route_count"]) is int and result["route_count"] == route_count)
        del seed_bytes
        update_result(original, document)
    finally:
        os.close(lock)


def main():
    logging.disable(logging.CRITICAL)
    try:
        seed_deployment()
    except BaseException:
        # A timeout/result-write failure may happen after the DB transaction
        # committed. Do not retry automatically or expose captured output.
        sys.stderr.write("初始化未完成：请先人工核验数据库和部署状态；未输出凭据，请勿直接重试。\n")
        return 1
    sys.stdout.write('{"status":"seeded","admin_seeded":true,"model_seeded":true}\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
