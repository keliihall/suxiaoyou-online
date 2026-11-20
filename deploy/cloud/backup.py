#!/usr/bin/env python3
"""Offline PostgreSQL, local-object and private-tenant snapshots.

Restore accepts only an empty target. Existing cloud state is never overwritten.
Requires Docker Compose v2, built cloud images and Python 3.10+ on the host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
FILES = ("database.dump", "objects.tar.gz")
DATA_DIR = "/var/lib/suxiaoyou"
OWNER_LABEL = "io.suxiaoyou.cloud.owner"
GENERATION_LABEL = "io.suxiaoyou.cloud.generation"
ARCHIVE = """
import pathlib, sys, tarfile
root = pathlib.Path('/var/lib/suxiaoyou')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:
    for item in sorted(root.iterdir()):
        archive.add(item, arcname=item.name, recursive=True)
"""
EMPTY = """
import pathlib, sys
sys.exit(1 if any(pathlib.Path('/var/lib/suxiaoyou').iterdir()) else 0)
"""
EXTRACT = """
import os, pathlib, sys, tarfile
os.umask(0o077)
root = pathlib.Path('/var/lib/suxiaoyou')
if any(root.iterdir()):
    raise SystemExit('Object target is not empty; restore refused')
# Defer symlinks until all file bytes are written. Virtual environments and
# workspace projects may legitimately link to immutable runtime paths such as
# /usr/bin/python; never follow those links while extracting an archive.
links = []
directories = []
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
    for item in archive:
        name = pathlib.PurePosixPath(item.name)
        if name.is_absolute() or '..' in name.parts or not name.parts:
            raise SystemExit('Unsafe archive path')
        if item.issym():
            links.append((item.name, item.linkname))
        else:
            # Python's data filter deliberately clears directory modes. Keep
            # their original safe permission bits, then restore them only after
            # all bytes have been written (a read-only dir must remain writable
            # while extracting its children).
            if item.isdir():
                directories.append((item.name, item.mode & 0o777))
            archive.extract(item, path=root, filter='data')
for name, target in links:
    output = root / name
    if not output.parent.resolve().is_relative_to(root.resolve()):
        raise SystemExit('Unsafe archive symlink parent')
    os.symlink(target, output)
for name, mode in reversed(directories):
    output = root / name
    if output.is_symlink() or not output.is_dir() or not output.resolve().is_relative_to(root.resolve()):
        raise SystemExit('Unsafe archive directory permission target')
    os.chmod(output, mode, follow_symlinks=False)
"""


def private_output(path: Path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def tenant_names(user_id: str) -> tuple[str, str, str]:
    owner = hashlib.sha256(user_id.encode()).hexdigest()
    return owner, f"sxy-user-{owner[:32]}", f"sxy-data-{owner[:32]}"


def verify_tenant(record: dict, container: dict | None, volume: dict) -> dict:
    owner, container_name, volume_name = tenant_names(record["user_id"])
    if record.get("container_name") != container_name:
        raise ValueError("Tenant container metadata does not match its owner")
    if volume.get("Name") != volume_name or (volume.get("Labels") or {}).get(OWNER_LABEL) != owner:
        raise ValueError("Tenant volume ownership check failed")
    if record.get("state") == "starting":
        raise ValueError("A tenant is still being provisioned; retry after it finishes")
    result = {"user_id": record["user_id"], "owner": owner, "container_name": container_name,
              "volume_name": volume_name, "generation": record["generation"], "running": False,
              "image_id": None, "archive": f"tenants/{owner}.tar.gz"}
    if container is not None:
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get(OWNER_LABEL) != owner or labels.get(GENERATION_LABEL) != str(record["generation"]):
            raise ValueError("Tenant container ownership or generation check failed")
        mounts = [item for item in container.get("Mounts", []) if item.get("Destination") == "/tenant"]
        if len(mounts) != 1 or mounts[0].get("Type") != "volume" or mounts[0].get("Name") != volume_name:
            raise ValueError("Tenant container is not bound to its private volume")
        result["running"] = bool(container.get("State", {}).get("Running"))
        result["image_id"] = container.get("Image")
    return result


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class Deployment:
    def __init__(self, args):
        self.command = ["docker", "compose", "--env-file", str(args.env_file.resolve()),
                        "-f", str(ROOT / "compose.cloud.yaml")]
        if args.project_name:
            self.command += ["--project-name", args.project_name]
        # Capture configuration only in memory; it contains secrets.
        config = json.loads(self.call("config", "--format", "json", capture=True))
        environment = config["services"]["api"]["environment"]
        if environment["SUXIAOYOU_CLOUD_STORAGE_BACKEND"] != "local":
            raise ValueError("This tool covers local objects only; use a coordinated S3 snapshot instead")
        self.helper_image = config["services"]["api"]["image"]
        # The host CLI and Compose share DOCKER_HOST / docker context. The bind
        # path belongs to the deployment host and may not be a usable client
        # socket path on a remote Docker context (for example a Linux VM).
        self.docker_command = ["docker"]

    def call(self, *arguments, capture=False, stdin=None, stdout=None):
        completed = subprocess.run(self.command + list(arguments), cwd=ROOT, check=True,
                                   stdin=stdin, stdout=subprocess.PIPE if capture else stdout)
        return completed.stdout.decode().strip() if capture else None

    def sql(self, statement: str) -> str:
        # The query is a fixed program argument, never shell-interpolated.
        return self.call("exec", "-T", "db", "sh", "-c",
                         'exec psql -XAt --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -c "$1"',
                         "sh", statement, capture=True)

    def objects(self, program: str, **kwargs):
        return self.call("run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
                         "api", "-c", program, **kwargs)

    def docker(self, *arguments, capture=False, missing_ok=False, stdin=None, stdout=None):
        result = subprocess.run(self.docker_command + list(arguments), cwd=ROOT, stdin=stdin,
            stdout=subprocess.PIPE if capture else stdout, stderr=subprocess.PIPE)
        if result.returncode:
            if missing_ok and result.returncode == 1:
                return None
            raise ValueError("Tenant Docker operation failed; check the dedicated daemon and image availability")
        return result.stdout.decode().strip() if capture else None

    def tenant_inventory(self) -> list[dict]:
        records = self.tenant_records()
        tenants = []
        for record in records:
            _owner, container_name, volume_name = tenant_names(record["user_id"])
            raw_volume = self.docker("volume", "inspect", volume_name, capture=True, missing_ok=True)
            if raw_volume is None:
                raise ValueError("A registered tenant volume is missing; snapshot refused")
            raw_container = self.docker("container", "inspect", container_name, capture=True, missing_ok=True)
            tenants.append(verify_tenant(record, json.loads(raw_container)[0] if raw_container else None,
                                         json.loads(raw_volume)[0]))
        # Unregistered labelled volumes must not silently disappear from backup.
        names = self.docker("volume", "ls", "--filter", f"label={OWNER_LABEL}", "--format", "{{.Name}}", capture=True).splitlines()
        if set(names) - {item["volume_name"] for item in tenants}:
            raise ValueError("Unregistered tenant volumes require investigation before snapshot")
        return tenants

    def tenant_records(self) -> list[dict]:
        if self.sql("SELECT to_regclass('public.cloud_tenant_runtimes') IS NOT NULL") != "t":
            return []
        return json.loads(self.sql("SELECT COALESCE(json_agg(json_build_object('user_id',user_id,"
            "'container_name',container_name,'generation',generation,'state',state)), '[]'::json) "
            "FROM cloud_tenant_runtimes"))

    def tenant_objects(self, volume_name: str, program: str, *, writable=False, **kwargs):
        mount = f"type=volume,source={volume_name},target={DATA_DIR}" + ("" if writable else ",readonly")
        return self.docker("run", "--rm", "--interactive", "--network", "none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit", "32",
            "--memory", "512m", "--user", "10001:10001", "--mount", mount,
            "--entrypoint", "python", self.helper_image, "-c", program, **kwargs)

    def resume(self, app_services: dict[str, list[str]], tenants: list[dict]):
        failures = []
        # Start the metering gateway before engines, and ingress last.
        if "api" in app_services:
            try:
                for container_id in app_services["api"]:
                    self.docker("start", container_id)
                self.docker("exec", app_services["api"][0], "python", "-c", """
import time, urllib.request
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=2).close()
        break
    except Exception:
        time.sleep(.5)
else:
    raise SystemExit('Control plane did not restart')
""")
            except (ValueError, OSError, subprocess.CalledProcessError):
                failures.append("api")
        for tenant in tenants:
            if tenant["running"]:
                try:
                    self.docker("start", tenant["container_name"])
                except (ValueError, OSError, subprocess.CalledProcessError):
                    failures.append(tenant["container_name"])
        for service in ("worker", "web", "proxy"):
            if service in app_services:
                try:
                    for container_id in app_services[service]:
                        self.docker("start", container_id)
                except (ValueError, OSError, subprocess.CalledProcessError):
                    failures.append(service)
        if failures:
            raise ValueError("Snapshot service recovery failed for: " + ", ".join(failures))


def backup(deployment: Deployment, args):
    target = args.directory.resolve()
    if target.exists():
        raise ValueError("Backup destination already exists; choose a new directory")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = target.with_name(target.name + ".partial-" + uuid4().hex[:8])
    pending.mkdir(mode=0o700)
    running = deployment.call("ps", "--status", "running", "--services", capture=True).splitlines()
    # Capture exact running IDs before stopping. Compose start may also start
    # dependencies (including one-shot migrate), and --all would revive stopped
    # replicas; neither is part of restoring the original running state.
    app_services = {name: deployment.call("ps", "--status", "running", "--quiet", name, capture=True).splitlines()
                    for name in ("proxy", "web", "api", "worker") if name in running}
    if any(not ids for ids in app_services.values()):
        raise ValueError("Application container inventory changed; snapshot refused")
    tenants = []
    try:
        ingress = [name for name in app_services if name != "worker"]
        if ingress:
            deployment.call("stop", *ingress)
        tenants = deployment.tenant_inventory()
        for tenant in tenants:
            if tenant["running"]:
                deployment.docker("stop", "--time", str(args.drain_timeout), tenant["container_name"])
        # Archive only after every tenant has stopped, including SQLite/WAL and
        # scheduled-work writers. The private volumes retain their original IDs.
        for tenant in tenants:
            raw = deployment.docker("container", "inspect", tenant["container_name"], capture=True, missing_ok=True)
            if raw and json.loads(raw)[0].get("State", {}).get("Running"):
                raise ValueError("Tenant did not quiesce; snapshot refused")
        # Keep the worker alive to drain admitted work after ingress is stopped.
        deadline = time.monotonic() + args.drain_timeout
        while deployment.sql("SELECT count(*) FROM cloud_jobs WHERE state IN ('queued','running')") != "0":
            if time.monotonic() >= deadline:
                raise ValueError("Jobs did not drain before timeout; backup aborted and services will restart")
            time.sleep(2)
        if "worker" in app_services:
            deployment.call("stop", "worker")
        with private_output(pending / FILES[0]) as stream:
            deployment.call("exec", "-T", "db", "sh", "-c",
                            'exec pg_dump --format=custom --no-owner --no-acl --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"',
                            stdout=stream)
        with private_output(pending / FILES[1]) as stream:
            deployment.objects(ARCHIVE, stdout=stream)
        (pending / "tenants").mkdir(mode=0o700)
        archived = list(FILES)
        for tenant in tenants:
            with private_output(pending / tenant["archive"]) as stream:
                deployment.tenant_objects(tenant["volume_name"], ARCHIVE, stdout=stream)
            archived.append(tenant["archive"])
        manifest = {"format": 2, "created_at": datetime.now(timezone.utc).isoformat(),
                    "storage_backend": "local", "tenants": tenants,
                    "sha256": {name: digest(pending / name) for name in archived}}
        with private_output(pending / "manifest.json") as stream:
            stream.write((json.dumps(manifest, indent=2) + "\n").encode())
        pending.rename(target)
        print(f"Snapshot completed: {target}")
    finally:
        try:
            deployment.resume(app_services, tenants)
        finally:
            if pending.exists():
                print(f"Incomplete snapshot retained for diagnosis: {pending}", file=sys.stderr)


def restore(deployment: Deployment, args):
    source = args.directory.resolve()
    manifest_path = source / "manifest.json"
    if manifest_path.is_symlink() or not stat.S_ISREG(manifest_path.stat().st_mode):
        raise ValueError("Snapshot manifest must be a regular private file")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") not in {1, 2} or manifest.get("storage_backend") != "local":
        raise ValueError("Unsupported snapshot format")
    tenants = manifest.get("tenants", []) if manifest["format"] == 2 else []
    owners = set()
    for tenant in tenants:
        owner, container_name, volume_name = tenant_names(tenant["user_id"])
        if (owner in owners or tenant.get("owner") != owner or tenant.get("container_name") != container_name
                or tenant.get("volume_name") != volume_name or tenant.get("archive") != f"tenants/{owner}.tar.gz"):
            raise ValueError("Invalid tenant archive binding")
        owners.add(owner)
    expected_files = {*FILES, *(tenant["archive"] for tenant in tenants)}
    if set(manifest.get("sha256", {})) != expected_files:
        raise ValueError("Snapshot manifest file set is invalid")
    for name in expected_files:
        if ((source / name).is_symlink() or not (source / name).resolve().is_relative_to(source)
                or not stat.S_ISREG((source / name).stat().st_mode)):
            raise ValueError("Snapshot payload must be a regular private file")
        if digest(source / name) != manifest.get("sha256", {}).get(name):
            raise ValueError(f"Snapshot checksum mismatch: {name}")
    for tenant in tenants:
        if deployment.docker("volume", "inspect", tenant["volume_name"], capture=True, missing_ok=True) is not None:
            raise ValueError("Tenant target volume already exists; use an isolated empty Docker daemon")
        if deployment.docker("container", "inspect", tenant["container_name"], capture=True, missing_ok=True) is not None:
            raise ValueError("Tenant target container already exists; restore refused")
    running = deployment.call("ps", "--status", "running", "--services", capture=True).splitlines()
    if set(running) & {"proxy", "web", "api", "worker", "migrate"}:
        raise ValueError("Target application services must be stopped before restoring")
    deployment.call("up", "-d", "--wait", "db")
    if deployment.sql("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'") != "0":
        raise ValueError("Database target is not empty; use a fresh Compose project/volumes")
    deployment.objects(EMPTY)
    with (source / FILES[0]).open("rb") as stream:
        deployment.call("exec", "-T", "db", "sh", "-c",
                        'exec pg_restore --exit-on-error --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"',
                        stdin=stream)
    records = deployment.tenant_records()
    if ({(item["user_id"], item["generation"]) for item in records}
            != {(item["user_id"], item["generation"]) for item in tenants}):
        raise ValueError("Database tenants do not match the archived private volumes")
    if deployment.sql("SELECT count(*) FROM cloud_jobs WHERE state IN ('queued','running')") != "0":
        raise ValueError("Snapshot is not quiescent: queued/running jobs require manual investigation; do not start workers")
    with (source / FILES[1]).open("rb") as stream:
        deployment.objects(EXTRACT, stdin=stream)
    for tenant in tenants:
        deployment.docker("volume", "create", "--label", f"{OWNER_LABEL}={tenant['owner']}", tenant["volume_name"])
        with (source / tenant["archive"]).open("rb") as stream:
            deployment.tenant_objects(tenant["volume_name"], EXTRACT, writable=True, stdin=stream)
    # A database rollback must not revive old browser sessions. This snapshot
    # format has no global recovery epoch; isolated targets and fencing old
    # deployments remain an operational prerequisite.
    deployment.sql("DELETE FROM cloud_auth_sessions")
    if tenants:
        # A rollback must not revive engine inference capabilities. Subsequent
        # authorized startup creates a new container and rotates both tokens.
        deployment.sql("UPDATE cloud_tenant_runtimes SET state='stopped', generation=generation+1, "
                       "container_id=NULL, host_port=NULL, error='' ")
    unknown = deployment.sql("SELECT count(*) FROM cloud_request_records WHERE state='unknown'")
    print(f"Snapshot restored; all restored sessions revoked; unknown requests requiring review: {unknown}.")
    print("Application services remain stopped. Fence old workers and review unknown requests before enabling work.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.cloud")
    parser.add_argument("--project-name", help="Separate target project for a restore drill")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("backup", help="Drain work, stop writers, save a consistent snapshot, restart")
    snapshot.add_argument("directory", type=Path)
    snapshot.add_argument("--drain-timeout", type=int, default=120)
    recovery = commands.add_parser("restore", help="Restore verified snapshot into empty PostgreSQL/object volumes")
    recovery.add_argument("directory", type=Path)
    args = parser.parse_args()
    if getattr(args, "drain_timeout", 1) < 1:
        parser.error("--drain-timeout must be positive")
    try:
        deployment = Deployment(args)
        (backup if args.command == "backup" else restore)(deployment, args)
    except (ValueError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        # Do not render CalledProcessError.cmd: commands may contain env names
        # and internal configuration. Compose stderr still reports failures.
        if isinstance(exc, subprocess.CalledProcessError):
            parser.exit(1, f"Deployment command failed (exit {exc.returncode}).\n")
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
