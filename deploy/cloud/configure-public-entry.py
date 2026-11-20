#!/usr/bin/env python3
"""Apply the fixed public Origin/redirects without changing ports or certificates.

Run once as root or suxiaoyou with the separately reviewed runtime helper SHA.
Public certificate issuance is a separate step; the existing IP certificate is
retained and this operation does not claim public hostname TLS verification.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import ssl
import stat
import subprocess
import sys
import time
from types import ModuleType
import urllib.request

APP = Path("/srv/suxiaoyou")
VERSION = "20261115-2144"
MASTER = APP / "incoming/deploy-release-pinned.py"
MASTER_SHA256 = "4fd0363428249a2de992a884f48f703d4a59ddd1e83b1ab2e84a62df67f18ad8"
HELPER = APP / "incoming/public-origin-runtime.py"
PUBLIC = "https://nav.cyneu.com:18443"
INTERNAL = "https://192.168.1.211:8443"
PUBLIC_CONFIG = APP / "shared/config" / (VERSION + "-public")
AUDIT = PUBLIC_CONFIG / "public-entry-result.json"


class OperationFailure(Exception):
    pass


def require(condition):
    if not condition:
        raise OperationFailure


def decode(raw):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result)
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: require(False))


def read_file(path, maximum, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == 1001
                and not stat.S_IMODE(info.st_mode) & 0o022 and 0 < info.st_size <= maximum
                and (not private or stat.S_IMODE(info.st_mode) == 0o600))
        raw = handle.read(maximum + 1)
    require(len(raw) == info.st_size)
    return raw


def directory(path, *, strict=True):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 1001
            and not stat.S_IMODE(info.st_mode) & 0o022
            and (not strict or stat.S_IMODE(info.st_mode) == 0o700))


def atomic_write(master, path, content, mode=0o600):
    temporary = path.with_name("." + path.name + ".public-" + secrets.token_hex(8))
    try:
        master.private_write(temporary, content, mode)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def deployment_module():
    raw = read_file(MASTER, 128 * 1024)
    require(hashlib.sha256(raw).hexdigest() == MASTER_SHA256)
    module = ModuleType("suxiaoyou_public_entry_frozen_master")
    module.__file__ = str(MASTER)
    exec(compile(raw, str(MASTER), "exec"), module.__dict__)
    require(module.APP == APP and module.VERSION == VERSION and module.ORIGIN == INTERNAL
            and module.DEPLOY_UID == 1001 and module.DEPLOY_USER == "suxiaoyou"
            and module.CONFIG == APP / "shared/config" / VERSION
            and module.RELEASE == APP / "releases" / VERSION
            and module.PROJECT == "suxiaoyou-cloud"
            and module.SOCKET == Path("/run/user/1001/docker.sock"))
    return module


def command(master, arguments, *, content=None, timeout=180):
    completed = subprocess.run(arguments, cwd=master.RELEASE, env=master.command_environment(),
                               input=content, stdin=subprocess.DEVNULL if content is None else None,
                               capture_output=True, timeout=timeout, check=False)
    require(completed.returncode == 0)
    return completed.stdout


def helper(master, source, mode):
    raw = command(master, master.compose("exec", "-T", "api", "python", "-", "--mode", mode),
                  content=source, timeout=660)
    require(0 < len(raw) <= 256 * 1024)
    value = decode(raw)
    require(value.get("complete") is True and value.get("ready") is True and value.get("mode") == mode
            and value.get("target_origin") == PUBLIC
            and value.get("stage") == ("preflight_ready" if mode == "preflight" else "complete")
            and all(type(value.get(name)) is int and value[name] == 0
                    for name in ("active_requests", "active_jobs", "active_streams", "reserved_tokens"))
            and type(value.get("active_users")) is int and 0 <= value["active_users"] <= 10000)
    if mode == "preflight":
        require(value.get("source_origin") == INTERNAL)
    else:
        require(type(value.get("restarted_users")) is int and value["restarted_users"] >= 0)
    result = {name: value[name] for name in ("mode", "stage", "active_users", "active_requests", "active_jobs",
                                          "active_streams", "reserved_tokens")}
    if mode == "restart":
        result["restarted_users"] = value["restarted_users"]
    return result


def ready(master, *, wait=False):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            return None
    context = ssl.create_default_context(cafile=str(master.TLS / "cert.pem"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=context), NoRedirect())
    deadline = time.monotonic() + (120 if wait else 0)
    while True:
        try:
            with opener.open(INTERNAL + "/readyz", timeout=5) as response:
                raw = response.read(4097)
                require(response.status == 200 and response.geturl() == INTERNAL + "/readyz" and len(raw) <= 4096)
            value = decode(raw)
            require(value.get("status") == "ready" and value.get("mode") == "cloud")
            return
        except (OSError, ValueError, OperationFailure):
            if time.monotonic() >= deadline:
                raise OperationFailure from None
            time.sleep(2)


def adapt_nginx(before):
    text = before.decode("utf-8")
    require(text.count("listen 8443 ssl;") == 1
            and text.count("ssl_certificate /etc/nginx/tls/cert.pem;") == 1
            and text.count("ssl_certificate_key /etc/nginx/tls/key.pem;") == 1
            and "error_page 497" not in text and "location = / {" not in text)
    replacements = (
        ("server_name _;", "server_name nav.cyneu.com;\n        error_page 497 =308 " + PUBLIC + "$request_uri;"),
        ("        location / {", "        location = / {\n            return 308 " + PUBLIC
         + "/cloud$is_args$args;\n        }\n\n        location / {"),
    )
    for old, new in replacements:
        require(text.count(old) == 1)
        text = text.replace(old, new, 1)
    return text.encode()


def adapt_environment(master, before, destination):
    values = {}
    for line in before.decode("utf-8").splitlines():
        require("=" in line)
        name, value = line.split("=", 1)
        require(name not in values)
        values[name] = value
    expected = {"SUXIAOYOU_CLOUD_ALLOWED_ORIGIN": INTERNAL,
                "CLOUD_NGINX_CONFIG": str(master.CONFIG / "nginx.conf")}
    require(all(values.get(name) == value for name, value in expected.items())
            and values.get("SUXIAOYOU_CLOUD_ENVIRONMENT") == "production"
            and values.get("SUXIAOYOU_CLOUD_COOKIE_SECURE") == "true"
            and values.get("SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER") == "false"
            and values.get("CLOUD_DOCKER_SOCKET") == str(master.SOCKET)
            and values.get("CLOUD_DOCKER_GID") == "983")
    after = before
    for name, new in (("SUXIAOYOU_CLOUD_ALLOWED_ORIGIN", PUBLIC), ("CLOUD_NGINX_CONFIG", str(destination))):
        old_line, new_line = f"{name}={expected[name]}\n".encode(), f"{name}={new}\n".encode()
        require(after.count(old_line) == 1)
        after = after.replace(old_line, new_line, 1)
    return after


def apply(helper_hash):
    account = pwd.getpwnam("suxiaoyou")
    require(os.getuid() == os.geteuid() and os.getuid() in {0, 1001} and account.pw_uid == 1001)
    if os.getuid() == 0:
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    require(os.getuid() == os.geteuid() == 1001 and pwd.getpwuid(1001).pw_name == "suxiaoyou")
    os.environ["HOME"] = account.pw_dir
    os.umask(0o077)
    master = deployment_module()
    for path in (APP, APP / "incoming"):
        directory(path, strict=False)
    for path in (master.SHARED, master.SHARED / "config", master.CONFIG, master.RELEASE, master.TLS):
        directory(path)
    require((APP / "current").is_symlink() and (APP / "current").resolve() == master.RELEASE
            and not PUBLIC_CONFIG.exists() and not PUBLIC_CONFIG.is_symlink())
    source = read_file(HELPER, 128 * 1024, private=True)
    require(hashlib.sha256(source).hexdigest() == helper_hash)
    lock = os.open(master.SHARED / "deploy.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    summary = {"version": VERSION, "origin": PUBLIC, "stage": "validating", "complete": False,
               "certificate_status": "existing_ip_certificate_public_hostname_pending", "automatic_rollback": False}
    audit_created = False
    try:
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result_raw = read_file(master.CONFIG / "deployment-result.json", 65536, private=True)
        result = decode(result_raw)
        require(result.get("version") == VERSION and result.get("origin") == INTERNAL and result.get("ready") is True
                and result.get("source_archive_sha256") == master.EXPECTED_SHA256
                and result.get("base_image_evidence_sha256") == master.EXPECTED_BASE_METADATA_SHA256)
        env_before = read_file(master.ENV_FILE, 16384, private=True)
        nginx_before = read_file(master.CONFIG / "nginx.conf", 65536)
        nginx_path = PUBLIC_CONFIG / "nginx.conf"
        nginx_after = adapt_nginx(nginx_before)
        env_after = adapt_environment(master, env_before, nginx_path)
        ready(master)
        summary["stage"] = "runtime_preflight"
        preflight = helper(master, source, "preflight")
        summary["stage"] = "preparing_configuration"
        PUBLIC_CONFIG.mkdir(mode=0o700)
        directory(PUBLIC_CONFIG)
        for name, raw in (("environment.before", env_before), ("nginx.before", nginx_before),
                          ("deployment-result.before.json", result_raw)):
            master.private_write(PUBLIC_CONFIG / name, raw)
        summary.update(stage="prepared", runtime_preflight=preflight, runtime_helper_sha256=helper_hash,
                       created_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                       nginx_before_sha256=hashlib.sha256(nginx_before).hexdigest(),
                       nginx_after_sha256=hashlib.sha256(nginx_after).hexdigest(),
                       environment_changes=["SUXIAOYOU_CLOUD_ALLOWED_ORIGIN", "CLOUD_NGINX_CONFIG"],
                       credentials_modified=False, certificates_modified=False, published_ports_modified=False)
        master.private_write(AUDIT, (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode())
        audit_created = True
        save = lambda: atomic_write(master, AUDIT, (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode())
        master.private_write(nginx_path, nginx_after, 0o644)
        candidate = PUBLIC_CONFIG / "candidate.compose.yaml"
        master.private_write(candidate, ("services:\n  proxy:\n    volumes:\n      - " + str(nginx_path)
                                        + ":/etc/nginx/nginx.conf:ro\n").encode())
        candidate_compose = [*master.compose(), "-f", str(candidate)]
        summary["stage"] = "candidate_validation"
        save()
        command(master, [*candidate_compose, "config", "--quiet"], timeout=60)
        command(master, [*candidate_compose, "run", "--rm", "--no-deps", "--entrypoint", "nginx", "proxy", "-t"], timeout=120)
        require(read_file(master.ENV_FILE, 16384, private=True) == env_before
                and read_file(master.CONFIG / "nginx.conf", 65536) == nginx_before)
        summary["stage"] = "environment_applying"
        save()
        atomic_write(master, master.ENV_FILE, env_after)
        summary["stage"] = "services_recreating"
        save()
        command(master, master.compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", "--wait",
                                      "--wait-timeout", "180", "api", "worker", "proxy"), timeout=240)
        summary["stage"] = "https_readiness"
        save()
        ready(master, wait=True)
        summary["stage"] = "runtime_restarting"
        save()
        restarted = helper(master, source, "restart")
        require(restarted["active_users"] == preflight["active_users"]
                and restarted["restarted_users"] == preflight["active_users"]
                and read_file(master.ENV_FILE, 16384, private=True) == env_after
                and read_file(master.CONFIG / "nginx.conf", 65536) == nginx_before
                and read_file(nginx_path, 65536) == nginx_after)
        summary.update(stage="complete", complete=True, runtime_restart=restarted,
                       internal_ip_ca_readiness=True, browser_login="pending_human_verification")
        save()
    except BaseException:
        summary.update(failed_at=summary["stage"], stage="failed", operator_review_required=True)
        if audit_created:
            try:
                atomic_write(master, AUDIT, (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode())
            except BaseException:
                summary["audit_update_failed"] = True
    finally:
        os.close(lock)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-helper-sha256", required=True)
    args = parser.parse_args()
    try:
        require(re.fullmatch(r"[0-9a-f]{64}", args.runtime_helper_sha256))
        summary = apply(args.runtime_helper_sha256)
    except BaseException:
        summary = {"complete": False, "stage": "precondition_failed", "operator_review_required": True,
                   "origin": PUBLIC, "certificate_status": "existing_ip_certificate_public_hostname_pending"}
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
