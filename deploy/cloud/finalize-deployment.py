#!/usr/bin/env python3
"""One-shot reviewed production seed and administrative runtime acceptance.

Run remotely as root or suxiaoyou (1001); private seed JSON arrives on stdin.
No seed file is created or removed. Children always run as suxiaoyou. Captured
output is validated and reduced to public status/counts; raw subprocess output,
arguments, seed data, passwords and model credentials are never logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
from types import ModuleType


APP = Path("/srv/suxiaoyou")
INCOMING = APP / "incoming"
VERSION = "20261115-2144"
ORIGIN = "https://192.168.1.211:8443"
RELEASE = APP / "releases" / VERSION
CONFIG = APP / "shared/config" / VERSION
SAFE_LOG = APP / "shared/logs" / (VERSION + "-finalization.json")
USER, UID = "suxiaoyou", 1001
MODEL_ID = "deepseek-office"
REPORT_NAME = "deployment-runtime-check.json"
MAX_INPUT = 64 * 1024
REVIEWED = {
    "deploy-release-pinned.py": "4fd0363428249a2de992a884f48f703d4a59ddd1e83b1ab2e84a62df67f18ad8",
    "seed-deployment.py": "cd034d4ce607774c91d9ae0f5ec167839e3be2fbf3ffa1286dc8f31b94d0f165",
    "bootstrap-seed.py": "09f58a3d492d411e3aea4670567dd6eb50f0f13460d758f8c670fcfc103162ef",
    "runtime-deployment-check.py": "b6db12dd932f439799611d4e54d5d99086fd36b035ce718db13610876e88cd54",
}


class FinalizationFailure(Exception):
    pass


def require(condition):
    if not condition:
        raise FinalizationFailure


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def invalid_constant(_value):
    raise FinalizationFailure


def document(raw):
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def read_regular(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == UID and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and 0 < info.st_size <= maximum)
        raw = handle.read(maximum + 1)
    require(len(raw) == info.st_size and len(raw) <= maximum)
    return raw


def safe_directory(path, *, private=True):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == UID
            and not stat.S_IMODE(info.st_mode) & 0o022
            and (not private or stat.S_IMODE(info.st_mode) == 0o700))


def reviewed_sources():
    sources = {}
    for name, expected in REVIEWED.items():
        raw = read_regular(INCOMING / name, 128 * 1024)
        require(hashlib.sha256(raw).hexdigest() == expected)
        sources[name] = raw
    module = ModuleType("suxiaoyou_finalization_reviewed_deployment")
    module.__file__ = str(INCOMING / "deploy-release-pinned.py")
    exec(compile(sources["deploy-release-pinned.py"], module.__file__, "exec"), module.__dict__)
    require(module.APP == APP and module.RELEASE == RELEASE and module.CONFIG == CONFIG
            and module.ORIGIN == ORIGIN and module.DEPLOY_USER == USER and module.DEPLOY_UID == UID
            and module.PROJECT == "suxiaoyou-cloud" and callable(module.compose)
            and callable(module.command_environment))
    return sources, module


def read_seed():
    require(not sys.stdin.isatty())
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    require(0 < len(raw) <= MAX_INPUT)
    seed = document(raw)
    require(isinstance(seed, dict) and set(seed) == {"schema_version", "admin", "routes"}
            and type(seed["schema_version"]) is int and seed["schema_version"] == 1
            and isinstance(seed["admin"], dict) and isinstance(seed["routes"], list)
            and 1 <= len(seed["routes"]) <= 8
            and any(isinstance(route, dict) and route.get("id") == MODEL_ID for route in seed["routes"]))
    return raw, len(seed["routes"])


def save_summary(handle, summary):
    raw = (json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n").encode()
    handle.seek(0)
    handle.write(raw)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def runtime_summary(raw):
    require(0 < len(raw) <= MAX_INPUT)
    value = document(raw)
    require(isinstance(value, dict) and value.get("complete") is True
            and value.get("web_authentication") == "pending_human_browser_login"
            and type(value.get("prompt_submissions")) is int and value["prompt_submissions"] == 1
            and type(value.get("provider_attempts")) is int and 1 <= value["provider_attempts"] <= 200
            and type(value.get("total_tokens")) is int and value["total_tokens"] > 0
            and value.get("private_report_name") == REPORT_NAME
            and isinstance(value.get("checks"), dict) and value["checks"]
            and all(isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key) and result is True
                    for key, result in value["checks"].items()))
    return {"complete": True, "prompt_submissions": 1,
            "provider_attempts": value["provider_attempts"], "total_tokens": value["total_tokens"],
            "checks": value["checks"], "private_report_name": REPORT_NAME}


def finalize():
    os.umask(0o077)
    require(len(sys.argv) == 1 and os.getuid() == os.geteuid() and os.getuid() in {0, UID})
    account = pwd.getpwnam(USER)
    require(account.pw_uid == UID)
    if os.getuid() == UID:
        require(pwd.getpwuid(UID).pw_name == USER)
    for path in (APP, INCOMING):
        safe_directory(path, private=False)
    for path in (RELEASE, CONFIG, APP / "shared/logs"):
        safe_directory(path)
    sources, module = reviewed_sources()
    state = document(read_regular(CONFIG / "deployment-result.json", MAX_INPUT))
    require(state.get("version") == VERSION and state.get("origin") == ORIGIN
            and state.get("ready") is True and state.get("admin_seeded") is False
            and state.get("model_seeded") is False)
    seed_bytes, route_count = read_seed()
    prefix = ["/usr/sbin/runuser", "--user", USER, "--"] if os.getuid() == 0 else []
    environment = module.command_environment()
    descriptor = os.open(SAFE_LOG, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    summary = {"version": VERSION, "origin": ORIGIN, "stage": "started", "admin_seeded": False,
               "model_seeded": False, "expected_admin_count": 1, "expected_route_count": route_count,
               "runtime_attempted": False, "runtime_complete": False,
               "web_authentication": "pending_human_browser_login", "automatic_retries": False}
    if os.getuid() == 0:
        os.fchown(descriptor, UID, account.pw_gid)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w+b") as output:
        try:
            save_summary(output, summary)
            # The controller checks the deployment lock and fresh DB, and the
            # private seed travels only through this child's stdin.
            seeded = subprocess.run(
                [*prefix, "python3", "-c", sources["seed-deployment.py"].decode("utf-8")],
                cwd=RELEASE, env=environment, input=seed_bytes, capture_output=True,
                timeout=120, check=False,
            )
            del seed_bytes
            require(seeded.returncode == 0 and not seeded.stderr.strip()
                    and 0 < len(seeded.stdout) <= 4096)
            require(document(seeded.stdout) == {
                "status": "seeded", "admin_seeded": True, "model_seeded": True})
            # No rollback/reset/reseed is attempted after this point. Runtime
            # failure retains the seeded DB and its private diagnostic report.
            summary.update(stage="seeded", admin_seeded=True, model_seeded=True,
                           admin_count=1, route_count=route_count)
            save_summary(output, summary)
            current = document(read_regular(CONFIG / "deployment-result.json", MAX_INPUT))
            require(current.get("admin_seeded") is True and current.get("model_seeded") is True)
            summary.update(stage="runtime_running", runtime_attempted=True)
            save_summary(output, summary)
            checked = subprocess.run(
                [*prefix, *module.compose("exec", "-T", "api", "python", "-", "--model-id", MODEL_ID,
                                          "--report-name", REPORT_NAME)],
                cwd=RELEASE, env=environment, input=sources["runtime-deployment-check.py"],
                capture_output=True, timeout=330, check=False,
            )
            require(checked.returncode == 0 and not checked.stderr.strip())
            summary["runtime"] = runtime_summary(checked.stdout)
            summary.update(stage="complete", runtime_complete=True)
        except BaseException:
            summary["stage"] = "runtime_failed" if summary["admin_seeded"] else "seed_failed"
            # A failed seed may have committed before its controller could
            # update status. Always inspect state/DB before any manual retry.
            summary["operator_review_required"] = True
        finally:
            save_summary(output, summary)
    return summary


def main():
    logging.disable(logging.CRITICAL)
    try:
        summary = finalize()
    except BaseException:
        # Never print exceptions, Pydantic inputs, subprocess argv or outputs.
        summary = {"stage": "wrapper_failed", "operator_review_required": True,
                   "web_authentication": "pending_human_browser_login", "automatic_retries": False}
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["stage"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
