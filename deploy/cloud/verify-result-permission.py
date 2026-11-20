#!/usr/bin/env python3
"""Read-only presentation-permission patch proof; no inference or business reads.

Local: --build-root /path/to/production/.next.
Server (root): --pins reviewed.json --pins-sha256 <reviewed JSON file digest>.
Pins contain archive_sha256, source_sha256, manifest_sha256, controller_sha256,
runtime_helper_sha256, images {api,runtime,web}, and changes {relative-path:SHA}.
No environment file, credential, log, message body, model or mutation is read/run.
Only Boolean proofs, source counts/digests and public asset URLs leave stdout.
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

VERSION = "20261117-results2"
OLD_VERSION = "20261117-results"
OLD_BACKEND = OLD_VERSION
WEB_VERSION = OLD_VERSION
HEAD = "0013_v110_context_projection"
MIGRATION = "backend/alembic/versions/" + HEAD + ".py"
MIGRATION_SHA = "f27dfaa0beb655cc5e81241154c4999223ac5c90500124bc5c4967a000559787"
PROCESSOR = "backend/app/session/processor.py"
PROCESSOR_SHA = "46949ae5f96ea5ed23baae5cccdc31b826dda185591ea3f5c2fd6897e081e6e4"
WEB_IMAGE = "sha256:a5c24e6872cbf840c8edd0fd606bddd677c98867a8ed7bf47b8bf24de19e71d1"
APP = Path("/srv/suxiaoyou")
RELEASE = APP / "releases" / VERSION
CONFIG = APP / "shared/config" / VERSION
PROJECT = "suxiaoyou-cloud"
ORIGIN = "https://nav.cyneu.com:18443"
SHA = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
PREFIX = ["runuser", "--user", "suxiaoyou", "--", "env", "HOME=/home/suxiaoyou",
          "XDG_RUNTIME_DIR=/run/user/1001", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
          "DOCKER_HOST=unix:///run/user/1001/docker.sock", "docker"]


class Failure(Exception):
    pass


def require(value, code):
    if not value:
        raise Failure(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result, "duplicate_json_key")
            result[name] = value
        return result
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


def command(arguments, *, content=None, maximum=65536):
    result = subprocess.run(arguments, input=content, stdin=subprocess.DEVNULL if content is None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180, check=False)
    require(result.returncode == 0 and len(result.stdout) <= maximum, "read_probe_failed")
    return result.stdout


def docker(*args, content=None, maximum=65536):
    return command(PREFIX + list(args), content=content, maximum=maximum)


def validate_pins(pins):
    hashes = {"archive_sha256", "source_sha256", "manifest_sha256", "controller_sha256", "runtime_helper_sha256"}
    require(isinstance(pins, dict) and set(pins) == hashes | {"images", "changes"}
            and all(isinstance(pins[name], str) and SHA.fullmatch(pins[name]) for name in hashes), "invalid_pins")
    require(isinstance(pins["images"], dict) and set(pins["images"]) == {"api", "runtime", "web"}
            and all(isinstance(value, str) and IMAGE_SHA.fullmatch(value) for value in pins["images"].values()), "invalid_image_pins")
    require(pins["images"]["web"] == WEB_IMAGE, "unchanged_web_pin_mismatch")
    require(pins["changes"] == {PROCESSOR: PROCESSOR_SHA}, "invalid_source_pins")


def deployment_contract(state):
    require(state.get("schema_changes") is False and state.get("cloud_schema_changes") is False
            and state.get("tenant_schema_migration") is None and all(state.get(key) is False for key in (
                "dependencies_changed", "credentials_changed", "automatic_rollback")), "deployment_contract_mismatch")


def preserved_services(services, original, api_id):
    require(set(services) == set(original) == {"db", "api", "worker", "web", "proxy"}, "service_set_changed")
    for name, value in services.items():
        require(value["running"] is True and value["oom"] is False and value["mounts"] == original[name]["mounts"]
                and (name not in {"db", "api", "web"} or value["health"] == "healthy"), "service_lifecycle_changed")
        if name in {"db", "web", "proxy"}:
            require(all(value[key] == original[name][key] for key in ("id", "image_id", "image")), "preserved_service_changed")
    require(services["api"]["id"] == api_id, "control_identity_changed")


def transcript_proof(before, after):
    original = {item["owner_id"]: item for item in before["tenants"]}
    current = {item["owner_id"]: item for item in after["tenants"]}
    require(len(original) == len(before["tenants"]) and len(current) == len(after["tenants"])
            and original.keys() == current.keys(), "deployment_tenant_set_changed")
    for owner, item in original.items():
        peer = current[owner]
        require(SHA.fullmatch(owner) and item["volume_name"] == peer["volume_name"] == "sxy-data-" + owner[:32], "tenant_volume_changed")
        require(item.get("transcript") == peer.get("transcript"), "original_transcript_changed")
        if item["state"] == "running":
            rows = item.get("transcript")
            require(isinstance(rows, dict) and set(rows) == {"message", "part"}
                    and all(isinstance(row, dict) and set(row) == {"count", "sha256"}
                            and type(row["count"]) is int and row["count"] >= 0
                            and isinstance(row["sha256"], str) and SHA.fullmatch(row["sha256"])
                            for row in rows.values()), "invalid_transcript_proof")
            require(peer["state"] == "running" and peer["generation"] == item["generation"] + 1
                    and peer["runtime_image"] == "suxiaoyou-online-runtime:" + VERSION, "deployment_runtime_changed")
        else:
            require(all(item.get(key) == peer.get(key) for key in (
                "state", "generation", "container_id", "runtime_image")), "inactive_tenant_changed")
    return len(original)


def source_expectation(backend, source_root):
    require(source_root in {"/app/app", "/opt/backend/app"}, "invalid_source_root")
    files = {name: value for name, value in backend.items()
             if source_root == "/opt/backend/app" or not name.startswith("data/")}
    require(files, "empty_source_scope")
    rows = [[name, value] for name, value in sorted(files.items())]
    return files, {"ok": True, "files": len(files), "sha256": digest(json.dumps(rows, separators=(",", ":")).encode())}


SOURCE_PROBE = r'''
import hashlib,json,os,stat,sys
from pathlib import Path,PurePosixPath
request=json.load(sys.stdin);root=Path(request["root"]);rows=[]
if str(root) not in {"/app/app","/opt/backend/app"} or not request["files"]:raise SystemExit(1)
for name,expected in sorted(request["files"].items()):
 parts=PurePosixPath(name).parts
 if not parts or name.startswith("/") or ".." in parts:raise SystemExit(1)
 fd=os.open(root.joinpath(*parts),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,"rb") as source:
  info=os.fstat(source.fileno())
  if not stat.S_ISREG(info.st_mode) or not 0<=info.st_size<=32*1024*1024:raise SystemExit(1)
  actual=hashlib.file_digest(source,"sha256").hexdigest()
 if actual!=expected:raise SystemExit(1)
 rows.append([name,actual])
print(json.dumps({"ok":True,"files":len(rows),"sha256":hashlib.sha256(json.dumps(rows,separators=(",",":")).encode()).hexdigest()},separators=(",",":")))
'''

SCHEMA_PROBE = r'''
import hashlib,json,os,sqlite3,stat,sys
request=json.load(sys.stdin);path="/opt/backend/alembic/versions/0013_v110_context_projection.py"
fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
with os.fdopen(fd,"rb") as source:
 info=os.fstat(source.fileno())
 if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=65536:raise SystemExit(1)
 if hashlib.file_digest(source,"sha256").hexdigest()!=request["migration_sha256"]:raise SystemExit(1)
connection=sqlite3.connect("file:/tenant/state/data/suxiaoyou.db?mode=ro",uri=True)
try:
 connection.execute("BEGIN")
 if connection.execute("SELECT version_num FROM alembic_version").fetchall()!=[("0013_v110_context_projection",)]:raise SystemExit(1)
 if connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_context_projection'").fetchall()!=[("session_context_projection",)]:raise SystemExit(1)
 columns={row[1] for row in connection.execute("PRAGMA table_info(session_context_projection)")}
 if not {"session_id","anchor_message_id","boundary_text","pruned_tool_part_ids"}<=columns:raise SystemExit(1)
finally:connection.close()
print('{"ok":true,"migration_file":true,"head_0013":true,"projection_table":true}')
'''

WEB_PROBE = r'''
const fs=require("fs"),path=require("path"),crypto=require("crypto"),root=path.resolve(process.argv[1]);
const flags={history_refresh:t=>t.includes("Message history changed during refresh"),
 default_deliverables:t=>/fileScope\s*:\s*"deliverables"/.test(t),file_request_revision:t=>t.includes("fileRequestRevision"),
 scoped_files:t=>t.includes("?scope="),composer_control:t=>t.includes("chat-primary-action")};
const found=[],files=[];let bytes=0;
function must(v){if(!v)throw Error("compiled_contract_failed");}
function walk(p){must(fs.lstatSync(p).isDirectory());for(const ent of fs.readdirSync(p,{withFileTypes:true})){const q=path.join(p,ent.name);
 must(!ent.isSymbolicLink());if(ent.isDirectory())walk(q);else if(ent.isFile()&&q.endsWith(".js"))files.push(q);}}
try{
 walk(path.join(root,"static/chunks"));must(files.length<=3000);files.sort();
 for(const p of files){const info=fs.lstatSync(p);bytes+=info.size;must(info.size<=16*1024*1024&&bytes<=256*1024*1024);
  const raw=fs.readFileSync(p),text=raw.toString("utf8"),checks=Object.keys(flags).filter(k=>flags[k](text));
  if(checks.length)found.push({url:"https://nav.cyneu.com:18443/_next/"+path.relative(root,p).split(path.sep).map(encodeURIComponent).join("/"),
   sha256:crypto.createHash("sha256").update(raw).digest("hex"),checks});
 }
 const remaining=new Set(Object.keys(flags)),assets=[];
 while(remaining.size){found.sort((a,b)=>b.checks.filter(k=>remaining.has(k)).length-a.checks.filter(k=>remaining.has(k)).length||a.url.localeCompare(b.url));
  const item=found.shift();must(item&&item.checks.some(k=>remaining.has(k)));for(const k of item.checks)remaining.delete(k);
  assets.push({url:item.url,sha256:item.sha256});
 }
 const build=fs.readFileSync(path.join(root,"BUILD_ID"),"utf8").trim();must(/^[A-Za-z0-9_-]{1,128}$/.test(build));
 console.log(JSON.stringify({ok:true,checks:Object.fromEntries(Object.keys(flags).map(k=>[k,true])),assets}));
}catch(_){console.log('{"ok":false,"error":"compiled_contract_failed"}');process.exitCode=1;}
'''


def compiled_probe(arguments):
    result = decode(command(arguments, maximum=8192))
    required = {"history_refresh", "default_deliverables", "file_request_revision", "scoped_files", "composer_control"}
    require(result.get("ok") is True and set(result.get("checks", {})) == required
            and all(value is True for value in result["checks"].values()), "compiled_contract_failed")
    require(1 <= len(result.get("assets", [])) <= 5 and all(
        re.fullmatch(re.escape(ORIGIN) + r"/_next/static/chunks/[A-Za-z0-9_./%~-]+\.js", item.get("url", ""))
        and SHA.fullmatch(item.get("sha256", "")) for item in result["assets"]), "unsafe_asset_result")
    return result


def container(name):
    template = ('{"id":{{json .Id}},"image_id":{{json .Image}},"image":{{json .Config.Image}},'
                '"running":{{json .State.Running}},"oom":{{json .State.OOMKilled}},'
                '"health":{{with index .State "Health"}}{{json (index . "Status")}}{{else}}null{{end}},"mounts":{{json .Mounts}}}')
    value = decode(docker("container", "inspect", "--format", template, name))
    require(SHA.fullmatch(value.get("id", "")) and IMAGE_SHA.fullmatch(value.get("image_id", "")), "invalid_container_identity")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--pins", type=Path)
    parser.add_argument("--pins-sha256")
    args = parser.parse_args()
    if args.build_root is not None:
        return {"ok": True, "version": VERSION, "local_only": True,
                "web": compiled_probe(["node", "-e", WEB_PROBE, str(args.build_root.resolve())])}
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
            and state.get("controller_sha256") == pins["controller_sha256"] and state.get("runtime_helper_sha256") == pins["runtime_helper_sha256"]
            and state.get("built_images") == pins["images"] and set(state.get("source_changes", [])) == set(pins["changes"]), "active_state_mismatch")
    deployment_contract(state)
    for name, key in (("upgrade-result-permission.py", "controller_sha256"), ("result-permission-runtime.py", "runtime_helper_sha256")):
        require(digest(read(APP / "incoming" / name, 128 * 1024)) == pins[key], "maintenance_source_changed")
    raw = read(RELEASE / "RELEASE-MANIFEST.json", 4 * 1024 * 1024)
    require(digest(raw) == pins["manifest_sha256"], "manifest_identity_mismatch")
    manifest = decode(raw)
    records = {record["path"]: record for record in manifest["files"]}
    require(manifest["source_sha256"] == pins["source_sha256"] and len(records) == len(manifest["files"]) == manifest["source_file_count"], "manifest_contract_mismatch")
    for name, expected in pins["changes"].items():
        record = records[name]
        require(record["sha256"] == expected and digest(read(RELEASE / name, record["size"] + 1)) == expected, "release_source_changed")
    migration = records[MIGRATION]
    require(migration["sha256"] == MIGRATION_SHA and digest(read(RELEASE / MIGRATION, migration["size"] + 1)) == MIGRATION_SHA,
            "existing_migration_changed")
    backend = {name[len("backend/app/"):]: record["sha256"] for name, record in records.items() if name.startswith("backend/app/")}
    require(backend and all(SHA.fullmatch(value) for value in backend.values()), "invalid_backend_manifest")
    before, after = state["before"], state["after"]
    require(before["billing"] == after["billing"] and before["web_auth_sessions"] == after["web_auth_sessions"], "deployment_ledger_changed")
    require(before["runtime_image"] == "suxiaoyou-online-runtime:" + OLD_BACKEND
            and after["runtime_image"] == "suxiaoyou-online-runtime:" + VERSION
            and after["api_image_id"] == pins["images"]["api"], "deployment_baseline_mismatch")
    tenant_count = transcript_proof(before, after)
    image_cache = {}
    def image(reference):
        if reference not in image_cache:
            value = decode(docker("image", "inspect", reference, maximum=256 * 1024))[0]
            require(value.get("Os") == "linux" and value.get("Architecture") == "amd64" and IMAGE_SHA.fullmatch(value.get("Id", "")), "image_contract_failed")
            image_cache[reference] = value
        return image_cache[reference]
    def bind(value, kind):
        tag = ("suxiaoyou-online-runtime:" if kind == "runtime" else "suxiaoyou-cloud-" + kind + ":") + (WEB_VERSION if kind == "web" else VERSION)
        expected = image(tag)
        require(expected["Id"] == pins["images"][kind] and tag in expected.get("RepoTags", []) and value["image"] == tag, "image_pin_mismatch")
        actual = image(value["image_id"])
        require(actual["Config"] == expected["Config"] and actual["RootFS"] == expected["RootFS"], "image_binding_changed")
    services = {name: container(PROJECT + "-" + name + "-1") for name in ("db", "api", "worker", "web", "proxy")}
    preserved_services(services, state["original_services"], after["api_id"])
    for name, value in services.items():
        if name in {"api", "worker", "web"}:
            bind(value, "api" if name == "worker" else name)
    api_files, api_proof = source_expectation(backend, "/app/app")
    tenant_files, tenant_proof = source_expectation(backend, "/opt/backend/app")
    def source_probe(name, root, files, expected):
        payload = json.dumps({"root": root, "files": files}, separators=(",", ":")).encode()
        result = decode(docker("exec", "-i", "--user", "10001:10001", name, "python", "-B", "-c", SOURCE_PROBE, content=payload, maximum=2048))
        require(result == expected, "actual_container_source_changed")
    source_probe(PROJECT + "-api-1", "/app/app", api_files, api_proof)
    names = docker("ps", "--filter", "name=^/sxy-user-", "--format", "{{.Names}}").decode().splitlines()
    require(1 <= len(names) <= 1000 and len(names) == len(set(names)) and all(re.fullmatch(r"sxy-user-[a-f0-9]{32}", name) for name in names), "invalid_runtime_inventory")
    original_running = {"sxy-user-" + item["owner_id"][:32]: item for item in after["tenants"] if item["state"] == "running"}
    require(set(original_running) <= set(names), "original_running_tenant_missing")
    for name in names:
        value = container(name)
        bind(value, "runtime")
        require(value["running"] is True and value["oom"] is False and len(value["mounts"]) == 1
                and value["mounts"][0].get("Type") == "volume" and value["mounts"][0].get("Name") == "sxy-data-" + name[len("sxy-user-"):]
                and value["mounts"][0].get("Destination") == "/tenant", "runtime_mount_or_lifecycle_changed")
        if name in original_running:
            require(value["id"] == original_running[name]["container_id"], "original_runtime_restarted")
        source_probe(name, "/opt/backend/app", tenant_files, tenant_proof)
        result = decode(docker("exec", "-i", "--user", "10001:10001", name, "python", "-B", "-c", SCHEMA_PROBE,
                               content=json.dumps({"migration_sha256": MIGRATION_SHA}).encode(), maximum=2048))
        require(result == {"ok": True, "migration_file": True, "head_0013": True, "projection_table": True}, "runtime_schema_mismatch")
    web = compiled_probe(PREFIX + ["exec", "--user", "10001:10001", PROJECT + "-web-1", "node", "-e", WEB_PROBE, "/app/.next"])
    return {"ok": True, "version": VERSION, "active_state": True, "source_pins": True, "image_pins": True,
            "original_transcripts_preserved": True, "deployment_ledger_preserved": True, "schema_0013": True,
            "web_proxy_db_preserved": True, "permission_patch_only": True,
            "tenants_at_deployment": tenant_count, "running_runtimes": len(names),
            "source": {"api": api_proof, "runtime": tenant_proof}, "web": web}


if __name__ == "__main__":
    try:
        result = main()
        raw = json.dumps(result, separators=(",", ":"))
        require(len(raw.encode()) <= 6000, "safe_output_budget_exceeded")
        status = 0
    except BaseException as error:
        raw = json.dumps({"ok": False, "version": VERSION,
                          "error": str(error) if isinstance(error, Failure) else "verification_failed"}, separators=(",", ":"))
        status = 1
    print("SXY_RESULT_PERMISSION_PROVENANCE:" + raw)
    raise SystemExit(status)
