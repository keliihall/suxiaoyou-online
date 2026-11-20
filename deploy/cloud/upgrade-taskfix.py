#!/usr/bin/env python3
"""Build, explicitly activate, or explicitly roll back the fixed task repair.

Run as suxiaoyou (UID 1001), with an already reviewed source package and helper
in /srv/suxiaoyou/incoming. Build never changes serving containers. Activation
requires idle tenants and closes proxy ingress before a second idle check and
private backups. No migration, seeding, database restore, cancellation, model
request or automatic retry is performed. A failure leaves its durable phase
and requires review. Rollback is an explicit command using the original volumes
and current databases; it never restores potentially stale database backups.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import time
from types import ModuleType
import urllib.request

APP = Path("/srv/suxiaoyou")
SHARED = APP / "shared"
OLD_VERSION = "20261115-2144"
VERSION = "20261116-0205-taskfix"
OLD_RELEASE = APP / "releases" / OLD_VERSION
RELEASE = APP / "releases" / VERSION
OLD_CONFIG = SHARED / "config" / OLD_VERSION
CONFIG = SHARED / "config" / VERSION
SOURCE = APP / "incoming/taskfix-20261116.tar.gz"
SOURCE_SHA256 = "1d6d93d9ebbb47b09f4b22b19cc422f0c1133eae6e889f3772cd611480fa67b6"
MASTER = APP / "incoming/deploy-release-pinned.py"
MASTER_SHA256 = "4fd0363428249a2de992a884f48f703d4a59ddd1e83b1ab2e84a62df67f18ad8"
PUBLIC_HELPER = APP / "incoming/public-origin-runtime.py"
PUBLIC_HELPER_SHA256 = "d314324ace60022b2fd9a4a8509d53baee676432bb998bb1fd0791f0de402fcd"
HELPER = APP / "incoming/taskfix-runtime.py"
HELPER_SHA256 = "dfe4a958be00d1bec18b4f7fce836e67bb1f632222a575a56cdd2e733f6b4210"
ENV = SHARED / ".env.cloud"
STATE = CONFIG / "upgrade-state.json"
LOG = SHARED / "logs" / (VERSION + ".log")
PROJECT = "suxiaoyou-cloud"
SOCKET = "/run/user/1001/docker.sock"
ORIGIN = "https://192.168.1.211:8443"
OLD_WEB_SHA256 = "24e7e322c003137dd33bf8f846a8cb2c83cd279f003e9aa9ad18ac2b38629316"
OLD_OVERRIDE_SHA256 = "eb97b91b06b48bf0868b661b5c831e3c4e6a590992ade8796d4d9b0124069294"
OLD_IMAGES = {
    "api": "suxiaoyou-cloud-api:" + OLD_VERSION,
    "web": "suxiaoyou-cloud-web:" + OLD_VERSION,
    "runtime": "suxiaoyou-online-runtime:" + OLD_VERSION,
}
IMAGES = {name: tag.replace(OLD_VERSION, VERSION) for name, tag in OLD_IMAGES.items()}
CACHE_TAG = "suxiaoyou-npm-cache-complete:" + OLD_VERSION
NODE_TAG = "suxiaoyou-base-node:20261115-amd64"
BASE_IDS = {
    OLD_IMAGES["api"]: "sha256:6c651b9a371b3a52aa69cd9f8113afd74f0182c59be5244f6e633ec24130dc2c",
    OLD_IMAGES["web"]: "sha256:996626ac05e2ff8728780dd87acd619778d727c90c4db4d4083acda8d9129396",
    OLD_IMAGES["runtime"]: "sha256:932d2552c60065f4ca6a3bd198caba4f41743968eb8c371d788c2b5ceea12a5a",
    CACHE_TAG: "sha256:51fe5c916aafefc26dfce5508b14ae943b866f9eb4d6d3c22386d88a26870e67",
    NODE_TAG: "sha256:f9c30b69fe318cb92754ebf62d853c24bf77a702611fa3ae664fbd4144af8722",
}
SHA = re.compile(r"[0-9a-f]{64}\Z")
SERVICES = ("db", "api", "worker", "web", "proxy")


class Failure(Exception):
    def __init__(self, code="review_required"):
        self.code = code
        super().__init__(code)


def require(condition, code="precondition_failed"):
    if not condition:
        raise Failure(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value)
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False))


def read(path, maximum=1024 * 1024, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1
                and not stat.S_IMODE(info.st_mode) & 0o022 and 0 <= info.st_size <= maximum
                and (not private or stat.S_IMODE(info.st_mode) == 0o600), "unsafe_input_file")
        raw = handle.read(maximum + 1)
    require(len(raw) == info.st_size, "input_file_changed")
    return raw


def directory(path, *, strict=True):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 1001
            and not stat.S_IMODE(info.st_mode) & 0o022
            and (not strict or stat.S_IMODE(info.st_mode) == 0o700), "unsafe_directory")


def write(path, raw, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def atomic(path, raw):
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    try:
        write(temporary, raw)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def save(state, phase=None):
    if phase is not None:
        state["phase"] = phase
    state["updated_at_utc"] = now()
    atomic(STATE, (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def environment():
    account = pwd.getpwnam("suxiaoyou")
    require(os.getuid() == os.geteuid() == account.pw_uid == 1001
            and os.getgid() == account.pw_gid == 1001, "dedicated_deployment_user_required")
    result = {key: value for key, value in os.environ.items() if not key.startswith(
        ("COMPOSE_", "SUXIAOYOU_", "NEXT_PUBLIC_", "DOCKER_")) and key not in {
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}}
    result.update(HOME=account.pw_dir, PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                  DOCKER_HOST="unix://" + SOCKET, XDG_RUNTIME_DIR="/run/user/1001",
                  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1001/bus", NO_PROXY="127.0.0.1,localhost,192.168.1.211")
    return result


def command(arguments, *, content=None, capture=True, timeout=180, output=None):
    # Captured inspect/helper output can contain process-only secrets. It is
    # never copied into a log or public stdout. No shell is invoked.
    with LOG.open("ab") as log:
        result = subprocess.run(arguments, cwd=APP, env=environment(), input=content,
                                stdin=subprocess.DEVNULL if content is None else None,
                                stdout=output if output is not None else (subprocess.PIPE if capture else log),
                                stderr=log, timeout=timeout, check=False)
    require(result.returncode == 0, "operation_failed_review_private_log")
    return result.stdout if capture and output is None else b""


def compose(*arguments, old=False):
    release, config = (OLD_RELEASE, OLD_CONFIG) if old else (RELEASE, CONFIG)
    return ["docker", "compose", "--project-name", PROJECT, "--env-file", str(ENV),
            "-f", str(release / "compose.cloud.yaml"), "-f", str(config / "compose.override.yaml"), *arguments]


def pinned_master():
    raw = read(MASTER, 128 * 1024)
    require(digest(raw) == MASTER_SHA256, "master_identity_mismatch")
    module = ModuleType("suxiaoyou_taskfix_package_validator")
    module.__file__ = str(MASTER)
    exec(compile(raw, str(MASTER), "exec"), module.__dict__)
    require(module.APP == APP and module.VERSION == OLD_VERSION and module.DEPLOY_UID == 1001
            and module.RELEASE == OLD_RELEASE and module.PROJECT == PROJECT)
    return module


def fixed_inputs():
    raw = read(ENV, 16384, private=True)
    values = {}
    for line in raw.decode().splitlines():
        require("=" in line)
        key, value = line.split("=", 1)
        require(key not in values)
        values[key] = value
    require(values.get("SUXIAOYOU_CLOUD_ENVIRONMENT") == "production"
            and values.get("SUXIAOYOU_CLOUD_COOKIE_SECURE") == "true"
            and values.get("SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER") == "false"
            and values.get("CLOUD_DOCKER_SOCKET") == SOCKET and values.get("CLOUD_DOCKER_GID") == "983")
    origin = values.get("SUXIAOYOU_CLOUD_ALLOWED_ORIGIN")
    require(origin in {ORIGIN, "https://nav.cyneu.com:18443"})
    nginx = Path(values.get("CLOUD_NGINX_CONFIG", ""))
    require(nginx.is_absolute() and nginx.parent in {OLD_CONFIG, SHARED / "config" / (OLD_VERSION + "-public")})
    directory(nginx.parent)
    key = (SHARED / "tls/key.pem").lstat()
    require(stat.S_ISREG(key.st_mode) and key.st_uid == 1001 and key.st_nlink == 1
            and stat.S_IMODE(key.st_mode) == 0o444)
    return {"environment_sha256": digest(raw), "nginx_path": str(nginx),
            "nginx_sha256": digest(read(nginx, 65536)), "origin": origin,
            "certificate_sha256": digest(read(SHARED / "tls/cert.pem", 65536)),
            "key_identity": [key.st_dev, key.st_ino, key.st_size, key.st_mtime_ns],
            "old_override_sha256": digest(read(OLD_CONFIG / "compose.override.yaml", 65536, private=True))}


def image(tag):
    info = decode(command(["docker", "image", "inspect", tag]))[0]
    require(info.get("Architecture") == "amd64" and info.get("Os") == "linux"
            and tag in info.get("RepoTags", []) and re.fullmatch(r"sha256:[0-9a-f]{64}", info.get("Id", "")))
    return info


def base_images():
    result = {}
    for tag, expected in BASE_IDS.items():
        info = image(tag)
        require(info["Id"] == expected, "base_image_identity_changed")
        result[tag] = info
    recipe = decode(read(OLD_CONFIG / "build-recipe.json", private=True))
    evidence = [row for row in recipe["offline_resume"]["verified_local_images"] if row["local_tag"] == NODE_TAG]
    require(len(evidence) == 1 and evidence[0]["raw_saved_config_sha256_verified"] is True
            and evidence[0]["inspect_image_id"] == BASE_IDS[NODE_TAG]
            and result[NODE_TAG]["RootFS"]["Layers"] == evidence[0]["all_rootfs_diff_ids"])
    receipt = recipe["npm_offline_continuation"]["cache_receipt"]
    node, cache = result[NODE_TAG], result[CACHE_TAG]
    layers = node["RootFS"]["Layers"]
    require(receipt["image_tag"] == CACHE_TAG and receipt["image_id"] == BASE_IDS[CACHE_TAG]
            and receipt["node_base_image_id"] == BASE_IDS[NODE_TAG]
            and cache["Config"] == node["Config"]
            and cache["RootFS"]["Layers"][:len(layers)] == layers
            and len(cache["RootFS"]["Layers"]) == len(layers) + 1)
    require(re.search(rb"(?m)^Driver:\s+docker\s*$", command(["docker", "buildx", "inspect"])) is not None)
    return result


def service(service):
    # Select public operational fields at the daemon, so Config.Env is never
    # even included in this captured document.
    template = '{"id":{{json .Id}},"image_id":{{json .Image}},"image":{{json .Config.Image}},"state":{{json .State}},"mounts":{{json .Mounts}}}'
    raw = command(["docker", "container", "inspect", "--format", template, PROJECT + "-" + service + "-1"])
    value = decode(raw)
    require(re.fullmatch(r"[0-9a-f]{64}", value.get("id", "")))
    # Healthcheck log timestamps change while an unaffected container serves.
    # Persist identity/lifecycle/health, never compare volatile probe history.
    lifecycle = value["state"]
    value["state"] = {name: lifecycle.get(name) for name in
                      ("Status", "Running", "OOMKilled", "ExitCode", "StartedAt")}
    if "Health" in lifecycle:
        value["state"]["Health"] = {"Status": lifecycle["Health"].get("Status")}
    return value


def services():
    return {name: service(name) for name in SERVICES}


def health(inventory):
    for name, row in inventory.items():
        require(row["state"].get("Running") is True, "service_not_running")
        if name in {"db", "api", "web"}:
            require(row["state"].get("Health", {}).get("Status") == "healthy", "service_not_healthy")


def no_volume_change(before, after):
    for name in SERVICES:
        require(before[name]["mounts"] == after[name]["mounts"], "service_mounts_changed")
    require(before["db"]["id"] == after["db"]["id"], "database_container_changed")


def verify_source(master, sha):
    manifest, files = master.validate_package(SOURCE, sha)
    old_manifest, old_files = master.validate_package(APP / "incoming/release.tar.gz", master.EXPECTED_SHA256)
    old, new = {n: b for n, b, _ in old_files}, {n: b for n, b, _ in files}
    for name, raw, _mode in old_files:
        require(read(OLD_RELEASE / name, max(1, len(raw))) == raw, "old_release_source_changed")
    protected = lambda name: (name == "compose.cloud.yaml" or name == "frontend/package.json"
        or name == "frontend/package-lock.json" or name.startswith(("backend/alembic", "backend/cloud_alembic",
        "backend/release_packaging/", "backend/requirements")) or name in {"backend/pyproject.toml", "backend/uv.lock"})
    require(all(name in new and new[name] == old[name] for name in old if protected(name))
            and all(name in old for name in new if protected(name)), "dependencies_or_schema_changed")
    require(all(name in new for name in old if name.startswith("backend/app/")), "overlay_cannot_remove_source")
    changes = sorted(name for name in set(old) | set(new)
                     if name != "RELEASE-MANIFEST.json" and old.get(name) != new.get(name))
    require(all(name.startswith(("backend/app/", "frontend/src/")) for name in changes), "unexpected_source_change")
    require(manifest["source_sha256"] != old_manifest["source_sha256"])
    return manifest, files, changes


def checked_release(files):
    for name, raw, _mode in files:
        require(read(RELEASE / name, max(1, len(raw))) == raw, "staged_source_changed")


def extract(files):
    require(not RELEASE.exists() and not RELEASE.is_symlink(), "release_already_exists")
    RELEASE.mkdir(mode=0o700)
    for name, raw, mode in files:
        parent = RELEASE
        for part in PurePosixPath(name).parts[:-1]:
            parent /= part
            if not parent.exists():
                parent.mkdir(mode=0o755)
                parent.chmod(0o755)
        write(RELEASE / name, raw, mode)


def helper_bytes():
    reference, helper = read(PUBLIC_HELPER, 128 * 1024), read(HELPER, 128 * 1024)
    require(digest(reference) == PUBLIC_HELPER_SHA256 and digest(helper) == HELPER_SHA256,
            "runtime_helper_identity_changed")
    return reference, helper


def runtime(mode, state, *, old=False, baseline=None, rollback=False):
    reference, helper = helper_bytes()
    api = service("api")
    tags = OLD_IMAGES if old else IMAGES
    require(api["image"] == tags["api"], "wrong_active_api_image")
    expected_id = BASE_IDS[OLD_IMAGES["api"]] if old else state["built_images"]["api"]
    require(api["image_id"] == expected_id, "active_api_identity_changed")
    payload = {"mode": mode, "operation_id": VERSION, "origin": state["fixed_inputs"]["origin"],
               "api_image_id": api["image_id"], "runtime_image": tags["runtime"],
               "images": {OLD_IMAGES["runtime"]: BASE_IDS[OLD_IMAGES["runtime"]],
                          IMAGES["runtime"]: state["built_images"]["runtime"]},
               "baseline": baseline, "rollback": rollback}
    program = b"PUBLIC_SOURCE = " + repr(reference).encode() + b"\nimport json\nREQUEST = json.loads(" + repr(json.dumps(payload)).encode() + b")\n" + helper
    result = decode(command(compose("exec", "-T", "api", "python", "-", old=old), content=program, timeout=1200))
    require(result.get("complete") is True and result.get("ready") is True
            and result.get("mode") == mode and result.get("operation_id") == VERSION
            and isinstance(result.get("snapshot"), dict), "runtime_operation_failed")
    return result["snapshot"]


def https_ready():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args):
            return None
    context = ssl.create_default_context(cafile=str(SHARED / "tls/cert.pem"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=context), NoRedirect())
    for path in ("/readyz", "/cloud"):
        with opener.open(ORIGIN + path, timeout=10) as response:
            raw = response.read(1024 * 1024 + 1)
            require(response.status == 200 and response.geturl() == ORIGIN + path and len(raw) <= 1024 * 1024,
                    "https_readiness_failed")
        if path == "/readyz":
            require(decode(raw).get("status") == "ready" and decode(raw).get("mode") == "cloud")


def internal_ready(*, old=False):
    probe = b'''import json, urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for url in ("http://127.0.0.1:8000/readyz", "http://web:3000/cloud"):
 with opener.open(url, timeout=10) as response:
  raw=response.read(1024*1024+1)
  if response.status!=200 or len(raw)>1024*1024: raise SystemExit(1)
  if url.endswith("/readyz") and json.loads(raw).get("status")!="ready": raise SystemExit(1)
print("READY")
'''
    require(command(compose("exec", "-T", "api", "python", "-", old=old), content=probe).strip() == b"READY")


def backup(state, baseline):
    target = CONFIG / "backups"
    require(not target.exists() and not target.is_symlink(), "backup_directory_already_exists")
    target.mkdir(mode=0o700)
    backups = []
    def output_file(path, arguments):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            command(arguments, output=output, capture=False, timeout=300)
            output.flush()
            os.fsync(output.fileno())
        require(0 < path.stat().st_size <= 1024 ** 3, "backup_size_invalid")
        fingerprint = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                fingerprint.update(block)
        backups.append({"file": path.name, "bytes": path.stat().st_size, "sha256": fingerprint.hexdigest()})
    pg = target / "cloud-postgres.dump"
    output_file(pg, compose("exec", "-T", "db", "pg_dump", "--username=suxiaoyou", "--dbname=suxiaoyou_cloud",
                            "--format=custom", "--no-owner", "--no-acl", "--no-password", old=True))
    with pg.open("rb") as source:
        require(source.read(5) == b"PGDMP", "invalid_postgres_backup")
    sqlite_program = '''import sqlite3,sys
source=sqlite3.connect("file:/tenant/state/data/suxiaoyou.db?mode=ro",uri=True)
if source.execute("PRAGMA page_count").fetchone()[0]*source.execute("PRAGMA page_size").fetchone()[0]>256*1024*1024: raise SystemExit(1)
target=sqlite3.connect(":memory:")
source.backup(target)
if target.execute("PRAGMA quick_check").fetchall()!=[("ok",)]: raise SystemExit(1)
sys.stdout.buffer.write(target.serialize())
target.close(); source.close()
'''
    for item in baseline["tenants"]:
        if item["state"] != "running":
            continue
        owner = item["owner_id"]
        require(SHA.fullmatch(owner))
        name = "sxy-user-" + owner[:32]
        path = target / (owner + ".sqlite3")
        output_file(path, ["docker", "exec", "--user", "10001:10001", name, "python", "-c", sqlite_program])
        with path.open("rb") as source:
            require(source.read(16) == b"SQLite format 3\0", "invalid_tenant_backup")
    state["backups"] = backups
    save(state, "backups_complete")


def switch_current(release):
    temporary = APP / (".current-taskfix-" + secrets.token_hex(8))
    os.symlink(release, temporary)
    os.replace(temporary, APP / "current")
    descriptor = os.open(APP, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build(master, sha):
    require((APP / "current").is_symlink() and (APP / "current").resolve() == OLD_RELEASE,
            "old_release_not_current")
    require(not CONFIG.exists() and not CONFIG.is_symlink(), "version_already_staged")
    preserved = fixed_inputs()
    require(preserved["old_override_sha256"] == OLD_OVERRIDE_SHA256)
    old = services()
    health(old)
    https_ready()
    bases = base_images()
    for name, tag in OLD_IMAGES.items():
        if name != "runtime":
            require(old[name]["image"] == tag and old[name]["image_id"] == BASE_IDS[tag])
    require(old["worker"]["image"] == OLD_IMAGES["api"])
    manifest, files, changes = verify_source(master, sha)
    require(shutil.disk_usage(APP).free >= 10 * 1024 ** 3, "insufficient_build_disk")
    for tag in IMAGES.values():
        require(not command(["docker", "image", "ls", "--filter", "reference=" + tag, "--format", "{{.ID}}"]),
                "new_image_tag_already_exists")
    CONFIG.mkdir(mode=0o700)
    state = {"format_version": 1, "version": VERSION, "old_version": OLD_VERSION, "phase": "preparing",
             "created_at_utc": now(), "source_archive_sha256": sha, "source_sha256": manifest["source_sha256"],
             "fixed_inputs": preserved, "original_services": old, "source_changes": changes,
             "schema_changes": False, "dependencies_changed": False, "credentials_changed": False,
             "master_sha256": MASTER_SHA256, "runtime_helper_sha256": HELPER_SHA256,
             "controller_sha256": digest(read(Path(__file__), 128 * 1024)), "automatic_rollback": False}
    save(state)
    try:
        helper_bytes()
        extract(files)
        directory(CONFIG)
        build_dir = CONFIG / "build"
        build_dir.mkdir(mode=0o700)
        dockerfiles = {}
        for name, destination in (("api", "/app/app"), ("runtime", "/opt/backend/app")):
            dockerfiles[name] = ("FROM " + OLD_IMAGES[name] + "\nCOPY --chown=10001:10001 backend/app " + destination + "\n").encode()
        web = read(OLD_CONFIG / "build/Dockerfile.web", 65536)
        require(digest(web) == OLD_WEB_SHA256 and b"--offline" in web)
        dockerfiles["web"] = web
        for name, raw in dockerfiles.items():
            path = build_dir / ("Dockerfile." + name)
            write(path, raw)
            write(path.with_name(path.name + ".dockerignore"), read(OLD_CONFIG / "build" / (path.name + ".dockerignore"), 65536))
        override = read(OLD_CONFIG / "compose.override.yaml", 65536, private=True).decode()
        for name, old_tag in OLD_IMAGES.items():
            require(override.count(old_tag) >= 1)
            override = override.replace(old_tag, IMAGES[name])
        override = override.replace(str(OLD_CONFIG / "build"), str(CONFIG / "build"))
        require(OLD_VERSION not in override and "migrate:" in override)
        write(CONFIG / "compose.override.yaml", override.encode())
        state["build_recipes"] = {name: digest(raw) for name, raw in dockerfiles.items()}
        save(state, "building")
        command(compose("config", "--quiet"), timeout=60)
        state["built_images"] = {}
        for name in ("runtime", "api", "web"):
            for tag, expected in BASE_IDS.items():
                require(image(tag)["Id"] == expected, "build_base_changed")
            command(["docker", "build", "--platform", "linux/amd64", "--network", "none", "--pull=false",
                     "--file", str(build_dir / ("Dockerfile." + name)), "--tag", IMAGES[name],
                     "--build-arg", "NEXT_PUBLIC_API_URL=http://api:8000", str(RELEASE)], capture=False, timeout=1800)
            info = image(IMAGES[name])
            if name != "web":
                original = bases[OLD_IMAGES[name]]
                layers = original["RootFS"]["Layers"]
                require(info["Config"] == original["Config"] and info["RootFS"]["Layers"][:len(layers)] == layers
                        and len(info["RootFS"]["Layers"]) == len(layers) + 1, "overlay_image_contract_changed")
            state["built_images"][name] = info["Id"]
            save(state)
        require(fixed_inputs() == preserved and services() == old, "build_changed_serving_state")
        checked_release(files)
        save(state, "built")
        return state
    except BaseException:
        state["failed_phase"] = state["phase"]
        save(state, "failed")
        raise


def activate(master, sha, *, rollback=False):
    state = decode(read(STATE, private=True))
    require(state.get("version") == VERSION and state.get("source_archive_sha256") == sha
            and state.get("controller_sha256") == digest(read(Path(__file__), 128 * 1024))
            and state.get("runtime_helper_sha256") == HELPER_SHA256)
    require(state["phase"] in ({"active", "failed"} if rollback else {"built"}), "phase_requires_manual_review")
    require((APP / "current").is_symlink() and (APP / "current").resolve()
            in ({OLD_RELEASE, RELEASE} if rollback else {OLD_RELEASE}))
    require(fixed_inputs() == state["fixed_inputs"], "fixed_inputs_changed")
    _manifest, files, _changes = verify_source(master, sha)
    checked_release(files)
    helper_bytes()
    for name, tag in IMAGES.items():
        require(image(tag)["Id"] == state["built_images"][name], "built_image_identity_changed")
    for tag, expected in BASE_IDS.items():
        require(image(tag)["Id"] == expected, "original_image_identity_changed")
    if rollback:
        require("before" in state, "rollback_without_activation_snapshot")
    try:
        if not rollback:
            before = runtime("snapshot", state, old=True)
            state["before"] = before
            save(state, "idle_preflight_complete")
        save(state, "rollback_closing_ingress" if rollback else "closing_ingress")
        command(compose("stop", "--timeout", "30", "proxy", old=rollback), timeout=60)
        require(service("proxy")["state"].get("Running") is False, "proxy_did_not_stop")
        if not rollback:
            second = runtime("snapshot", state, old=True)
            require(second == state["before"], "idle_state_changed_after_ingress_closed")
            save(state, "ingress_closed_idle_confirmed")
            backup(state, second)
        else:
            # Explicit rollback rechecks idle state before replacing anything.
            # The helper accepts only the originally running users as targets.
            current_api = service("api")
            use_old = current_api["image"] == OLD_IMAGES["api"]
            state["rollback_before"] = runtime("snapshot", state, old=use_old,
                                               baseline=state["before"], rollback=True)
            require(state["rollback_before"]["billing"] == state["before"]["billing"]
                    and state["rollback_before"]["web_auth_sessions"] == state["before"]["web_auth_sessions"],
                    "rollback_billing_or_sessions_changed")
        save(state, "rollback_services" if rollback else "services_switching")
        command(compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "--wait",
                        "--wait-timeout", "180", "api", "worker", "web", old=rollback), timeout=240)
        current = services()
        no_volume_change(state["original_services"], current)
        for name in ("api", "web"):
            require(current[name]["image"] == (OLD_IMAGES if rollback else IMAGES)[name])
        save(state, "rollback_tenants" if rollback else "tenants_switching")
        # Rollback must select the original running inventory, including any
        # tenant left failed by a partial forward refresh.
        baseline = state["before"]
        refreshed = runtime("refresh", state, old=rollback, baseline=baseline, rollback=rollback)
        state["rollback_after" if rollback else "after"] = refreshed
        save(state, "rollback_verifying" if rollback else "verifying")
        verified = runtime("verify", state, old=rollback, baseline=refreshed, rollback=rollback)
        require(verified["billing"] == state["before"]["billing"]
                and verified["web_auth_sessions"] == state["before"]["web_auth_sessions"])
        internal_ready(old=rollback)
        command(compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "proxy", old=rollback), timeout=120)
        health(services())
        no_volume_change(state["original_services"], services())
        https_ready()
        require(fixed_inputs() == state["fixed_inputs"], "fixed_inputs_changed")
        switch_current(OLD_RELEASE if rollback else RELEASE)
        state["completed_at_utc"] = now()
        save(state, "rolled_back" if rollback else "active")
        atomic(CONFIG / "deployment-result.json", (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())
        return state
    except BaseException:
        state["failed_phase"] = state["phase"]
        save(state, "failed")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "activate", "rollback"))
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    lock = None
    try:
        require(SHA.fullmatch(args.source_sha256) and args.source_sha256 == SOURCE_SHA256)
        environment()
        for path in (APP, APP / "incoming", APP / "releases"):
            directory(path, strict=False)
        for path in (SHARED, SHARED / "config", SHARED / "logs", SHARED / "tls", OLD_CONFIG, OLD_RELEASE):
            directory(path)
        lock = os.open(SHARED / "deploy.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not LOG.exists():
            write(LOG, b"Task repair deployment log.\n")
        read(LOG, 256 * 1024 * 1024, private=True)
        master = pinned_master()
        result = build(master, args.source_sha256) if args.phase == "build" else activate(
            master, args.source_sha256, rollback=args.phase == "rollback")
        print(json.dumps({"complete": True, "version": VERSION, "phase": result["phase"], "operator_review_required": False}))
        return 0
    except BaseException as error:
        print(json.dumps({"complete": False, "version": VERSION, "phase": args.phase,
                          "failure": error.code if isinstance(error, Failure) else "operation_failed",
                          "operator_review_required": True, "automatic_rollback": False}))
        return 1
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
