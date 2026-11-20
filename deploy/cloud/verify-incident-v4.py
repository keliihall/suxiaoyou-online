#!/usr/bin/env python3
"""Read-only provenance check v4 after 20261117-incident2 activation.

Run as root. Docker calls use the existing suxiaoyou account/rootless socket.
No provider, HTTP, login, database, configuration write or application action
is invoked. Never read environment/key files or container logs. Stdout contains
at most 1800 bytes of safe JSON, including public compiled asset paths/hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

VERSION = "20261117-incident2"
APP = Path("/srv/suxiaoyou")
RELEASE = APP / "releases" / VERSION
CONFIG = APP / "shared/config" / VERSION
ARCHIVE_SHA256 = "0021fdaa6e0099df84b77f68d70f21339a3521e4a3f9a8f33885f0aa68b5ec03"
SOURCE_SHA256 = "c9fe2c578b34660f841cf2ba8679db9b5ff4902230e134f12f0b93b95cac779d"
MANIFEST_SHA256 = "d3f6529c471ed3b3110cd9b64cfd792ca18ac07043f833824122f63d48dd19e9"
CONTROLLER_SHA256 = "e4e764c506bd4b39cd7df1ab1f449bdf0bd58c1d7d6c608562c19a87f1ec3c53"
HELPER_SHA256 = "d589d5d716da6034dd69e9e2761288556c0a756d08a6c9a9a7f7d913b9fd90c4"
CHANGED_FILES = ("backend/app/cloud/gateway_provider.py", "backend/app/cloud/inference.py")
EXPECTED_IMAGES = {"api": "sha256:cc0d81fd35c73648b24ff2de58b58191f2ed2c606d358e3dcce3915353bcec08", "runtime": "sha256:009335107e865474a823d19615a26d31b60783497d104a4b7941e7caee9de4d7", "web": "sha256:48bbdd122b426d0283a799cbd0ab408293d524bb3608925e285114f7ae08cf82"}
PROJECT = "suxiaoyou-cloud"
SHA = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
PREFIX = ["runuser", "--user", "suxiaoyou", "--", "env",
          "HOME=/home/suxiaoyou", "XDG_RUNTIME_DIR=/run/user/1001",
          "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
          "DOCKER_HOST=unix:///run/user/1001/docker.sock", "docker"]


class Failure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Failure(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, maximum, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1
                and not info.st_mode & 0o022 and 0 <= info.st_size <= maximum
                and (not private or stat.S_IMODE(info.st_mode) == 0o600), "unsafe_provenance_file")
        raw = handle.read(maximum + 1)
    require(len(raw) == info.st_size, "provenance_file_changed")
    return raw


def docker(*args, content=None, maximum=1024 * 1024, timeout=90):
    result = subprocess.run(PREFIX + list(args), input=content, stdin=subprocess.DEVNULL if content is None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    require(result.returncode == 0 and len(result.stdout) <= maximum, "docker_read_failed")
    return result.stdout


def container(name):
    # index handles absent Health without reading or returning healthcheck logs.
    template = ('{"id":{{json .Id}},"image_id":{{json .Image}},"image":{{json .Config.Image}},'
                '"running":{{json .State.Running}},"oom":{{json .State.OOMKilled}},'
                '"health":{{with index .State "Health"}}{{json (index . "Status")}}{{else}}null{{end}},'
                '"mounts":{{json .Mounts}}}')
    value = json.loads(docker("container", "inspect", "--format", template, name))
    require(SHA.fullmatch(value.get("id", "")) and IMAGE_SHA.fullmatch(value.get("image_id", "")),
            "invalid_container_identity")
    return value


SOURCE_PROBE = r'''
import hashlib,json,os,stat,sys
from pathlib import Path,PurePosixPath
request=json.load(sys.stdin); root=Path(request["root"]); expected=request["files"]
if str(root) not in {"/app/app","/opt/backend/app"} or not expected: raise SystemExit(1)
checked=[]
for name,identity in sorted(expected.items()):
 parts=PurePosixPath(name).parts
 if not parts or name.startswith("/") or ".." in parts: raise SystemExit(1)
 descriptor=os.open(root.joinpath(*parts),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(descriptor,"rb") as source:
  info=os.fstat(source.fileno())
  if not stat.S_ISREG(info.st_mode) or not 0<=info.st_size<=32*1024*1024: raise SystemExit(1)
  actual=hashlib.file_digest(source,"sha256").hexdigest()
 if actual!=identity: raise SystemExit(1)
 checked.append([name,actual])
print(json.dumps({"ok":True,"files":len(checked),"sha256":hashlib.sha256(json.dumps(checked,separators=(",",":")).encode()).hexdigest()},separators=(",",":")))
'''


WEB_PROBE = r'''
const fs=require("fs"), path=require("path"), crypto=require("crypto");
const root="/app/.next", dir=path.join(root,"static/chunks");
const required={output_code:"model_output_exhausted",context_code:"model_context_too_long",
 request_field:"request_id",zh_reference:"请求编号：{{requestId}}",en_reference:"Request ID: {{requestId}}"};
const files=[]; let bytes=0;
function walk(p){for(const ent of fs.readdirSync(p,{withFileTypes:true}).sort((a,b)=>a.name.localeCompare(b.name))){
 const q=path.join(p,ent.name); if(ent.isSymbolicLink())throw Error("link");
 if(ent.isDirectory())walk(q); else if(ent.isFile()&&ent.name.endsWith(".js"))files.push(q);
}}
function present(text,needle){const escaped=Array.from(needle,c=>c.charCodeAt(0)>127?"\\u"+c.charCodeAt(0).toString(16).padStart(4,"0"):c).join("");
 return text.includes(needle)||text.toLowerCase().includes(escaped.toLowerCase());}
walk(dir); if(files.length>3000)throw Error("files"); const hits=[];
for(const p of files){const size=fs.statSync(p).size; bytes+=size;if(size>16*1024*1024||bytes>256*1024*1024)throw Error("budget");
 const raw=fs.readFileSync(p),text=raw.toString("utf8"),checks=Object.keys(required).filter(k=>present(text,required[k]));
 if(checks.length)hits.push({path:"/_next/"+path.relative(root,p).split(path.sep).map(encodeURIComponent).join("/"),sha256:crypto.createHash("sha256").update(raw).digest("hex"),checks});
}
const remaining=new Set(Object.keys(required)),assets=[];
while(remaining.size){hits.sort((a,b)=>b.checks.filter(k=>remaining.has(k)).length-a.checks.filter(k=>remaining.has(k)).length||a.path.localeCompare(b.path));
 const selected=hits.shift();if(!selected||!selected.checks.some(k=>remaining.has(k)))throw Error("feature");
 for(const k of selected.checks)remaining.delete(k);assets.push({path:selected.path,sha256:selected.sha256});
}
const build_id=fs.readFileSync(path.join(root,"BUILD_ID"),"utf8").trim();
if(!/^[A-Za-z0-9_-]{1,128}$/.test(build_id)||assets.length>4)throw Error("build");
console.log(JSON.stringify({ok:true,build_id,checks:Object.fromEntries(Object.keys(required).map(k=>[k,true])),assets}));
'''


def source_expectation(backend, source_root):
    require(source_root in {"/app/app", "/opt/backend/app"}, "invalid_source_root")
    # The pinned API recipe excludes backend/app/data/; the full tenant
    # runtime includes it. All shared application code stays in both scopes.
    expected = {name: identity for name, identity in backend.items()
                if source_root == "/opt/backend/app" or not name.startswith("data/")}
    require(expected, "source_scope_empty")
    checked = [[name, identity] for name, identity in sorted(expected.items())]
    proof = {"ok": True, "files": len(checked),
             "sha256": digest(json.dumps(checked, separators=(",", ":")).encode())}
    return expected, proof


def main():
    require(os.getuid() == os.geteuid() == 0, "root_required")
    require(all(SHA.fullmatch(value) for value in (ARCHIVE_SHA256, SOURCE_SHA256, MANIFEST_SHA256, CONTROLLER_SHA256, HELPER_SHA256))
            and set(EXPECTED_IMAGES) == {"api", "runtime", "web"}
            and all(IMAGE_SHA.fullmatch(value) for value in EXPECTED_IMAGES.values()), "verifier_pin_not_finalized")
    require((APP / "current").is_symlink() and (APP / "current").resolve() == RELEASE, "current_release_mismatch")
    state = json.loads(read(CONFIG / "upgrade-state.json", 8 * 1024 * 1024, private=True))
    require(state.get("version") == VERSION and state.get("phase") == "active"
            and state.get("source_archive_sha256") == ARCHIVE_SHA256
            and state.get("source_sha256") == SOURCE_SHA256
            and state.get("controller_sha256") == CONTROLLER_SHA256
            and state.get("runtime_helper_sha256") == HELPER_SHA256, "active_state_mismatch")
    require(digest(read(APP / "incoming/upgrade-incident-reasoning.py", 128 * 1024)) == CONTROLLER_SHA256
            and digest(read(APP / "incoming/incident-runtime-v2.py", 128 * 1024)) == HELPER_SHA256,
            "controller_or_helper_changed")
    raw = read(RELEASE / "RELEASE-MANIFEST.json", 4 * 1024 * 1024)
    require(digest(raw) == MANIFEST_SHA256, "manifest_identity_mismatch")
    manifest = json.loads(raw)
    require(manifest["source_sha256"] == SOURCE_SHA256 and manifest["source_file_count"] == 1233,
            "manifest_contract_mismatch")
    records = {record["path"]: record for record in manifest["files"]}
    require(set(state["source_changes"]) == set(CHANGED_FILES), "source_change_provenance_mismatch")
    for name in CHANGED_FILES:
        record = records[name]
        require(digest(read(RELEASE / name, record["size"] + 1)) == record["sha256"], "release_source_changed")
    backend = {}
    for record in manifest["files"]:
        name = record["path"]
        if name.startswith("backend/app/"):
            require(SHA.fullmatch(record["sha256"]), "invalid_source_digest")
            backend[name[len("backend/app/"):]] = record["sha256"]
    require(backend, "backend_source_missing")
    originals, images = state["original_services"], state["built_images"]
    require(images == EXPECTED_IMAGES, "built_image_provenance_mismatch")
    services = {name: container(PROJECT + "-" + name + "-1") for name in ("db", "api", "worker", "web", "proxy")}
    for name, value in services.items():
        kind = "api" if name == "worker" else name
        tag = "suxiaoyou-cloud-" + kind + ":" + VERSION if kind in {"api", "web"} else originals[name]["image"]
        identity = images[kind] if kind in {"api", "web"} else originals[name]["image_id"]
        require(value["running"] is True and value["oom"] is False
                and value["image"] == tag and value["image_id"] == identity, "service_image_or_lifecycle_mismatch")
        require(name not in {"db", "api", "web"} or value["health"] == "healthy", "service_unhealthy")
        require(value["mounts"] == originals[name]["mounts"], "service_mount_changed")
    require(services["db"]["id"] == originals["db"]["id"], "database_container_changed")
    before, after = state["before"], state["after"]
    require(before["billing"] == after["billing"] and before["web_auth_sessions"] == after["web_auth_sessions"],
            "deployment_data_invariant_failed")
    original_tenants = {t["owner_id"]: t for t in before["tenants"]}
    require(set(original_tenants) == {t["owner_id"] for t in after["tenants"]}, "deployment_tenant_set_changed")
    running = []
    for tenant in after["tenants"]:
        owner, original = tenant["owner_id"], original_tenants[tenant["owner_id"]]
        require(SHA.fullmatch(owner) and tenant["volume_name"] == original["volume_name"] == "sxy-data-" + owner[:32],
                "tenant_volume_changed")
        require(json.loads(docker("volume", "inspect", "--format", "{{json .Name}}", tenant["volume_name"]))
                == tenant["volume_name"], "tenant_volume_missing")
        if tenant["state"] != "running":
            continue
        name = "sxy-user-" + owner[:32]
        value = container(name)
        require(value["id"] == tenant["container_id"] and value["image_id"] == tenant["image_id"]
                and value["image"] == "suxiaoyou-online-runtime:" + VERSION
                and value["running"] is True and value["oom"] is False, "tenant_runtime_changed")
        mounts = value["mounts"]
        require(len(mounts) == 1 and mounts[0].get("Type") == "volume"
                and mounts[0].get("Name") == tenant["volume_name"] and mounts[0].get("Destination") == "/tenant",
                "tenant_mount_changed")
        running.append(name)
    api_expected, api_proof = source_expectation(backend, "/app/app")
    tenant_expected, tenant_proof = source_expectation(backend, "/opt/backend/app")
    require(len(api_expected) == 413 and len(tenant_expected) == 871
            and all(tenant_expected.get(name) == identity for name, identity in api_expected.items()),
            "source_manifest_scope_mismatch")
    for name, source_root in [(PROJECT + "-api-1", "/app/app")] + [(name, "/opt/backend/app") for name in running]:
        expected, expected_proof = (api_expected, api_proof) if source_root == "/app/app" else (tenant_expected, tenant_proof)
        payload = json.dumps({"root": source_root, "files": expected}, separators=(",", ":")).encode()
        proof = json.loads(docker("exec", "-i", "--user", "10001:10001", name, "python", "-B", "-c", SOURCE_PROBE,
                                  content=payload, maximum=2048, timeout=180))
        require(proof == expected_proof, "runtime_source_mismatch")
    web = json.loads(docker("exec", "--user", "10001:10001", PROJECT + "-web-1", "node", "-e", WEB_PROBE,
                            maximum=6000, timeout=180))
    require(web.get("ok") is True and web.get("checks") and all(web["checks"].values()), "compiled_feature_missing")
    return {"ok": True, "version": VERSION, "services": 5, "tenants": len(running),
            "active_state": True, "mounts_preserved": True, "deployment_ledger_preserved": True,
            "source": {"api_files": len(api_expected), "tenant_files": len(tenant_expected), "tenant_containers": len(running),
                       "shared_code_files": len(api_expected), "api_sha256": api_proof["sha256"], "tenant_sha256": tenant_proof["sha256"]},
            "web": web}


if __name__ == "__main__":
    try:
        result = main()
        raw = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        require(len(raw.encode()) + len("SXY_INCIDENT_PROVENANCE:") <= 1800, "safe_output_budget_exceeded")
    except BaseException as error:
        raw = json.dumps({"ok": False, "version": VERSION,
                          "error": str(error) if isinstance(error, Failure) else "verification_failed"}, separators=(",", ":"))
    print("SXY_INCIDENT_PROVENANCE:" + raw)
