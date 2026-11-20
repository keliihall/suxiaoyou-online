#!/usr/bin/env python3
"""Install a trusted nav.cyneu.com certificate; recreate only the TLS proxy.

Certbot deploy-hook example (invoke the reviewed, protected script with Python
isolated mode): python3 -I /protected/install-public-certificate.py --fullchain
"$RENEWED_LINEAGE/fullchain.pem" --private-key "$RENEWED_LINEAGE/privkey.pem".
Run as root to read root-owned ACME files, or as suxiaoyou for private files
already delivered to that account. Root privileges are permanently dropped
before loading the pinned deployment module or invoking Docker. Caddy/acme.sh
callers can provide absolute, resolved regular-file paths. No CA is contacted.

An interrupted/failed transaction requires operator review; it is deliberately
not automatically retried or rolled back. Private backups remain on the host.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from types import ModuleType

APP = Path("/srv/suxiaoyou")
VERSION = "20261115-2144"
DOMAIN = "nav.cyneu.com"
ORIGIN = "https://nav.cyneu.com:18443"
HOST_IP = "192.168.1.211"
DEPLOY_UID = 1001
MASTER = APP / "incoming/deploy-release-pinned.py"
MASTER_SHA256 = "4fd0363428249a2de992a884f48f703d4a59ddd1e83b1ab2e84a62df67f18ad8"
SHARED = APP / "shared"
PUBLIC_CONFIG = SHARED / "config" / (VERSION + "-public")
INSTALL_ROOT = SHARED / "certificate-installs"
SYSTEM_CA = Path("/etc/ssl/certs/ca-certificates.crt")
OPENSSL = "/usr/bin/openssl"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CERT_PATTERN = re.compile(rb"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----")


class InstallFailure(Exception):
    """Never include provider data, file contents or child stderr in errors."""


def require(condition):
    if not condition:
        raise InstallFailure


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def decode(raw):
    def unique(pairs):
        value = {}
        for name, item in pairs:
            require(name not in value)
            value[name] = item
        return value
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False))


def safe_read(path, maximum, *, key=False, owners=(0, DEPLOY_UID), private=False):
    """Read through pinned, non-symlink directory descriptors, without TOCTOU."""
    path = Path(path)
    require(path.is_absolute() and all(part not in {".", ".."} for part in path.parts[1:]))
    require(len(path.parts) > 1)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    protected = False
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            mode = stat.S_IMODE(info.st_mode)
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            require(info.st_uid in owners and (not mode & 0o022 or sticky_root))
            protected = protected or not mode & 0o077
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(child, "rb") as handle:
            info = os.fstat(handle.fileno())
            mode = stat.S_IMODE(info.st_mode)
            require(stat.S_ISREG(info.st_mode) and info.st_uid in owners and info.st_nlink == 1
                    and not mode & 0o022 and 0 < info.st_size <= maximum
                    and (not private or mode == 0o600)
                    and (not key or not mode & 0o077 or protected))
            raw = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            require(len(raw) == info.st_size and info.st_size == after.st_size
                    and info.st_mtime_ns == after.st_mtime_ns and info.st_ctime_ns == after.st_ctime_ns)
            return raw
    finally:
        os.close(descriptor)


def source_bytes(path, *, key=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts)
    maximum = 64 * 1024 if key else 256 * 1024
    if not path.is_symlink():
        return safe_read(path, maximum, key=key)
    # Certbot uses a single root-owned live -> archive link. No arbitrary link
    # following is allowed, including in any directory component.
    wanted = "privkey" if key else "fullchain"
    match = re.fullmatch(r"/etc/letsencrypt/live/([A-Za-z0-9][A-Za-z0-9_.-]*)/" + wanted + r"\.pem", str(path))
    require(match is not None)
    lineage = match.group(1)
    for parent in reversed(path.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022)
    info = path.lstat()
    target = os.readlink(path)
    require(info.st_uid == 0 and stat.S_ISLNK(info.st_mode)
            and re.fullmatch(r"\.\./\.\./archive/" + re.escape(lineage) + "/" + wanted + r"[1-9][0-9]*\.pem", target))
    resolved = Path("/etc/letsencrypt/archive") / lineage / Path(target).name
    raw = safe_read(resolved, maximum, key=key, owners=(0,))
    after = path.lstat()
    require((info.st_dev, info.st_ino, info.st_mtime_ns, target)
            == (after.st_dev, after.st_ino, after.st_mtime_ns, os.readlink(path)))
    return raw


def openssl(*arguments, content=None):
    environment = {"PATH": SAFE_PATH, "LANG": "C", "LC_ALL": "C"}
    result = subprocess.run([OPENSSL, *map(str, arguments)], input=content,
                            stdin=subprocess.DEVNULL if content is None else None,
                            capture_output=True, env=environment, timeout=20, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 256 * 1024)
    return result.stdout


def write_file(path, content, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def material_identity(fullchain, key, *, ca_file=SYSTEM_CA):
    """Validate offline against system trust, never expose OpenSSL errors."""
    certificates = CERT_PATTERN.findall(fullchain)
    require(1 <= len(certificates) <= 8 and not CERT_PATTERN.sub(b"", fullchain).strip())
    require(0 < len(key) <= 64 * 1024)
    with tempfile.TemporaryDirectory(prefix="suxiaoyou-cert-validation-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        leaf, intermediates, key_file = directory / "leaf.pem", directory / "chain.pem", directory / "key.pem"
        write_file(leaf, certificates[0] + b"\n")
        write_file(key_file, key)
        verify = ["verify", "-CAfile", ca_file, "-no-CApath", "-no-CAstore", "-purpose", "sslserver",
                  "-verify_hostname", DOMAIN]
        if len(certificates) > 1:
            write_file(intermediates, b"\n".join(certificates[1:]) + b"\n")
            verify += ["-untrusted", intermediates]
        openssl(*verify, leaf)
        san = openssl("x509", "-in", leaf, "-noout", "-ext", "subjectAltName").decode("ascii")
        dns_names = re.findall(r"DNS:([^,\s]+)", san)
        require(DOMAIN in dns_names or "*.cyneu.com" in dns_names)
        openssl("x509", "-in", leaf, "-checkend", "604800", "-noout")
        cert_public = openssl("x509", "-in", leaf, "-pubkey", "-noout")
        key_public = openssl("pkey", "-in", key_file, "-passin", "pass:", "-pubout")
        cert_der = openssl("pkey", "-pubin", "-outform", "DER", content=cert_public)
        key_der = openssl("pkey", "-pubin", "-outform", "DER", content=key_public)
        require(cert_der == key_der)
        leaf_der = openssl("x509", "-in", leaf, "-outform", "DER")
        expiry = openssl("x509", "-in", leaf, "-noout", "-enddate").decode("ascii").strip()
        require(expiry.startswith("notAfter=") and len(expiry) <= 80)
        return {"sha256_fingerprint": hashlib.sha256(leaf_der).hexdigest(), "not_after": expiry[9:]}


def directory(path, *, strict=True):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == DEPLOY_UID
            and not stat.S_IMODE(info.st_mode) & 0o022
            and (not strict or stat.S_IMODE(info.st_mode) == 0o700))


def deployment_module():
    raw = safe_read(MASTER, 128 * 1024, owners=(DEPLOY_UID,))
    require(hashlib.sha256(raw).hexdigest() == MASTER_SHA256)
    module = ModuleType("suxiaoyou_certificate_frozen_master")
    module.__file__ = str(MASTER)
    exec(compile(raw, str(MASTER), "exec"), module.__dict__)
    require(module.APP == APP and module.VERSION == VERSION and module.DEPLOY_UID == DEPLOY_UID
            and module.PROJECT == "suxiaoyou-cloud" and module.TLS == SHARED / "tls"
            and module.CONFIG == SHARED / "config" / VERSION
            and module.RELEASE == APP / "releases" / VERSION
            and module.SOCKET == Path("/run/user/1001/docker.sock"))
    return module


def atomic_write(path, content, mode=0o600):
    temporary = path.with_name("." + path.name + ".certificate-" + secrets.token_hex(8))
    try:
        write_file(temporary, content, mode)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def compose_command(master, arguments, *, timeout=180):
    environment = master.command_environment()
    environment.update(PATH=SAFE_PATH, LANG="C", LC_ALL="C")
    result = subprocess.run(arguments, cwd=master.RELEASE, env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=timeout, check=False)
    # Neither subprocess output nor environment is printed or persisted: an
    # invalid private key must not leak a provider's diagnostic into logs.
    require(result.returncode == 0)


def ready(identity, *, wait=False):
    context = ssl.create_default_context(cafile=str(SYSTEM_CA))
    deadline = time.monotonic() + (90 if wait else 0)
    while True:
        connection = None
        try:
            raw_socket = socket.create_connection((HOST_IP, 8443), timeout=5)
            try:
                tls_socket = context.wrap_socket(raw_socket, server_hostname=DOMAIN)
            except BaseException:
                raw_socket.close()
                raise
            connection = http.client.HTTPConnection(HOST_IP, 8443, timeout=5)
            connection.sock = tls_socket
            require(hashlib.sha256(tls_socket.getpeercert(binary_form=True)).hexdigest()
                    == identity["sha256_fingerprint"])
            connection.request("GET", "/readyz", headers={"Host": DOMAIN + ":18443", "Connection": "close"})
            response = connection.getresponse()
            raw = response.read(4097)
            require(response.status == 200 and len(raw) <= 4096)
            value = decode(raw)
            require(value.get("status") == "ready" and value.get("mode") == "cloud")
            return
        except (OSError, ValueError, InstallFailure, http.client.HTTPException):
            if time.monotonic() >= deadline:
                raise InstallFailure from None
            time.sleep(2)
        finally:
            if connection is not None:
                connection.close()


def state(master):
    for path in (APP, APP / "incoming"):
        directory(path, strict=False)
    for path in (SHARED, SHARED / "config", master.CONFIG, master.RELEASE, master.TLS, PUBLIC_CONFIG):
        directory(path)
    require((APP / "current").is_symlink() and (APP / "current").resolve() == master.RELEASE)
    read = lambda path, limit=65536, private=True: safe_read(path, limit, owners=(DEPLOY_UID,), private=private)
    result = decode(read(master.CONFIG / "deployment-result.json"))
    require(result.get("version") == VERSION and result.get("ready") is True
            and result.get("source_archive_sha256") == master.EXPECTED_SHA256
            and result.get("base_image_evidence_sha256") == master.EXPECTED_BASE_METADATA_SHA256)
    public = decode(read(PUBLIC_CONFIG / "public-entry-result.json"))
    require(public.get("complete") is True and public.get("stage") == "complete"
            and public.get("origin") == ORIGIN and public.get("version") == VERSION)
    environment = read(master.ENV_FILE, 16384)
    values = {}
    for line in environment.decode().splitlines():
        require("=" in line)
        name, value = line.split("=", 1)
        require(name not in values)
        values[name] = value
    require(values.get("SUXIAOYOU_CLOUD_ALLOWED_ORIGIN") == ORIGIN
            and values.get("CLOUD_NGINX_CONFIG") == str(PUBLIC_CONFIG / "nginx.conf")
            and values.get("SUXIAOYOU_CLOUD_ENVIRONMENT") == "production"
            and values.get("SUXIAOYOU_CLOUD_COOKIE_SECURE") == "true"
            and values.get("SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER") == "false"
            and values.get("CLOUD_DOCKER_SOCKET") == str(master.SOCKET)
            and values.get("CLOUD_DOCKER_GID") == "983")
    nginx = read(PUBLIC_CONFIG / "nginx.conf", private=False)
    require(nginx.count(b"ssl_certificate /etc/nginx/tls/cert.pem;") == 1
            and nginx.count(b"ssl_certificate_key /etc/nginx/tls/key.pem;") == 1
            and nginx.count(b"server_name nav.cyneu.com;") == 1
            and hashlib.sha256(nginx).hexdigest() == public.get("nginx_after_sha256"))
    return environment, nginx


def install(fullchain_path, private_key_path):
    account = pwd.getpwnam("suxiaoyou")
    require(os.getuid() == os.geteuid() and os.getuid() in {0, DEPLOY_UID}
            and account.pw_uid == DEPLOY_UID and account.pw_gid == DEPLOY_UID)
    os.umask(0o077)
    fullchain = source_bytes(fullchain_path)
    key = source_bytes(private_key_path, key=True)
    identity = material_identity(fullchain, key)
    if os.getuid() == 0:
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    require(os.getuid() == os.geteuid() == DEPLOY_UID and os.getgid() == DEPLOY_UID)
    os.environ["HOME"] = account.pw_dir
    os.chdir(account.pw_dir)
    master = deployment_module()
    directory(SHARED)
    lock = os.open(SHARED / "deploy.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    transaction = None
    summary = {"complete": False, "status": "review_required", "domain": DOMAIN,
               "version": VERSION, "changed": False, **identity}
    journal = None
    try:
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == DEPLOY_UID and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        baseline = state(master)
        if not INSTALL_ROOT.exists():
            INSTALL_ROOT.mkdir(mode=0o700)
        directory(INSTALL_ROOT)
        journal = INSTALL_ROOT / "latest.json"
        if journal.exists() or journal.is_symlink():
            previous = decode(safe_read(journal, 65536, owners=(DEPLOY_UID,), private=True))
            require(previous.get("complete") is True and previous.get("status") == "installed")
        old_cert = safe_read(master.TLS / "cert.pem", 256 * 1024, owners=(DEPLOY_UID,))
        old_key = safe_read(master.TLS / "key.pem", 64 * 1024, key=True, owners=(DEPLOY_UID,))
        if fullchain == old_cert and key == old_key:
            ready(identity)
            summary.update(complete=True, status="unchanged")
            # A manually delivered, already-active certificate still receives
            # a verified receipt. Keep the persistent success status identical
            # to an installation so repeated hooks remain idempotent.
            receipt = {**summary, "status": "installed", "stage": "complete",
                       "verified_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            atomic_write(journal, json_bytes(receipt))
            return summary
        identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6)
        transaction = INSTALL_ROOT / identifier
        transaction.mkdir(mode=0o700)
        directory(transaction)
        # Backup before any file replacement; old material is never overwritten.
        write_file(transaction / "certificate.before.pem", old_cert)
        write_file(transaction / "private-key.before.pem", old_key)
        write_file(transaction / "certificate.candidate.pem", fullchain, 0o444)
        write_file(transaction / "private-key.candidate.pem", key, 0o444)
        candidate = transaction / "candidate.compose.yaml"
        write_file(candidate, ("services:\n  proxy:\n    volumes:\n      - "
                              + str(transaction / "certificate.candidate.pem") + ":/etc/nginx/tls/cert.pem:ro\n      - "
                              + str(transaction / "private-key.candidate.pem") + ":/etc/nginx/tls/key.pem:ro\n").encode())
        summary.update(transaction=identifier, stage="candidate_validation",
                       installed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        atomic_write(journal, json_bytes(summary))
        atomic_write(transaction / "result.json", json_bytes(summary))
        candidate_compose = [*master.compose(), "-f", str(candidate)]
        compose_command(master, [*candidate_compose, "config", "--quiet"], timeout=60)
        compose_command(master, [*candidate_compose, "run", "--rm", "--no-deps", "--pull", "never",
                                 "--entrypoint", "nginx", "proxy", "-t"], timeout=120)
        require(state(master) == baseline
                and safe_read(master.TLS / "cert.pem", 256 * 1024, owners=(DEPLOY_UID,)) == old_cert
                and safe_read(master.TLS / "key.pem", 64 * 1024, key=True, owners=(DEPLOY_UID,)) == old_key)
        summary["stage"] = "installing"
        atomic_write(journal, json_bytes(summary))
        atomic_write(transaction / "result.json", json_bytes(summary))
        atomic_write(master.TLS / "cert.pem", fullchain, 0o444)
        atomic_write(master.TLS / "key.pem", key, 0o444)
        summary.update(stage="proxy_recreating", changed=True)
        atomic_write(journal, json_bytes(summary))
        atomic_write(transaction / "result.json", json_bytes(summary))
        compose_command(master, master.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                                               "--force-recreate", "--wait", "--wait-timeout", "90", "proxy"), timeout=150)
        ready(identity, wait=True)
        require(state(master) == baseline
                and safe_read(master.TLS / "cert.pem", 256 * 1024, owners=(DEPLOY_UID,)) == fullchain
                and safe_read(master.TLS / "key.pem", 64 * 1024, key=True, owners=(DEPLOY_UID,)) == key)
        summary.update(complete=True, status="installed", stage="complete")
        atomic_write(transaction / "result.json", json_bytes(summary))
        atomic_write(journal, json_bytes(summary))
        return summary
    except BaseException:
        summary.update(complete=False, status="review_required", operator_review_required=True)
        if transaction is not None:
            try:
                atomic_write(transaction / "result.json", json_bytes(summary))
                atomic_write(journal, json_bytes(summary))
            except BaseException:
                pass
        return summary
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fullchain", required=True)
    parser.add_argument("--private-key", required=True)
    args = parser.parse_args()
    try:
        result = install(args.fullchain, args.private_key)
    except BaseException:
        result = {"complete": False, "status": "review_required", "operator_review_required": True,
                  "domain": DOMAIN, "version": VERSION}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
