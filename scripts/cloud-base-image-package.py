#!/usr/bin/env python3
"""Package four pinned public linux/amd64 bases without using a Docker daemon.

Downloads are anonymous HTTPS requests with certificate verification. Each
compressed blob and decompressed layer is checked against the pinned upstream
manifest/config before writing a standard Docker-load archive. No image is run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import certifi


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "deploy/cloud/base-image-digests.json"
EVIDENCE_SHA256 = "94196786fae5ff446469732f381a91f40f64c37e6d6eac2c21ad79f63827d5d1"
SOURCES = {
    "node:22-bookworm-slim": "suxiaoyou-base-node:20261115-amd64",
    "python:3.12-slim-bookworm": "suxiaoyou-base-python312:20261115-amd64",
    "python:3.13-slim-bookworm": "suxiaoyou-base-python313:20261115-amd64",
    "docker:28-cli": "suxiaoyou-base-docker28:20261115-amd64",
}
HOSTS = ("docker.m.daocloud.io", "docker.1ms.run")
CHUNK = 1024 * 1024
DISK_LIMIT = 3 * 1024**3 - 1
LAYER_LIMIT = 800 * 1024**2
ACCEPT = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"


class PackageFailure(Exception):
    pass


def require(condition):
    if not condition:
        raise PackageFailure


def progress(stage, **values):
    print(json.dumps({"stage": stage, **values}, separators=(",", ":")), flush=True)


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while content := handle.read(CHUNK):
            result.update(content)
    return "sha256:" + result.hexdigest()


def checked_digest(value):
    require(isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value))
    return value


def private_directory(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700)


def regular(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
    return info


class Budget:
    def __init__(self, initial=0):
        self.used, self.lock = initial, threading.Lock()
        require(initial < DISK_LIMIT)

    def reserve(self, amount):
        with self.lock:
            require(0 <= self.used + amount <= DISK_LIMIT)
            self.used += amount

    def remove(self, path):
        if path.exists():
            size = regular(path).st_size
            path.unlink()
            self.reserve(-size)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, destination):
        parsed = urllib.parse.urlsplit(destination)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return None
        redirected = super().redirect_request(request, fp, code, message, headers, destination)
        if redirected and parsed.netloc != urllib.parse.urlsplit(request.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


class Registry:
    def __init__(self, repository):
        self.repository, self.tokens, self.lock = repository, {}, threading.Lock()
        self.used_hosts = set()

    @staticmethod
    def opener():
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), SafeRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))

    def token(self, host, *, refresh=False):
        with self.lock:
            cached = self.tokens.get(host)
            if not refresh and cached and cached[1] > time.monotonic():
                return cached[0]
            realm = ("https://m.daocloud.io/auth/token" if host == HOSTS[0]
                     else "https://docker.1ms.run/openapi/v1/auth/token")
            url = realm + "?" + urllib.parse.urlencode({
                "service": host, "scope": "repository:" + self.repository + ":pull"})
            with self.opener().open(url, timeout=15) as response:
                raw = response.read(65537)
                require(response.status == 200 and len(raw) <= 65536)
            document = json.loads(raw)
            token = document.get("token") or document.get("access_token")
            require(isinstance(token, str) and 0 < len(token) <= 16384)
            self.tokens[host] = (token, time.monotonic() + 120)
            return token

    def get(self, path, *, limit, byte_range=None, accept=None):
        for attempt in range(6):
            host = HOSTS[(attempt // 2) % len(HOSTS)]
            try:
                headers = {"Authorization": "Bearer " + self.token(host, refresh=attempt % 2 == 1),
                           "User-Agent": "suxiaoyou-offline-public-bases/1"}
                if accept:
                    headers["Accept"] = accept
                if byte_range:
                    start, end, total = byte_range
                    headers["Range"] = f"bytes={start}-{end}"
                request = urllib.request.Request("https://" + host + "/v2/" + self.repository + path,
                                                 headers=headers)
                with self.opener().open(request, timeout=30) as response:
                    content = response.read(limit + 1)
                    require(len(content) <= limit)
                    if byte_range:
                        require((response.status == 206 and response.headers.get("Content-Range")
                                 == f"bytes {start}-{end}/{total}")
                                or (response.status == 200 and start == 0 and end + 1 == total))
                        require(len(content) == end - start + 1)
                    else:
                        require(response.status == 200)
                with self.lock:
                    self.used_hosts.add(host)
                return content
            except Exception:
                if attempt == 5:
                    raise PackageFailure from None
                time.sleep(min(0.25 * (attempt + 1), 1))
        raise PackageFailure


def write_new(path, content, budget, *, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), mode)
        budget.reserve(len(content))
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def prepare(work, budget):
    raw = EVIDENCE.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == EVIDENCE_SHA256)
    evidence = json.loads(raw)
    require(evidence["platform"] == "linux/amd64")
    source_rows = {row["source"]: row for row in evidence["images"]}
    images, blobs = [], {}
    for source, tag in SOURCES.items():
        row = source_rows[source]
        expected = checked_digest(row["amd64_digest"])
        require(expected == row["mirror_verified_sha256"])
        repository = "library/" + source.split(":", 1)[0]
        registry = Registry(repository)
        manifest_raw = registry.get("/manifests/" + expected, limit=2 * CHUNK, accept=ACCEPT)
        require("sha256:" + hashlib.sha256(manifest_raw).hexdigest() == expected)
        manifest = json.loads(manifest_raw)
        config_digest = checked_digest(manifest["config"]["digest"])
        require(config_digest == row["verification"]["config_blob_sha256"])
        config_raw = registry.get("/blobs/" + config_digest, limit=2 * CHUNK)
        require("sha256:" + hashlib.sha256(config_raw).hexdigest() == config_digest)
        config = json.loads(config_raw)
        require(config.get("architecture") == "amd64" and config.get("os") == "linux"
                and config.get("rootfs", {}).get("type") == "layers")
        layers = manifest["layers"]
        diff_ids = config["rootfs"]["diff_ids"]
        require(1 <= len(layers) == len(diff_ids) <= 64)
        for layer, diff_id in zip(layers, diff_ids):
            digest = checked_digest(layer["digest"])
            checked_digest(diff_id)
            require(type(layer["size"]) is int and 0 < layer["size"] <= 512 * CHUNK)
            media = layer.get("mediaType", "")
            require(media in {"application/vnd.oci.image.layer.v1.tar+gzip",
                              "application/vnd.docker.image.rootfs.diff.tar.gzip",
                              "application/vnd.oci.image.layer.v1.tar"})
            if digest in blobs:
                require(blobs[digest]["diff_id"] == diff_id and blobs[digest]["size"] == layer["size"])
            else:
                blobs[digest] = {"digest": digest, "diff_id": diff_id, "size": layer["size"],
                                 "media_type": media, "registry": registry}
        config_file = work / (config_digest[7:] + ".json")
        if config_file.exists():
            regular(config_file)
            require(sha256(config_file) == config_digest)
        else:
            write_new(config_file, config_raw, budget)
        images.append({"source": source, "repo_tag": tag, "official_manifest_digest": expected,
                       "config_digest": config_digest, "diff_ids": diff_ids,
                       "compressed_layer_digests": [layer["digest"] for layer in layers]})
        progress("manifest_verified", source=source, layers=len(layers), config_digest=config_digest)
    require(sum(blob["size"] for blob in blobs.values()) < 512 * CHUNK)
    return images, blobs


def download(blob, work, budget):
    target = work / (blob["digest"][7:] + ".blob")
    if target.exists():
        regular(target)
        if target.stat().st_size == blob["size"] and sha256(target) == blob["digest"]:
            progress("blob_cached", digest=blob["digest"], bytes=blob["size"])
            return
        budget.remove(target)
    partial = target.with_suffix(".partial")
    if partial.exists():
        info = regular(partial)
        require(info.st_size <= blob["size"] and (info.st_size % CHUNK == 0 or info.st_size == blob["size"]))
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        os.fchmod(handle.fileno(), 0o600)
        offset = handle.tell()
        while offset < blob["size"]:
            end = min(offset + CHUNK, blob["size"]) - 1
            content = blob["registry"].get("/blobs/" + blob["digest"], limit=end - offset + 1,
                                            byte_range=(offset, end, blob["size"]))
            budget.reserve(len(content))
            handle.write(content)
            handle.flush()
            offset += len(content)
        os.fsync(handle.fileno())
    require(sha256(partial) == blob["digest"])
    os.replace(partial, target)
    progress("blob_verified", digest=blob["digest"], bytes=blob["size"])


def unpack(blob, work, budget):
    target = work / (blob["diff_id"][7:] + ".layer.tar")
    if target.exists():
        regular(target)
        require(sha256(target) == blob["diff_id"])
        return target
    temporary = target.with_suffix(".partial")
    if temporary.exists():
        budget.remove(temporary)
    source = work / (blob["digest"][7:] + ".blob")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    checksum, size = hashlib.sha256(), 0
    with source.open("rb") as compressed, os.fdopen(descriptor, "wb") as output:
        reader = gzip.GzipFile(fileobj=compressed) if blob["media_type"].endswith(("+gzip", ".gzip")) else compressed
        try:
            while chunk := reader.read(CHUNK):
                size += len(chunk)
                require(size <= LAYER_LIMIT)
                budget.reserve(len(chunk))
                output.write(chunk)
                checksum.update(chunk)
        finally:
            if reader is not compressed:
                reader.close()
        output.flush()
        os.fsync(output.fileno())
    require("sha256:" + checksum.hexdigest() == blob["diff_id"])
    os.replace(temporary, target)
    progress("diff_id_verified", diff_id=blob["diff_id"], bytes=size)
    return target


class BudgetOutput:
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget

    def write(self, content):
        self.budget.reserve(len(content))
        return self.stream.write(content)

    def flush(self):
        return self.stream.flush()


def add_file(archive, name, path=None, content=None):
    size = path.stat().st_size if path is not None else len(content)
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.uid, info.gid, info.mtime = size, 0o644, 0, 0, 0
    if path is not None:
        with path.open("rb") as handle:
            archive.addfile(info, handle)
    else:
        archive.addfile(info, io.BytesIO(content))


def package(images, layers, output, budget):
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        with gzip.GzipFile(filename="", mode="wb", compresslevel=1, mtime=0,
                           fileobj=BudgetOutput(handle, budget)) as zipped:
            with tarfile.open(mode="w|", fileobj=zipped, format=tarfile.PAX_FORMAT) as archive:
                records = []
                for image in images:
                    name = image["config_digest"][7:] + ".json"
                    records.append({"Config": name, "RepoTags": [image["repo_tag"]],
                                    "Layers": [diff[7:] + "/layer.tar" for diff in image["diff_ids"]]})
                add_file(archive, "manifest.json", content=json.dumps(records, separators=(",", ":")).encode())
                configs = {image["config_digest"] for image in images}
                for digest in sorted(configs):
                    add_file(archive, digest[7:] + ".json", path=layers["config:" + digest])
                for diff_id in sorted(key for key in layers if not key.startswith("config:")):
                    add_file(archive, diff_id[7:] + "/layer.tar", path=layers[diff_id])
        handle.flush()
        os.fsync(handle.fileno())


def run(args):
    require(1 <= args.workers <= 4 and args.output.is_absolute() and args.work.is_absolute())
    require(not args.output.exists() and not args.output.is_symlink())
    manifest_path = args.output.with_name("sxy-amd64-bases.manifest.json")
    sha_path = args.output.with_name("sxy-amd64-bases.sha256")
    require(not manifest_path.exists() and not manifest_path.is_symlink()
            and not sha_path.exists() and not sha_path.is_symlink())
    require(shutil.disk_usage(args.output.parent).free > DISK_LIMIT)
    private_directory(args.work)
    initial = 0
    for child in args.work.iterdir():
        initial += regular(child).st_size
    budget = Budget(initial)
    progress("started", pid=os.getpid(), images=len(SOURCES), max_parallel=args.workers,
             disk_limit_bytes=DISK_LIMIT)
    images, blobs = prepare(args.work, budget)
    total = sum(blob["size"] for blob in blobs.values())
    progress("downloading", unique_blobs=len(blobs), compressed_bytes=total)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(download, blob, args.work, budget)
                   for blob in sorted(blobs.values(), key=lambda blob: -blob["size"])]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    layers = {}
    for blob in blobs.values():
        layers[blob["diff_id"]] = unpack(blob, args.work, budget)
    for image in images:
        layers["config:" + image["config_digest"]] = args.work / (image["config_digest"][7:] + ".json")
    progress("archiving", unique_layers=len(blobs), disk_bytes=budget.used)
    try:
        package(images, layers, args.output, budget)
    except BaseException:
        if args.output.exists():
            budget.remove(args.output)
        raise
    checksum = sha256(args.output)[7:]
    metadata = {
        "format_version": 1, "platform": "linux/amd64",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "archive": {"filename": args.output.name, "sha256": checksum, "bytes": args.output.stat().st_size},
        "images": [{key: image[key] for key in ("source", "repo_tag", "official_manifest_digest", "config_digest")}
                   | {"verification": {"diff_ids": image["diff_ids"],
                                       "compressed_layer_digests": image["compressed_layer_digests"],
                                       "all_compressed_sha256_verified": True, "all_diff_ids_verified": True}}
                   for image in images],
    }
    encoded = (json.dumps(metadata, indent=2) + "\n").encode()
    write_new(manifest_path, encoded, budget)
    manifest_checksum = hashlib.sha256(encoded).hexdigest()
    write_new(sha_path, (f"{checksum}  {args.output.name}\n{manifest_checksum}  {manifest_path.name}\n").encode(),
              budget, mode=0o644)
    progress("complete", archive=str(args.output), archive_sha256=checksum,
             archive_bytes=args.output.stat().st_size, manifest=str(manifest_path),
             manifest_sha256=manifest_checksum, disk_bytes=budget.used)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/sxy-amd64-bases.tar.gz"))
    parser.add_argument("--work", type=Path, default=Path("/tmp/sxy-amd64-bases-work"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run(args)
    except BaseException:
        progress("failed", reason="public_image_download_or_verification_failed", retry_automatically=False)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
