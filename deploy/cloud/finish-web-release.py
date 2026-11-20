#!/usr/bin/env python3
"""Finish this first release using a separately verified public npm cache.

Run only after the operator has stopped its former build/controller. Never stops
processes, prepares caches, downloads bases, seeds users or changes frozen source.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from types import ModuleType

APP = Path("/srv/suxiaoyou")
CONTINUE = APP / "incoming/continue-release.py"
CONTINUE_SHA256 = "0cfbc8a3958f725cf522318814e09b4e69370037d101c73b7e5c647542d045ec"
CACHE_RECEIPT = APP / "incoming/npm-cache-base.manifest.json"
CACHE_TAG = "suxiaoyou-npm-cache:20261115-2144"
NODE_TAG = "suxiaoyou-base-node:20261115-amd64"


def require(condition):
    if not condition:
        raise RuntimeError("Reviewed web recovery precondition failed.")


def modules():
    descriptor = os.open(CONTINUE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1
                and not stat.S_IMODE(info.st_mode) & 0o022 and info.st_size <= 128 * 1024)
        raw = handle.read(128 * 1024 + 1)
    require(hashlib.sha256(raw).hexdigest() == CONTINUE_SHA256)
    continuation = ModuleType("suxiaoyou_frozen_web_continuation")
    continuation.__file__ = str(CONTINUE)
    exec(compile(raw, str(CONTINUE), "exec"), continuation.__dict__)
    resume = continuation.resume_module()
    return continuation, resume, resume.master_module()


def image(master, resume, tag):
    return resume.decode(master.command(["docker", "image", "inspect", "--format", "{{json .}}", tag],
                                       "Verify prepared release image", capture=True, timeout=30))


def verify_cache(master, resume, receipt_hash, recipe):
    raw = resume.read_file(CACHE_RECEIPT, 65536, private=True)
    require(hashlib.sha256(raw).hexdigest() == receipt_hash)
    receipt = resume.decode(raw)
    require(set(receipt) == {"format_version", "platform", "image_tag", "image_id", "node_base_tag",
                            "node_base_image_id", "cache_archive_sha256", "cache_bytes", "cache_file_count",
                            "public_registry_only", "content_integrity_verified"}
            and receipt["format_version"] == 1 and receipt["platform"] == "linux/amd64"
            and receipt["image_tag"] == CACHE_TAG and receipt["node_base_tag"] == NODE_TAG
            and receipt["public_registry_only"] is True and receipt["content_integrity_verified"] is True
            and re.fullmatch(r"[0-9a-f]{64}", receipt["cache_archive_sha256"])
            and type(receipt["cache_bytes"]) is int and 0 < receipt["cache_bytes"] <= 2 * 1024**3
            and type(receipt["cache_file_count"]) is int and 0 < receipt["cache_file_count"] <= 100000)
    node, cached = image(master, resume, NODE_TAG), image(master, resume, CACHE_TAG)
    evidence = [item for item in recipe["offline_resume"]["verified_local_images"] if item["local_tag"] == NODE_TAG]
    require(len(evidence) == 1 and evidence[0]["raw_saved_config_sha256_verified"] is True
            and node["Id"] == receipt["node_base_image_id"] == evidence[0]["inspect_image_id"]
            and node["RootFS"]["Layers"] == evidence[0]["all_rootfs_diff_ids"]
            and cached["Id"] == receipt["image_id"] and cached["Config"] == node["Config"])
    for value, tag in ((node, NODE_TAG), (cached, CACHE_TAG)):
        require(value.get("Os") == "linux" and value.get("Architecture") == "amd64"
                and tag in value.get("RepoTags", []) and value.get("RootFS", {}).get("Type") == "layers"
                and re.fullmatch(r"sha256:[0-9a-f]{64}", value["Id"]))
    layers = node["RootFS"]["Layers"]
    require(cached["RootFS"]["Layers"][:len(layers)] == layers and len(cached["RootFS"]["Layers"]) == len(layers) + 1)
    return receipt


def verify_build_copies(continuation, resume, master, recipe):
    images, evidence = master.load_base_images()
    require(recipe["source_archive_sha256"] == master.EXPECTED_SHA256
            and recipe["base_image_evidence_sha256"] == master.EXPECTED_BASE_METADATA_SHA256
            and recipe["base_images"] == images and recipe["platform"] == "linux/amd64"
            and recipe["source_context_modified"] is False and recipe["python_require_hashes"] is True
            and recipe["pip_index_url"] == master.PIP_INDEX and "npm_cache_continuation" not in recipe
            and resume.read_file(master.CONFIG / "base-image-digests.json", 512 * 1024, private=True) == evidence)
    offline, apt = recipe["offline_resume"], recipe["apt_transport_continuation"]
    require(offline["master_deployer_sha256"] == resume.MASTER_SHA256
            and offline["offline_manifest_sha256"] == resume.BASE_MANIFEST_SHA256
            and offline["archive"]["sha256"] == resume.BASE_ARCHIVE_SHA256
            and offline["effective_base_images"] == resume.BASE_TAGS
            and offline["source_context_modified"] is False and offline["credentials_modified_by_resume"] is False
            and apt["master_deployer_sha256"] == resume.MASTER_SHA256
            and apt["offline_resume_sha256"] == continuation.RESUME_SHA256 and apt["target"] == "runtime"
            and apt["source_context_modified"] is False and apt["tls_modified"] is False
            and apt["credentials_modified"] is False and apt["socket_gid_probe_pending"] is True
            and apt["environment_modified"] is False)
    notes = {entry["target"]: entry for entry in recipe["dockerfiles"]}
    resumed = {entry["target"]: entry for entry in offline["dockerfiles"]}
    require(set(notes) == set(resumed) == set(master.DOCKERFILE_BASES))
    expression = re.compile(r"^FROM ([^\s]+)( AS [A-Za-z0-9_-]+)?$", re.MULTILINE)
    for target, bases in master.DOCKERFILE_BASES.items():
        path = master.CONFIG / "build" / ("Dockerfile." + target)
        original = resume.read_file(master.RELEASE / "deploy/cloud" / path.name, 65536)
        require(hashlib.sha256(original).hexdigest() == notes[target]["original_dockerfile_sha256"])
        require(tuple(item.group(1) for item in expression.finditer(original.decode())) == bases)
        text = expression.sub(lambda match: "FROM " + resume.BASE_TAGS[match.group(1)] + (match.group(2) or ""), original.decode())
        if target != "web":
            text = text.replace("RUN python -m pip install --require-hashes -r ",
                                f"RUN PIP_INDEX_URL={master.PIP_INDEX} PIP_EXTRA_INDEX_URL= python -m pip install --require-hashes -r ", 1)
        expected = text.encode()
        require(resumed[target]["before_sha256"] == notes[target]["adapted_dockerfile_sha256"]
                and hashlib.sha256(expected).hexdigest() == resumed[target]["after_sha256"])
        if target == "runtime":
            require(hashlib.sha256(expected).hexdigest() == apt["before_sha256"])
            expected = continuation.adapted_runtime(expected)
            require(hashlib.sha256(expected).hexdigest() == apt["after_sha256"])
        ignore = resume.read_file(path.with_name(path.name + ".dockerignore"), 65536)
        require(resume.read_file(path, 65536) == expected
                and hashlib.sha256(ignore).hexdigest() == notes[target]["dockerignore_sha256"]
                and ignore == resume.read_file(master.RELEASE / "deploy/cloud" / (path.name + ".dockerignore"), 65536))
    captured, writer = {}, master.private_write
    try:
        master.private_write = lambda path, content, mode=0o600: captured.update({path: content})
        master.configure_proxy(images)
    finally:
        master.private_write = writer
    require(len(captured) == 2 and all(resume.read_file(path, 65536) == content for path, content in captured.items()))


def adapt_web(before):
    text = before.decode()
    replacements = (
        ("FROM " + NODE_TAG + " AS build\n", "FROM " + CACHE_TAG + " AS build\n"),
        ("RUN npm ci --legacy-peer-deps --ignore-scripts\n",
         "RUN npm ci --legacy-peer-deps --ignore-scripts --prefer-offline --no-audit --no-fund --fetch-timeout=60000 --fetch-retries=1\n"),
        ("npm prune --legacy-peer-deps --omit=dev --ignore-scripts\n",
         "npm prune --legacy-peer-deps --omit=dev --ignore-scripts --no-audit --no-fund\n"),
    )
    for old, new in replacements:
        require(text.count(old) == 1)
        text = text.replace(old, new, 1)
    require(re.findall(r"(?m)^FROM ([^\s]+)(?: AS [A-Za-z0-9_-]+)?$", text) == [CACHE_TAG, NODE_TAG])
    return text.encode()


def finish(receipt_hash):
    os.umask(0o077)
    require(os.getuid() == os.geteuid() == 1001)
    continuation, resume, master = modules()
    for path in (APP, APP / "incoming"):
        resume.private_directory(path, strict=False)
    for path in (master.SHARED, master.SHARED / "config", master.CONFIG, master.CONFIG / "build", master.RELEASE, master.TLS):
        resume.private_directory(path)
    lock = os.open(master.SHARED / "deploy.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in (APP / "current", master.CONFIG / "deployment-result.json"):
            require(not path.exists() and not path.is_symlink())
        preserved = continuation.unchanged_inputs(resume, master)
        recipe = resume.decode(resume.read_file(master.CONFIG / "build-recipe.json", 512 * 1024, private=True))
        manifest = continuation.verify_source(resume, master, recipe)
        verify_build_copies(continuation, resume, master, recipe)
        receipt = verify_cache(master, resume, receipt_hash, recipe)
        require(not master.command(["docker", "ps", "--filter", "label=com.docker.compose.project=" + master.PROJECT,
                                    "--format", "{{.ID}}"], "Check no running cloud workloads", capture=True, timeout=30).strip())
        require(not master.command(["docker", "image", "ls", "--filter", "reference=" + master.WEB_IMAGE,
                                    "--format", "{{.ID}}"], "Check web image remains unpublished", capture=True, timeout=30).strip())
        for tag in (master.API_IMAGE, master.RUNTIME_IMAGE):
            value = image(master, resume, tag)
            require(value.get("Os") == "linux" and value.get("Architecture") == "amd64" and tag in value.get("RepoTags", []))
        require(re.search(rb"(?m)^Driver:\s+docker\s*$", master.command(["docker", "buildx", "inspect"],
                "Check embedded Docker builder", capture=True, timeout=30)) is not None)
        path = master.CONFIG / "build/Dockerfile.web"
        before = resume.read_file(path, 65536)
        after = adapt_web(before)
        recipe["npm_cache_continuation"] = {
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "continue_controller_sha256": CONTINUE_SHA256, "cache_receipt_sha256": receipt_hash, "cache_receipt": receipt,
            "before_sha256": hashlib.sha256(before).hexdigest(), "after_sha256": hashlib.sha256(after).hexdigest(),
            "source_context_modified": False, "lock_and_integrity_unchanged": True, "tls_verification_unchanged": True,
            "ci_options": "--prefer-offline --no-audit --no-fund --fetch-timeout=60000 --fetch-retries=1",
            "prune_options": "--no-audit --no-fund", "socket_gid_probe_pending": True,
        }
        recipe_path = master.CONFIG / "build-recipe.json"
        save_recipe = lambda: resume.atomic_write(master, recipe_path, (json.dumps(recipe, sort_keys=True, indent=2) + "\n").encode(), 0o600)
        save_recipe()
        resume.atomic_write(master, path, after, 0o644)
        master.command(master.compose("config", "--quiet"), "Validate npm-cache web recovery", timeout=60)
        master.progress("Finishing Web with verified public npm cache and bounded unaudited package fetches.")
        master.command(master.compose("build", "web"), "Finish web build with verified public npm cache")
        preserved[master.ENV_FILE], gid = continuation.probe_socket_retaining_credentials(resume, master, preserved[master.ENV_FILE])
        for name in ("apt_transport_continuation", "npm_cache_continuation"):
            recipe[name].update(socket_gid_probe_pending=False, docker_socket_gid=gid, environment_modified=(gid != 0))
        save_recipe()
        master.command(master.compose("config", "--quiet"), "Validate final production configuration", timeout=60)
        master.command(master.compose("run", "--rm", "--no-deps", "--entrypoint", "nginx", "proxy", "-t"), "Validate TLS proxy", timeout=120)
        master.command(master.compose("up", "-d", "--wait", "--wait-timeout", "240"), "Start reviewed cloud project", timeout=360)
        master.wait_ready()
        require(all(resume.read_file(path, 16384, private=(path == master.ENV_FILE)) == content for path, content in preserved.items()))
        current = APP / "current"
        require(not current.exists() and not current.is_symlink())
        temporary = APP / (".current-web-finish-" + secrets.token_hex(8))
        temporary.symlink_to(master.RELEASE)
        os.replace(temporary, current)
        master.private_write(master.CONFIG / "deployment-result.json", (json.dumps({
            "version": master.VERSION, "origin": master.ORIGIN, "source_archive_sha256": master.EXPECTED_SHA256,
            "source_sha256": manifest["source_sha256"], "ready": True,
            "base_image_evidence_sha256": master.EXPECTED_BASE_METADATA_SHA256, "platform": "linux/amd64",
            "build_recipe": str(recipe_path), "admin_seeded": False, "model_seeded": False,
        }, indent=2, sort_keys=True) + "\n").encode())
        master.progress("Cloud release is HTTPS-ready; administrator/model seed remains pending.")
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        require(re.fullmatch(r"[0-9a-f]{64}", args.cache_manifest_sha256))
        finish(args.cache_manifest_sha256)
    except BaseException:
        print("Web recovery stopped safely; inspect private deployment state before another attempt.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
