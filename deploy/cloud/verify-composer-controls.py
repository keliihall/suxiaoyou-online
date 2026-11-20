#!/usr/bin/env python3
"""Read-only web-only composer release verification.

Run on the server as root with the reviewed web image/controller digests. No
HTTP, model, database, tenant exec, environment file or container log is read.
The local mode checks an existing production Next build without Docker.
Only public asset URLs/digests and bounded Boolean results reach stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

VERSION = "20261117-composer"
OLD_VERSION = "20261117-incident2"
APP = Path("/srv/suxiaoyou")
RELEASE = APP / "releases" / VERSION
CONFIG = APP / "shared/config" / VERSION
ARCHIVE_SHA256 = "ae6345ae08b3c190aede20b04d5a0d0d2e5d084034a19cb023421fad07a52a13"
SOURCE_SHA256 = "53da546ba915a532ca685a4f0d335af1c4b26e89a11ddc8d54a1673b1b6fdbe8"
MANIFEST_SHA256 = "25a5fd53f03d8912143257e0eec378bc00d414cd31bfddcd2090be35c49c48a6"
ACTION = "frontend/src/components/chat/chat-actions.tsx"
ACTION_SHA256 = "3049edc15c79902c4cab416f25faf2d5f10778ee0bd70635cbcaa883ee9aabae"
PROJECT = "suxiaoyou-cloud"
PUBLIC_ORIGIN = "https://nav.cyneu.com:18443"
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


def command(arguments, *, timeout=90, maximum=8192):
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    require(result.returncode == 0 and len(result.stdout) <= maximum, "read_operation_failed")
    return result.stdout


def docker(*arguments, maximum=8192):
    return command(PREFIX + list(arguments), maximum=maximum)


def container(name):
    # Inspect only lifecycle/identity fields; exclude Config.Env and health logs.
    template = ('{"id":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
                '"image":{{json .Config.Image}},"started_at":{{json .State.StartedAt}},'
                '"running":{{json .State.Running}},"oom":{{json .State.OOMKilled}},'
                '"health":{{with index .State "Health"}}{{json (index . "Status")}}{{else}}null{{end}},'
                '"mounts":{{json .Mounts}}}')
    value = json.loads(docker("container", "inspect", "--format", template, name, maximum=65536))
    require(SHA.fullmatch(value.get("id", "")) and IMAGE_SHA.fullmatch(value.get("image_id", "")),
            "invalid_container_identity")
    return value


WEB_PROBE = r'''
const fs=require("fs"),path=require("path"),crypto=require("crypto");
const root=path.resolve(process.argv[1]), origin="https://nav.cyneu.com:18443";
const files=[];let bytes=0;
function must(ok){if(!ok)throw Error("compiled_contract_failed");}
function walk(p){must(fs.lstatSync(p).isDirectory());for(const ent of fs.readdirSync(p,{withFileTypes:true}).sort((a,b)=>a.name.localeCompare(b.name))){
 const q=path.join(p,ent.name);must(!ent.isSymbolicLink());
 if(ent.isDirectory())walk(q);else if(ent.isFile()&&/\.(js|css)$/.test(ent.name))files.push(q);
}}
function asset(p,raw){return {url:origin+"/_next/"+path.relative(root,p).split(path.sep).map(encodeURIComponent).join("/"),
 sha256:crypto.createHash("sha256").update(raw).digest("hex")};}
function escape(s){return s.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");}
try{
 walk(path.join(root,"static"));must(files.length<=4000);
 const js=[],css=[];
 for(const p of files){const size=fs.lstatSync(p).size;bytes+=size;must(size<=16*1024*1024&&bytes<=256*1024*1024);
  const raw=fs.readFileSync(p),text=raw.toString("utf8");
  if(p.endsWith(".js")&&text.includes('"chat-primary-action"'))js.push({p,raw,text});
  if(p.endsWith(".css")&&text.includes(".size-8{")&&text.includes(".h-11{")&&text.includes(".w-11{")&&text.includes(".rounded-full{"))css.push({p,raw,text});
 }
 must(js.length===1&&css.length>=1);
 const {p,raw,text}=js[0], marker='"chat-primary-action"', at=text.indexOf(marker);
 must(text.indexOf(marker,at+marker.length)===-1);
 const prefix=text.slice(Math.max(0,at-4096),at);
 const starts=[...prefix.matchAll(/function\s+[\w$]+\(([\w$]+)\)\{let\{isBusy:([\w$]+),canSend:([\w$]+),onSend:([\w$]+),onStop:([\w$]+),[^{}]{0,256}\}=\1,/g)];
 must(starts.length>=1);const start=starts[starts.length-1];
 let block=text.slice(Math.max(0,at-4096)+start.index);const next=block.indexOf("function ",9);
 block=block.slice(0,next<0?4096:next);must(block.length<6000&&block.includes(marker));
 const busy=escape(start[2]),can=escape(start[3]),send=escape(start[4]),stop=escape(start[5]);
 const label=block.match(/"aria-label":([\w$]+)/);must(label);
 const checks={
  single_primary_action:(block.match(/"data-testid":"chat-primary-action"/g)||[]).length===1&&(block.match(/onClick:/g)||[]).length===1,
  stop_send_same_button:new RegExp('"data-action":'+busy+'\\?"stop":"send"').test(block),
  busy_dispatches_only_stop:new RegExp('onClick:'+busy+'\\?'+stop+':'+send+'(?=,)').test(block),
  stop_remains_enabled:new RegExp('disabled:!'+busy+'&&!'+can+'(?=,)').test(block),
  busy_stop_label:new RegExp('(?:,|let )'+escape(label[1])+'='+busy+'\\?[\\w$]+\\("stopAction"\\):').test(block),
  button_type: block.includes('type:"button"'),
  touch_target_44: block.includes('h-11 min-h-11 w-11 shrink-0 rounded-full'),
  visual_circle_32: block.includes('flex size-8 items-center justify-center rounded-full'),
  icon_switch: new RegExp('children:'+busy+'\\?\\(0,').test(block)&&block.includes('size-3 fill-current')&&block.includes('className:"size-4"'),
  sr_label: block.includes('className:"sr-only",children:'+label[1]),
  compiled_css: true
 };
 must(Object.values(checks).every(v=>v===true));
 const build=fs.readFileSync(path.join(root,"BUILD_ID"),"utf8").trim();must(/^[A-Za-z0-9_-]{1,128}$/.test(build));
 console.log(JSON.stringify({ok:true,checks,assets:[asset(p,raw),asset(css[0].p,css[0].raw)]}));
}catch(_){console.log(JSON.stringify({ok:false,error:"compiled_contract_failed"}));process.exitCode=1;}
'''


def probe(arguments):
    web = json.loads(command(arguments, timeout=180))
    require(web.get("ok") is True and web.get("checks")
            and all(value is True for value in web["checks"].values()), "compiled_contract_failed")
    require(len(web.get("assets", [])) == 2 and all(
        isinstance(item, dict) and item.get("url", "").startswith(PUBLIC_ORIGIN + "/_next/static/")
        and SHA.fullmatch(item.get("sha256", "")) for item in web["assets"]), "unsafe_asset_result")
    return web


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-web-image")
    parser.add_argument("--expected-controller-sha256")
    parser.add_argument("--local-next-root", type=Path)
    args = parser.parse_args()
    if args.local_next_root is not None:
        return {"ok": True, "version": VERSION, "local_only": True,
                "web": probe(["node", "-e", WEB_PROBE, str(args.local_next_root.resolve())])}
    require(os.getuid() == os.geteuid() == 0, "root_required")
    require(IMAGE_SHA.fullmatch(args.expected_web_image or "")
            and SHA.fullmatch(args.expected_controller_sha256 or ""), "verifier_pin_not_finalized")
    require((APP / "current").is_symlink() and (APP / "current").resolve() == RELEASE, "current_release_mismatch")
    state = json.loads(read(CONFIG / "upgrade-state.json", 8 * 1024 * 1024, private=True))
    require(state.get("version") == VERSION and state.get("old_version") == OLD_VERSION
            and state.get("phase") == "active" and state.get("source_archive_sha256") == ARCHIVE_SHA256
            and state.get("source_sha256") == SOURCE_SHA256
            and state.get("controller_sha256") == args.expected_controller_sha256
            and state.get("built_web_image") == args.expected_web_image
            and state.get("source_changes") == [ACTION], "active_state_mismatch")
    require(digest(read(APP / "incoming/upgrade-composer-controls.py", 128 * 1024))
            == args.expected_controller_sha256, "controller_identity_mismatch")
    manifest_raw = read(RELEASE / "RELEASE-MANIFEST.json", 4 * 1024 * 1024)
    require(digest(manifest_raw) == MANIFEST_SHA256, "manifest_identity_mismatch")
    manifest = json.loads(manifest_raw)
    require(manifest.get("source_sha256") == SOURCE_SHA256, "manifest_contract_mismatch")
    require(digest(read(RELEASE / ACTION, 65536)) == ACTION_SHA256, "action_source_changed")
    require(all(state.get(key) is False for key in (
        "schema_changes", "dependencies_changed", "credentials_changed", "automatic_rollback"))
        and state.get("model_requests") == 0 and state.get("runtime_restarts") == 0
        and state.get("non_web_containers_preserved") is True
        and state.get("tenant_containers_preserved") is True, "web_only_state_mismatch")
    identity = ("id", "name", "image", "image_id", "started_at", "mounts")
    for name in ("db", "api", "worker", "web", "proxy"):
        current = container(PROJECT + "-" + name + "-1")
        expected = state["services_after"][name]
        require(all(current.get(key) == expected.get(key) for key in identity)
                and current["running"] is True and current["oom"] is False
                and (name not in {"db", "api", "web"} or current["health"] == "healthy"),
                "active_container_changed")
        if name == "web":
            require(current["image"] == "suxiaoyou-cloud-web:" + VERSION
                    and current["image_id"] == args.expected_web_image, "web_image_mismatch")
        else:
            for stage in ("original_services", "activation_services"):
                require(all(current.get(key) == state[stage][name].get(key) for key in identity),
                        "non_web_container_changed")
    web = probe(PREFIX + ["exec", "--user", "10001:10001", PROJECT + "-web-1",
                          "node", "-e", WEB_PROBE, "/app/.next"])
    return {"ok": True, "version": VERSION, "active_state": True, "source_pins": True,
            "web_image": True, "non_web_containers_preserved": True,
            "state_tenant_preservation": True, "web": web}


if __name__ == "__main__":
    try:
        result = main()
        raw = json.dumps(result, separators=(",", ":"))
        require(len(raw.encode()) <= 1800, "safe_output_budget_exceeded")
        status = 0
    except BaseException as error:
        raw = json.dumps({"ok": False, "version": VERSION,
                          "error": str(error) if isinstance(error, Failure) else "verification_failed"},
                         separators=(",", ":"))
        status = 1
    print("SXY_COMPOSER_PROVENANCE:" + raw)
    raise SystemExit(status)
