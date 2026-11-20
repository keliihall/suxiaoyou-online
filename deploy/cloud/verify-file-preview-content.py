#!/usr/bin/env python3
"""Read existing real DeepSeek output through the actual preview/download routes.

Run as root on the fixed server. It execs this same reviewed helper as UID 10001
in the fixed owning tenant; only that process reads its token from the process
environment. Four allowlisted HTTP reads are permitted, with no retry or
redirect. POST /api/files/content and content-binary are existing read routes.
No prompt, new session, file write, SQLite/PG query, model call or cleanup is
performed. File contents, private paths and credentials never leave stdout.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

TENANT = "sxy-user-f3464ecb4c32763c5309b621ccce2a83"
RUN_ID = "2900941a56e74ddea5ee39b3619fc98f"
SESSION_ID = "01M7KYG4BVQSA76NMDFMFD13HW"
WORKSPACE = "/tenant/workspaces/acceptance-results2-" + RUN_ID
FILE_PATH = WORKSPACE + "/result.md"
EXPECTED = ("SUXIAOYOU_RESULT_INTEGRITY " + RUN_ID + "\n").encode()
EXPECTED_SIZE = 60
EXPECTED_SHA256 = "c27613951ecc027af64296b34b93b40a1a83214cb7a2af5bd081fc10094e2bf3"
SESSION_FILES = "/api/sessions/" + SESSION_ID + "/files"
CONTENT = "/api/files/content"
BINARY = "/api/files/content-binary"
BODY = {"path": FILE_PATH, "workspace": WORKSPACE}
ALLOWED = (("GET", SESSION_FILES), ("GET", SESSION_FILES + "?scope=all"), ("POST", CONTENT), ("POST", BINARY))
MAX_BYTES = 64 * 1024
MAX_SECONDS = 45
PREFIX = ["runuser", "--user", "suxiaoyou", "--", "env", "HOME=/home/suxiaoyou",
          "XDG_RUNTIME_DIR=/run/user/1001", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
          "DOCKER_HOST=unix:///run/user/1001/docker.sock", "docker"]
MARKER = "SXY_FILE_PREVIEW_CONTENT:"


class Failure(Exception):
    pass


def require(value, code):
    if not value:
        raise Failure(code)


def decode(raw):
    def unique(pairs):
        value = {}
        for name, item in pairs:
            require(name not in value, "duplicate_response_key")
            value[name] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, "invalid_json_number"))


def read_json(client, method, path, *, body=None, deadline):
    # Reject every method/route/body outside this fixed read-only acceptance.
    # In particular no chat, session-creation, file-write or arbitrary path can
    # reach the HTTP client, even when this function is imported independently.
    require((method, path) in ALLOWED and ((method == "GET" and body is None)
            or (method == "POST" and body == BODY)), "endpoint_not_allowlisted")
    remaining = deadline - time.monotonic()
    require(remaining > 0, "content_acceptance_timeout")
    with client.stream(method, path, json=body, timeout=min(10, remaining)) as response:
        require(response.status_code == 200, "content_read_http_failed")
        raw = bytearray()
        for chunk in response.iter_bytes():
            require(len(raw) + len(chunk) <= MAX_BYTES, "content_response_too_large")
            require(time.monotonic() < deadline, "content_acceptance_timeout")
            raw.extend(chunk)
    return decode(raw)


def file_count(value, expected_names):
    rows = value.get("files") if isinstance(value, dict) else None
    require(isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("name"), str)
            and isinstance(row.get("path"), str) for row in rows), "invalid_file_list")
    require(Counter((row["name"], row["path"]) for row in rows)
            == {(name, WORKSPACE + "/" + name): 1 for name in expected_names}, "file_scope_or_identity_mismatch")
    return len(rows)


def metadata(value):
    require(isinstance(value, dict) and value.get("name") == "result.md" and value.get("path") == FILE_PATH
            and type(value.get("size")) is int and value["size"] == EXPECTED_SIZE, "file_metadata_mismatch")


def verify_existing_content(client):
    require(len(EXPECTED) == EXPECTED_SIZE and hashlib.sha256(EXPECTED).hexdigest() == EXPECTED_SHA256,
            "fixed_acceptance_identity_mismatch")
    deadline = time.monotonic() + MAX_SECONDS
    delivered = file_count(read_json(client, "GET", SESSION_FILES, deadline=deadline), ["result.md"])
    all_files = file_count(read_json(client, "GET", SESSION_FILES + "?scope=all", deadline=deadline),
                           ["draft.md", "process-notes.json", "result.md"])
    text = read_json(client, "POST", CONTENT, body=dict(BODY), deadline=deadline)
    metadata(text)
    require(isinstance(text.get("content"), str) and text["content"].encode() == EXPECTED, "text_preview_content_mismatch")
    binary = read_json(client, "POST", BINARY, body=dict(BODY), deadline=deadline)
    metadata(binary)
    require(isinstance(binary.get("content_base64"), str), "invalid_binary_encoding")
    try:
        raw = base64.b64decode(binary["content_base64"], validate=True)
    except (ValueError, TypeError):
        raise Failure("invalid_binary_encoding") from None
    require(raw == EXPECTED and hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256, "download_content_mismatch")
    return {"ok": True, "existing_real_model_output": True, "model_requests": 0,
            "created_sessions": 0, "file_writes": 0, "database_operations": 0, "automatic_retries": 0,
            "http_reads": 4, "default_deliverables": delivered, "all_files": all_files,
            "text_preview_exact": True, "download_exact": True, "bytes": EXPECTED_SIZE,
            "sha256": EXPECTED_SHA256, "endpoints": [method + " " + path for method, path in ALLOWED]}


def validate_result(result):
    expected = {"ok": True, "existing_real_model_output": True, "model_requests": 0,
                "created_sessions": 0, "file_writes": 0, "database_operations": 0, "automatic_retries": 0,
                "http_reads": 4, "default_deliverables": 1, "all_files": 3,
                "text_preview_exact": True, "download_exact": True, "bytes": EXPECTED_SIZE,
                "sha256": EXPECTED_SHA256, "endpoints": [method + " " + path for method, path in ALLOWED]}
    require(result == expected, "tenant_result_contract_failed")
    return result


def tenant_main():
    import httpx
    require(os.getuid() == os.geteuid() == 10001, "owning_tenant_uid_required")
    # The session credential stays only in this owning tenant process. Do not
    # read an auth file, expose it in Docker arguments or copy it into a report.
    token = os.environ.get("CLOUD_ENGINE_AUTH_TOKEN", "")
    require(isinstance(token, str) and token.startswith("suxiaoyou_st_") and 50 <= len(token) <= 4096
            and all(33 <= ord(char) <= 126 for char in token), "runtime_auth_missing")
    with httpx.Client(base_url="http://127.0.0.1:8080", trust_env=False, follow_redirects=False,
                      transport=httpx.HTTPTransport(retries=0),
                      headers={"Authorization": "Bearer " + token, "Accept-Language": "en"}) as client:
        return verify_existing_content(client)


def source_bytes():
    fd = os.open(Path(__file__), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid in {0, 1001} and info.st_nlink == 1
                and not stat.S_IMODE(info.st_mode) & 0o022 and 0 < info.st_size <= 32 * 1024, "unsafe_helper_source")
        raw = source.read(32 * 1024 + 1)
    require(len(raw) == info.st_size, "helper_source_changed")
    return raw


def root_main():
    require(os.getuid() == os.geteuid() == 0, "server_root_required")
    source = source_bytes()
    args = PREFIX + ["exec", "-i", "--user", "10001:10001", TENANT, "python", "-B", "-c",
                     "import sys;exec(compile(sys.stdin.buffer.read(),'<fixed-preview-content-helper>','exec'))",
                     "--inside-owning-tenant"]
    # No shell, environment dump, log read, credential transfer or retry.
    result = subprocess.run(args, input=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=75, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 4096 and result.stdout.startswith(MARKER.encode()),
            "tenant_content_probe_failed")
    return validate_result(decode(result.stdout[len(MARKER):]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside-owning-tenant", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return tenant_main() if args.inside_owning_tenant else root_main()


if __name__ == "__main__":
    try:
        result = main()
        status = 0
    except BaseException as error:
        result = {"ok": False, "model_requests": 0,
                  "error": str(error) if isinstance(error, Failure) else "content_verification_failed"}
        status = 1
    print(MARKER + json.dumps(result, separators=(",", ":")))
    raise SystemExit(status)
