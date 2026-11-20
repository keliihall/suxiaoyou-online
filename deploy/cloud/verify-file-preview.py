#!/usr/bin/env python3
"""Read-only provenance of the Web-only clickable file preview release.

Server root mode takes a reviewed private pins JSON and its SHA256. Local mode
checks an existing Next production build. This script makes no HTTP/model/DB
calls, reads no credentials/business messages/logs, and does not exec a tenant.
Only public compiled asset hashes, source counts and Boolean proofs leave it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

VERSION = "20261117-preview"
OLD_VERSION = "20261117-results2"
OLD_WEB_VERSION = "20261117-results"
APP = Path("/srv/suxiaoyou")
RELEASE = APP / "releases" / VERSION
CONFIG = APP / "shared/config" / VERSION
PROJECT = "suxiaoyou-cloud"
ORIGIN = "https://nav.cyneu.com:18443"
SHA = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
API_IMAGE = "sha256:d8f9c761513c7f34bbbd6f123d198f69d7e565f56bc1ad73e97b6ed0c1c286ed"
RUNTIME_IMAGE = "sha256:724eb22b285089ed453b2f443b80ee2ebd9222d245b7a90a5e8d837f50649ee8"
PREFIX = ["runuser", "--user", "suxiaoyou", "--", "env", "HOME=/home/suxiaoyou",
          "XDG_RUNTIME_DIR=/run/user/1001", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
          "DOCKER_HOST=unix:///run/user/1001/docker.sock", "docker"]
SERVICES = ("db", "api", "worker", "web", "proxy")
IDENTITY = ("id", "name", "image", "image_id", "started_at", "mounts")


class Failure(Exception):
    pass


def require(value, code):
    if not value:
        raise Failure(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate_json_key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, "invalid_json_number"))


def read(path, maximum, *, private=False, owners=(1001,)):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid in owners and info.st_nlink == 1
                and not stat.S_IMODE(info.st_mode) & 0o022 and 0 <= info.st_size <= maximum
                and (not private or stat.S_IMODE(info.st_mode) == 0o600), "unsafe_provenance_file")
        raw = source.read(maximum + 1)
    require(len(raw) == info.st_size, "provenance_file_changed")
    return raw


def command(arguments, *, maximum=65536):
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=180, check=False)
    require(result.returncode == 0 and len(result.stdout) <= maximum, "read_probe_failed")
    return result.stdout


def docker(*args, maximum=65536):
    return command(PREFIX + list(args), maximum=maximum)


def validate_pins(pins):
    hashes = {"archive_sha256", "source_sha256", "manifest_sha256", "controller_sha256"}
    require(isinstance(pins, dict) and set(pins) == hashes | {"images", "changes", "compiled_markers"}
            and all(isinstance(pins[name], str) and SHA.fullmatch(pins[name]) for name in hashes), "invalid_pins")
    require(isinstance(pins["images"], dict) and set(pins["images"]) == {"api", "runtime", "web"}
            and all(isinstance(value, str) and IMAGE_SHA.fullmatch(value) for value in pins["images"].values())
            and pins["images"]["api"] == API_IMAGE and pins["images"]["runtime"] == RUNTIME_IMAGE,
            "backend_image_must_be_preserved")
    changes = pins["changes"]
    require(isinstance(changes, dict) and 1 <= len(changes) <= 64 and all(
        isinstance(name, str) and name.startswith("frontend/src/") and ".." not in PurePosixPath(name).parts
        and isinstance(value, str) and SHA.fullmatch(value) for name, value in changes.items()), "invalid_source_allowlist")
    markers = pins["compiled_markers"]
    require(isinstance(markers, list) and 1 <= len(markers) <= 16 and len(set(markers)) == len(markers)
            and all(isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{4,95}", value)
                    for value in markers), "invalid_compiled_markers")


def container(name):
    template = ('{"id":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
                '"image":{{json .Config.Image}},"started_at":{{json .State.StartedAt}},'
                '"running":{{json .State.Running}},"oom":{{json .State.OOMKilled}},'
                '"health":{{with index .State "Health"}}{{json (index . "Status")}}{{else}}null{{end}},'
                '"mounts":{{json .Mounts}}}')
    value = decode(docker("container", "inspect", "--format", template, name))
    require(SHA.fullmatch(value.get("id", "")) and IMAGE_SHA.fullmatch(value.get("image_id", "")),
            "invalid_container_identity")
    return value


def deployment_contract(state):
    require(all(state.get(key) is False for key in (
        "schema_changes", "cloud_schema_changes", "dependencies_changed", "credentials_changed", "automatic_rollback"))
        and state.get("tenant_schema_migration") is None and state.get("api_runtime_version") == OLD_VERSION
        and state.get("old_web_version") == OLD_WEB_VERSION and all(state.get(key) == 0 for key in (
            "model_requests", "runtime_restarts", "database_operations", "auth_operations", "task_operations"))
        and state.get("non_web_containers_preserved") is True
        and state.get("tenant_containers_preserved") is True, "web_only_state_mismatch")


def preserved_services(current, state, images):
    require(set(current) == set(state["services_after"]) == set(SERVICES), "service_set_changed")
    for name, value in current.items():
        require(all(value.get(key) == state["services_after"][name].get(key) for key in IDENTITY)
                and value["running"] is True and value["oom"] is False
                and (name not in {"db", "api", "web"} or value["health"] == "healthy"), "active_container_changed")
        if name == "web":
            require(value["image"] == "suxiaoyou-cloud-web:" + VERSION
                    and value["image_id"] == images["web"], "web_image_mismatch")
        else:
            for stage in ("original_services", "activation_services"):
                require(all(value.get(key) == state[stage][name].get(key) for key in IDENTITY),
                        "non_web_container_changed")
            if name in {"api", "worker"}:
                require(value["image"] == "suxiaoyou-cloud-api:" + OLD_VERSION
                        and value["image_id"] == images["api"], "control_plane_image_changed")


def preserved_tenants(current, state):
    require(isinstance(current, dict) and len(current) <= 10000, "invalid_runtime_inventory")
    for stage in ("original_tenants", "activation_tenants", "tenants_after"):
        require(isinstance(state.get(stage), dict), "tenant_proof_missing")
        for name, original in state[stage].items():
            require(name in current and all(current[name].get(key) == original.get(key) for key in IDENTITY)
                    and (not original["running"] or current[name]["running"] is True), "tenant_container_changed")


WEB_PROBE = r'''
const fs=require("fs"),path=require("path"),crypto=require("crypto"),root=path.resolve(process.argv[1]);
const markers=JSON.parse(process.argv[2]),flags={history_refresh:t=>t.includes("Message history changed during refresh"),
 default_deliverables:t=>/fileScope\s*:\s*"deliverables"/.test(t),file_request_revision:t=>t.includes("fileRequestRevision"),
 scoped_files:t=>t.includes("?scope="),composer_control:t=>t.includes("chat-primary-action")};
for(const marker of markers)flags[marker]=t=>t.includes(marker);
const found=[],files=[],checks=Object.fromEntries(Object.keys(flags).map(k=>[k,false]));let bytes=0;
function must(v){if(!v)throw Error("compiled_contract_failed");}
function walk(p){must(fs.lstatSync(p).isDirectory());for(const ent of fs.readdirSync(p,{withFileTypes:true})){
 const q=path.join(p,ent.name);must(!ent.isSymbolicLink());if(ent.isDirectory())walk(q);else if(ent.isFile()&&q.endsWith(".js"))files.push(q);}}
try{
 must(Array.isArray(markers)&&markers.length>0&&markers.length<=16);walk(path.join(root,"static/chunks"));must(files.length<=3000);files.sort();
 for(const p of files){const info=fs.lstatSync(p);bytes+=info.size;must(info.size<=16*1024*1024&&bytes<=256*1024*1024);
  const raw=fs.readFileSync(p),text=raw.toString("utf8"),matched=Object.keys(flags).filter(k=>!checks[k]&&flags[k](text));
  if(matched.length){for(const k of matched)checks[k]=true;found.push({url:"https://nav.cyneu.com:18443/_next/"+path.relative(root,p).split(path.sep).map(encodeURIComponent).join("/"),
   sha256:crypto.createHash("sha256").update(raw).digest("hex")});}}
 must(Object.values(checks).every(v=>v===true));const build=fs.readFileSync(path.join(root,"BUILD_ID"),"utf8").trim();must(/^[A-Za-z0-9_-]{1,128}$/.test(build));
 console.log(JSON.stringify({ok:true,checks,assets:found}));
}catch(_){console.log('{"ok":false,"error":"compiled_contract_failed"}');process.exitCode=1;}
'''


def compiled_probe(arguments, markers):
    value = decode(command(arguments + [json.dumps(markers, separators=(",", ":"))], maximum=32768))
    expected = {"history_refresh", "default_deliverables", "file_request_revision", "scoped_files", "composer_control", *markers}
    require(value.get("ok") is True and set(value.get("checks", {})) == expected
            and all(flag is True for flag in value["checks"].values()), "compiled_contract_failed")
    assets = value.get("assets")
    require(isinstance(assets, list) and 1 <= len(assets) <= len(expected) and all(
        isinstance(item, dict) and set(item) == {"url", "sha256"} and isinstance(item["url"], str)
        and item["url"].startswith(ORIGIN + "/_next/static/chunks/") and IMAGE_SHA.fullmatch("sha256:" + item["sha256"])
        for item in assets), "unsafe_asset_result")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--compiled-marker", action="append", default=[])
    parser.add_argument("--pins", type=Path)
    parser.add_argument("--pins-sha256")
    args = parser.parse_args()
    if args.build_root is not None:
        require(args.compiled_marker, "compiled_markers_required")
        return {"ok": True, "version": VERSION, "local_only": True,
                "web": compiled_probe(["node", "-e", WEB_PROBE, str(args.build_root.resolve())], args.compiled_marker)}
    require(os.getuid() == os.geteuid() == 0, "root_required")
    require(args.pins is not None and SHA.fullmatch(args.pins_sha256 or ""), "pins_required")
    raw = read(args.pins, 128 * 1024, owners=(0, 1001))
    require(digest(raw) == args.pins_sha256, "pins_file_changed")
    pins = decode(raw)
    validate_pins(pins)
    require((APP / "current").is_symlink() and (APP / "current").resolve() == RELEASE, "current_release_mismatch")
    state = decode(read(CONFIG / "upgrade-state.json", 8 * 1024 * 1024, private=True))
    require(state.get("version") == VERSION and state.get("old_version") == OLD_VERSION and state.get("phase") == "active"
            and state.get("source_archive_sha256") == pins["archive_sha256"] and state.get("source_sha256") == pins["source_sha256"]
            and state.get("controller_sha256") == pins["controller_sha256"] and state.get("built_web_image") == pins["images"]["web"]
            and sorted(state.get("source_changes", [])) == sorted(pins["changes"]), "active_state_mismatch")
    deployment_contract(state)
    require(digest(read(APP / "incoming/upgrade-file-preview.py", 128 * 1024)) == pins["controller_sha256"], "controller_identity_mismatch")
    raw = read(RELEASE / "RELEASE-MANIFEST.json", 4 * 1024 * 1024)
    require(digest(raw) == pins["manifest_sha256"], "manifest_identity_mismatch")
    manifest = decode(raw)
    records = {item["path"]: item for item in manifest["files"]}
    require(manifest["source_sha256"] == pins["source_sha256"]
            and len(records) == len(manifest["files"]) == manifest["source_file_count"], "manifest_contract_mismatch")
    for name, expected in pins["changes"].items():
        record = records[name]
        require(record["sha256"] == expected and digest(read(RELEASE / name, record["size"] + 1)) == expected, "release_source_changed")
    services = {name: container(PROJECT + "-" + name + "-1") for name in SERVICES}
    preserved_services(services, state, pins["images"])
    names = docker("ps", "-a", "--filter", "name=^/sxy-user-", "--format", "{{.Names}}").decode().splitlines()
    require(len(names) <= 10000 and len(names) == len(set(names))
            and all(re.fullmatch(r"sxy-user-[a-f0-9]{32}", name) for name in names), "invalid_runtime_inventory")
    preserved_tenants({name: container(name) for name in names}, state)
    runtime = decode(docker("image", "inspect", "suxiaoyou-online-runtime:" + OLD_VERSION, maximum=256 * 1024))[0]
    require(runtime.get("Id") == RUNTIME_IMAGE, "runtime_image_tag_changed")
    web = compiled_probe(PREFIX + ["exec", "--user", "10001:10001", PROJECT + "-web-1", "node", "-e", WEB_PROBE,
                                  "/app/.next"], pins["compiled_markers"])
    return {"ok": True, "version": VERSION, "active_state": True, "source_pins": True, "image_pins": True,
            "non_web_containers_preserved": True, "tenant_containers_preserved": True,
            "database_auth_task_operations": 0, "model_requests": 0, "web": web}


if __name__ == "__main__":
    try:
        result = main()
        raw = json.dumps(result, separators=(",", ":"))
        require(len(raw.encode()) <= 8192, "safe_output_budget_exceeded")
        status = 0
    except BaseException as error:
        raw = json.dumps({"ok": False, "version": VERSION,
                          "error": str(error) if isinstance(error, Failure) else "verification_failed"}, separators=(",", ":"))
        status = 1
    print("SXY_FILE_PREVIEW_PROVENANCE:" + raw)
    raise SystemExit(status)
