"""Offline backup orchestration, ownership and real archive round-trip checks."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "deploy/cloud/backup.py"
SPEC = importlib.util.spec_from_file_location("suxiaoyou_cloud_backup", SOURCE)
backup_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_tool)


def tenant(user_id, running=True):
    owner, name, volume = backup_tool.tenant_names(user_id)
    record = {"user_id": user_id, "container_name": name, "generation": 4, "state": "running" if running else "stopped"}
    container = {"Config": {"Labels": {backup_tool.OWNER_LABEL: owner, backup_tool.GENERATION_LABEL: "4"}},
                 "State": {"Running": running}, "Image": "sha256:" + "a" * 64,
                 "Mounts": [{"Destination": "/tenant", "Type": "volume", "Name": volume}]}
    volume_info = {"Name": volume, "Labels": {backup_tool.OWNER_LABEL: owner}}
    return record, container, volume_info


class Deployment(backup_tool.Deployment):
    def __init__(self, *, active=True, fail_archive=False, empty_target=False):
        self.calls = []
        self.records = [tenant("user-a")[0], tenant("user-b", False)[0]]
        self.containers = {} if empty_target else {item[0]["container_name"]: item[1] for item in [tenant("user-a"), tenant("user-b", False)]}
        self.volumes = {} if empty_target else {item[2]["Name"]: item[2] for item in [tenant("user-a"), tenant("user-b", False)]}
        self.active, self.fail_archive = active, fail_archive

    def call(self, *args, capture=False, stdin=None, stdout=None):
        self.calls.append(("compose", *args))
        if args[0] == "ps":
            if "--quiet" in args:
                return "original-" + args[-1] + "-id" if self.active else ""
            return "proxy\nweb\napi\nworker" if self.active else ""
        if "pg_dump" in " ".join(args):
            stdout.write(b"private postgres snapshot")
        return "" if capture else None

    def sql(self, statement):
        self.calls.append(("sql", statement))
        if "to_regclass" in statement:
            return "t"
        if "json_agg" in statement:
            return json.dumps(self.records)
        if "information_schema" in statement:
            return "0"
        if "cloud_jobs" in statement:
            return "0"
        if "state='unknown'" in statement:
            return "2"
        return ""

    def docker(self, *args, capture=False, missing_ok=False, stdin=None, stdout=None):
        self.calls.append(("docker", *args))
        if args[:2] == ("volume", "ls"):
            return "\n".join(self.volumes)
        if args[:2] == ("volume", "inspect"):
            info = self.volumes.get(args[2])
            return json.dumps([info]) if info else None
        if args[:2] == ("container", "inspect"):
            info = self.containers.get(args[2])
            return json.dumps([info]) if info else None
        if args[0] == "stop":
            self.containers[args[-1]]["State"]["Running"] = False
        if args[0] == "start":
            if args[-1] in self.containers:
                self.containers[args[-1]]["State"]["Running"] = True
            else:
                assert args[-1] in {"original-" + name + "-id" for name in ("api", "worker", "web", "proxy")}
        if args[:2] == ("volume", "create"):
            self.volumes[args[-1]] = {"Name": args[-1], "Labels": {backup_tool.OWNER_LABEL: args[3].split("=", 1)[1]}}
        return "" if capture else None

    def objects(self, program, **kwargs):
        self.calls.append(("objects", "archive" if program == backup_tool.ARCHIVE else "restore"))
        if "stdout" in kwargs:
            kwargs["stdout"].write(b"private object snapshot")

    def tenant_objects(self, volume_name, program, *, writable=False, **kwargs):
        self.calls.append(("tenant_archive" if not writable else "tenant_restore", volume_name))
        if self.fail_archive and not writable:
            raise ValueError("archive failed")
        if "stdout" in kwargs:
            kwargs["stdout"].write(b"private tenant snapshot " + volume_name.encode())


def snapshot(tmp_path):
    source = tmp_path / "snapshot"
    deployment = Deployment()
    backup_tool.backup(deployment, SimpleNamespace(directory=source, drain_timeout=1))
    return source, deployment


def test_tenant_backup_quiesces_all_writers_and_restores_only_originally_running_engines(tmp_path):
    source, deployment = snapshot(tmp_path)
    manifest = json.loads((source / "manifest.json").read_text())
    assert manifest["format"] == 2
    assert len(manifest["tenants"]) == 2
    assert len(manifest["sha256"]) == 4
    for name, digest in manifest["sha256"].items():
        assert backup_tool.digest(source / name) == digest
        assert stat.S_IMODE((source / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(source.stat().st_mode) == 0o700
    assert stat.S_IMODE((source / "tenants").stat().st_mode) == 0o700
    name_a, name_b = [entry["container_name"] for entry in manifest["tenants"]]
    stops = [call for call in deployment.calls if call[:2] == ("docker", "stop")]
    starts = [call for call in deployment.calls if call[:2] == ("docker", "start")]
    assert [call[-1] for call in stops] == [name_a]
    assert starts == [("docker", "start", "original-api-id"), ("docker", "start", name_a),
                      ("docker", "start", "original-worker-id"), ("docker", "start", "original-web-id"), ("docker", "start", "original-proxy-id")]
    assert not any(call[:2] == ("compose", "start") for call in deployment.calls)
    assert name_b not in [call[-1] for call in starts]
    dump_index = next(index for index, call in enumerate(deployment.calls) if "pg_dump" in " ".join(call))
    assert deployment.calls.index(stops[0]) < dump_index
    assert deployment.calls.index(("docker", "start", "original-api-id")) < deployment.calls.index(("docker", "start", name_a))
    assert not any("UPDATE cloud_quota" in " ".join(call) for call in deployment.calls)


def test_partial_archive_failures_still_resume_engines_and_never_publish_a_valid_snapshot(tmp_path):
    deployment = Deployment(fail_archive=True)
    target = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="archive failed"):
        backup_tool.backup(deployment, SimpleNamespace(directory=target, drain_timeout=1))
    assert not target.exists()
    pending = list(tmp_path.glob("snapshot.partial-*"))
    assert len(pending) == 1
    assert not (pending[0] / "manifest.json").exists()
    assert deployment.containers[backup_tool.tenant_names("user-a")[1]]["State"]["Running"]
    assert not deployment.containers[backup_tool.tenant_names("user-b")[1]]["State"]["Running"]
    assert ("docker", "start", "original-proxy-id") in deployment.calls


@pytest.mark.parametrize("change", ["owner", "generation", "mount"])
def test_foreign_tenant_container_or_volume_is_rejected(change):
    record, container, volume = tenant("user-a")
    if change == "owner":
        volume["Labels"][backup_tool.OWNER_LABEL] = "foreign-owner"
    elif change == "generation":
        container["Config"]["Labels"][backup_tool.GENERATION_LABEL] = "3"
    else:
        container["Mounts"][0]["Name"] = "foreign-volume"
    with pytest.raises(ValueError, match="ownership|generation|private volume"):
        backup_tool.verify_tenant(record, container, volume)


def test_unregistered_tenant_volumes_refuse_snapshot_before_any_engine_is_stopped(tmp_path):
    deployment = Deployment()
    deployment.volumes["sxy-data-orphan"] = {"Name": "sxy-data-orphan"}
    with pytest.raises(ValueError, match="Unregistered"):
        backup_tool.backup(deployment, SimpleNamespace(directory=tmp_path / "snapshot", drain_timeout=1))
    assert not any(call[:2] == ("docker", "stop") for call in deployment.calls)
    assert ("docker", "start", "original-api-id") in deployment.calls


def test_restore_refuses_existing_tenant_volumes_even_in_another_compose_project(tmp_path):
    source, _ = snapshot(tmp_path)
    target = Deployment(active=False)
    with pytest.raises(ValueError, match="already exists"):
        backup_tool.restore(target, SimpleNamespace(directory=source))
    assert not any(call[:2] == ("compose", "up") for call in target.calls)


def test_restore_revokes_sessions_and_engine_capabilities_without_refunding_unknown_usage(tmp_path):
    source, _ = snapshot(tmp_path)
    target = Deployment(active=False, empty_target=True)
    backup_tool.restore(target, SimpleNamespace(directory=source))
    assert len([call for call in target.calls if call[0] == "tenant_restore"]) == 2
    statements = [call[1] for call in target.calls if call[0] == "sql"]
    assert "DELETE FROM cloud_auth_sessions" in statements
    assert any("UPDATE cloud_tenant_runtimes SET state='stopped', generation=generation+1" in query for query in statements)
    assert not any("UPDATE cloud_quota" in query or "DELETE FROM cloud_request" in query for query in statements)
    assert not any(call[:2] == ("compose", "start") for call in target.calls)


def test_corrupt_tenant_archive_is_rejected_before_creating_target_resources(tmp_path):
    source, _ = snapshot(tmp_path)
    manifest = json.loads((source / "manifest.json").read_text())
    (source / manifest["tenants"][0]["archive"]).write_bytes(b"tampered")
    target = Deployment(active=False, empty_target=True)
    with pytest.raises(ValueError, match="checksum mismatch"):
        backup_tool.restore(target, SimpleNamespace(directory=source))
    assert target.calls == []


def test_archive_round_trip_preserves_real_sqlite_files_binary_contents_and_runtime_symlinks(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    source.mkdir()
    restored.mkdir()
    (source / "workspaces").mkdir()
    (source / "secrets").mkdir(mode=0o700)
    key = source / "secrets/signing.key"
    key.write_bytes(b"private signing key round trip")
    key.chmod(0o600)
    (source / "workspaces/report.bin").write_bytes(bytes(range(256)))
    (source / "python").symlink_to("/usr/bin/python3")
    with sqlite3.connect(source / "engine.db") as connection:
        connection.execute("CREATE TABLE sessions (title TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('云端完整会话')")
    archive = subprocess.check_output([sys.executable, "-c", backup_tool.ARCHIVE.replace(repr(backup_tool.DATA_DIR), repr(str(source)))])
    subprocess.run([sys.executable, "-c", backup_tool.EXTRACT.replace(repr(backup_tool.DATA_DIR), repr(str(restored)))], input=archive, check=True)
    assert (restored / "workspaces/report.bin").read_bytes() == bytes(range(256))
    assert os.readlink(restored / "python") == "/usr/bin/python3"
    assert stat.S_IMODE((restored / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE((restored / "secrets/signing.key").stat().st_mode) == 0o600
    assert (restored / "secrets/signing.key").read_bytes() == b"private signing key round trip"
    with sqlite3.connect(restored / "engine.db") as connection:
        assert connection.execute("SELECT title FROM sessions").fetchone() == ("云端完整会话",)


def test_extract_never_follows_archive_symlinks_to_write_outside_private_volume(tmp_path):
    target, outside = tmp_path / "target", tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        link = tarfile.TarInfo("escape")
        link.type, link.linkname = tarfile.SYMTYPE, str(outside)
        archive.addfile(link)
        item = tarfile.TarInfo("escape/should-not-escape")
        item.size = 4
        archive.addfile(item, io.BytesIO(b"test"))
    result = subprocess.run([sys.executable, "-c", backup_tool.EXTRACT.replace(repr(backup_tool.DATA_DIR), repr(str(target)))], input=payload.getvalue(), capture_output=True)
    assert result.returncode != 0
    assert not (outside / "should-not-escape").exists()


@pytest.mark.parametrize("redirect", ["manifest", "tenant_parent"])
def test_restore_rejects_symlinked_snapshot_metadata_and_payload_parents(tmp_path, redirect):
    source, _ = snapshot(tmp_path)
    if redirect == "manifest":
        original = source / "manifest.json"
        relocated = tmp_path / "outside-manifest.json"
        original.rename(relocated)
        original.symlink_to(relocated)
    else:
        original = source / "tenants"
        relocated = tmp_path / "outside-tenants"
        original.rename(relocated)
        original.symlink_to(relocated, target_is_directory=True)
    target = Deployment(active=False, empty_target=True)
    with pytest.raises(ValueError, match="regular private file"):
        backup_tool.restore(target, SimpleNamespace(directory=source))
    assert target.calls == []
