#!/usr/bin/env python3
"""Read-only provenance check after 20261117-incident activation.

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

VERSION = "20261117-incident"
APP = Path("/srv/suxiaoyou")
RELEASE = APP / "releases" / VERSION
CONFIG = APP / "shared/config" / VERSION
ARCHIVE_SHA256 = "657c0e84901b429c4f3e9851d36e318869d8c8e72cc414d2bb4993b5a311d0e3"
SOURCE_SHA256 = "a54102c9a5e98c23430560bd8c60e0b4563ac6fddb23b0b67adc96faf3c8a76a"
MANIFEST_SHA256 = "1f2cfe9d39a74764fa8922d724dd2294504f8afe44e1dc3a5ecd0a2a4b1afb4c"
CONTROLLER_SHA256 = "3577adca6ac2f0422a538b49d138a4f71f4dcd28b62d5a37e705adbc7807890c"
HELPER_SHA256 = "2982b138814c81712de88a1969d00c90ad13d61aadd73a23bd2a6cb972d25a03"
CHANGED_FILES = ('backend/app/cloud/gateway_errors.py', 'backend/app/cloud/gateway_provider.py', 'backend/app/cloud/inference.py', 'backend/app/cloud/providers.py', 'backend/app/cloud/runtime_entry.py', 'backend/app/provider/openai_compat.py', 'backend/app/schemas/provider.py', 'backend/app/session/compaction.py', 'backend/app/session/goal_controller.py', 'backend/app/session/processor.py', 'backend/app/session/prompt.py', 'backend/app/session/retry.py', 'frontend/src/i18n/locales/en/chat.json', 'frontend/src/i18n/locales/zh/chat.json', 'frontend/src/lib/session-stream-registry.ts', 'frontend/src/lib/stream-error.ts', 'frontend/src/types/streaming.ts')
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


def main():
    require(os.getuid() == os.geteuid() == 0, "root_required")
    require(SHA.fullmatch(MANIFEST_SHA256), "verifier_pin_not_finalized")
    require((APP / "current").is_symlink() and (APP / "current").resolve() == RELEASE, "current_release_mismatch")
    state = json.loads(read(CONFIG / "upgrade-state.json", 8 * 1024 * 1024, private=True))
    require(state.get("version") == VERSION and state.get("phase") == "active"
            and state.get("source_archive_sha256") == ARCHIVE_SHA256
            and state.get("source_sha256") == SOURCE_SHA256
            and state.get("controller_sha256") == CONTROLLER_SHA256
            and state.get("runtime_helper_sha256") == HELPER_SHA256, "active_state_mismatch")
    require(digest(read(APP / "incoming/upgrade-incident.py", 128 * 1024)) == CONTROLLER_SHA256
            and digest(read(APP / "incoming/incident-runtime.py", 128 * 1024)) == HELPER_SHA256,
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
    source_results = []
    for name, source_root in [(PROJECT + "-api-1", "/app/app")] + [(name, "/opt/backend/app") for name in running]:
        payload = json.dumps({"root": source_root, "files": backend}, separators=(",", ":")).encode()
        proof = json.loads(docker("exec", "-i", "--user", "10001:10001", name, "python", "-B", "-c", SOURCE_PROBE,
                                  content=payload, maximum=2048, timeout=180))
        require(proof.get("ok") is True and proof.get("files") == len(backend) and SHA.fullmatch(proof.get("sha256", "")),
                "runtime_source_mismatch")
        source_results.append(proof)
    require(all(proof == source_results[0] for proof in source_results), "runtime_source_proofs_differ")
    web = json.loads(docker("exec", "--user", "10001:10001", PROJECT + "-web-1", "node", "-e", WEB_PROBE,
                            maximum=6000, timeout=180))
    require(web.get("ok") is True and web.get("checks") and all(web["checks"].values()), "compiled_feature_missing")
    return {"ok": True, "version": VERSION, "services": 5, "tenants": len(running),
            "active_state": True, "mounts_preserved": True, "deployment_ledger_preserved": True,
            "source": {"files": len(backend), "containers": len(source_results), "sha256": source_results[0]["sha256"]},
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
