#!/usr/bin/env python3
"""Package the current cloud source tree, including uncommitted source files.

Only the explicit production build inputs below are considered. Private state
never belongs in this archive; model credentials and tenant data must use a
separate, operator-controlled migration. No git checkout, build or network is
performed. Both the archive and its adjacent manifest are created as 0600 files.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
from datetime import datetime, timezone


SOURCE_FILES = (
    "LICENSE", "NOTICE", ".dockerignore", ".env.cloud.example", "compose.cloud.yaml",
    "deploy/cloud/Dockerfile.api", "deploy/cloud/Dockerfile.api.dockerignore",
    "deploy/cloud/Dockerfile.web", "deploy/cloud/Dockerfile.web.dockerignore",
    "deploy/cloud/Dockerfile.runtime", "deploy/cloud/Dockerfile.runtime.dockerignore",
    "deploy/cloud/nginx.conf", "deploy/cloud/backup.py",
    "backend/requirements.txt", "backend/requirements-cloud.in", "backend/requirements-cloud.txt",
    "backend/alembic.ini", "backend/cloud_alembic.ini",
    "frontend/package.json", "frontend/package-lock.json", "frontend/next.config.ts",
    "frontend/next-env.d.ts", "frontend/tsconfig.json", "frontend/postcss.config.mjs",
    "frontend/eslint.config.mjs", "frontend/scripts/prepare-pdfjs-assets.mjs",
    "scripts/cloud-release-package.py",
)
SOURCE_DIRECTORIES = (
    "backend/app", "backend/release_packaging", "backend/alembic", "backend/cloud_alembic",
    "frontend/src", "frontend/public",
)
PRIVATE_DIRECTORIES = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".config", ".cache", ".turbo", ".pytest_cache",
    "node_modules", "__pycache__", "test-results", "playwright-report", "backups", "backup",
    "venv", "env", "secrets", "credentials", "sessions", ".suxiaoyou", ".suxiaoyoudb",
})
PRIVATE_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".crt", ".cer",
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".sqlite-shm",
    ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".sql", ".dump", ".rdb", ".aof",
    ".journal", ".wal", ".shm", ".pid", ".lock", ".pyc", ".pyo", ".log",
    ".har", ".zip", ".tar", ".tar.gz", ".tgz", ".bak", ".backup", ".swp", ".swo",
)
PRIVATE_NAMES = frozenset({
    ".cloud-dev.json", "session_token.json", "session-token.json", "credentials.json",
    "credentials", "credentials.yaml", "credentials.yml", "credentials.toml",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "authorized_keys", "known_hosts",
    ".npmrc", ".pypirc", ".netrc", ".ds_store", "thumbs.db", "tsconfig.tsbuildinfo",
    "api_key", "api-key", "api_key.txt", "api-key.txt", "api_key.json", "api-key.json",
    "token.json", "tokens.json", "oauth_tokens.json", "access_token.json", "refresh_token.json",
})
GENERATED_PUBLIC = frozenset({
    "frontend/public/pdf.worker.min.mjs", "frontend/public/cmaps", "frontend/public/standard_fonts",
})
MAX_SOURCE_BYTES = 512 * 1024 * 1024
ARCHIVE_PREFIX = "suxiaoyou-release"


class PackageError(ValueError):
    pass


def excluded(relative: PurePosixPath) -> bool:
    """Apply the same private-state policy at every depth, even in source dirs."""
    name = relative.name.lower()
    if relative.as_posix() == ".env.cloud.example":
        return False
    for component in relative.parts:
        lower = component.lower()
        if (lower in PRIVATE_DIRECTORIES or lower.startswith((".next", ".venv", ".python-", ".env"))
                or lower.startswith((".cloud-dev.json", "session_token.json", "session-token.json"))):
            return True
    if (name in PRIVATE_NAMES or name.endswith(PRIVATE_SUFFIXES)
            or any(marker in name for marker in (".key.", ".pem.", ".p12.", ".pfx."))):
        return True
    if name.startswith(("credentials.", "credentials-", "credentials_")):
        return True
    return any(relative.as_posix() == item or relative.as_posix().startswith(item + "/")
               for item in GENERATED_PUBLIC)


def read_regular(root: Path, relative: PurePosixPath) -> tuple[bytes, int]:
    """Open every path component without following links, including parents."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PackageError("Only regular, unlinked source files are accepted: " + relative.as_posix())
            if before.st_size > MAX_SOURCE_BYTES:
                raise PackageError("Source file exceeds the package budget: " + relative.as_posix())
            content = handle.read(MAX_SOURCE_BYTES + 1)
            after = os.fstat(handle.fileno())
            if (len(content) > MAX_SOURCE_BYTES or len(content) != before.st_size
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise PackageError("Source file changed while packaging: " + relative.as_posix())
            return content, 0o755 if before.st_mode & 0o111 else 0o644
    finally:
        os.close(directory)


def collect(root: Path) -> tuple[list[tuple[str, bytes, int]], int]:
    paths = {PurePosixPath(item) for item in SOURCE_FILES}
    excluded_count = 0
    for selected in SOURCE_DIRECTORIES:
        directory = root / selected
        if directory.is_symlink() or not directory.is_dir():
            raise PackageError("Required source directory is missing or linked: " + selected)
        for current, directories, files in os.walk(directory, followlinks=False):
            for name in list(directories):
                path = Path(current) / name
                relative = PurePosixPath(path.relative_to(root).as_posix())
                if path.is_symlink():
                    raise PackageError("Symbolic links are not accepted: " + relative.as_posix())
                if excluded(relative):
                    directories.remove(name)
                    excluded_count += 1
            for name in files:
                path = Path(current) / name
                relative = PurePosixPath(path.relative_to(root).as_posix())
                if path.is_symlink():
                    raise PackageError("Symbolic links are not accepted: " + relative.as_posix())
                if excluded(relative):
                    excluded_count += 1
                else:
                    paths.add(relative)
    result, total = [], 0
    for relative in sorted(paths, key=str):
        if excluded(relative):
            raise PackageError("Required source conflicts with private-state policy: " + str(relative))
        content, mode = read_regular(root, relative)
        total += len(content)
        if total > MAX_SOURCE_BYTES:
            raise PackageError("Selected source exceeds the package budget")
        result.append((relative.as_posix(), content, mode))
    return result, excluded_count


def encoded(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def add_member(archive: tarfile.TarFile, name: str, content: bytes, mode: int, timestamp: int) -> None:
    entry = tarfile.TarInfo(ARCHIVE_PREFIX + "/" + name)
    entry.size, entry.mode, entry.mtime = len(content), mode, timestamp
    entry.uid = entry.gid = 0
    entry.uname = entry.gname = "root"
    archive.addfile(entry, io.BytesIO(content))


def package(root: Path, output: Path) -> dict:
    files, excluded_count = collect(root)
    created = datetime.now(timezone.utc).replace(microsecond=0)
    timestamp = int(created.timestamp())
    version = json.loads(dict((name, content) for name, content, _ in files)["frontend/package.json"])["version"]
    inventory = [{"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "mode": f"{mode:04o}"}
                 for name, content, mode in files]
    source_hash = hashlib.sha256(encoded({"files": inventory})).hexdigest()
    manifest = {"format_version": 1, "application": "suxiaoyou-online", "version": version,
                "created_at_utc": created.isoformat().replace("+00:00", "Z"),
                "source_sha256": source_hash, "source_file_count": len(files),
                "source_bytes": sum(item["size"] for item in inventory),
                "selection": "Explicit cloud build inputs from the current working tree, including uncommitted source.",
                "excluded_entry_count": excluded_count, "files": inventory}
    output = output.absolute()
    if not output.name.endswith(".tar.gz"):
        raise PackageError("--output must end in .tar.gz")
    adjacent = output.with_name(output.name[:-7] + ".manifest.json")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created_paths = []
    try:
        # O_EXCL refuses existing files and symlinks rather than replacing them.
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created_paths.append(output)
        with os.fdopen(descriptor, "wb") as destination:
            with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=timestamp) as compressed:
                with tarfile.open(mode="w", fileobj=compressed, format=tarfile.PAX_FORMAT) as archive:
                    for name, content, mode in files:
                        add_member(archive, name, content, mode, timestamp)
                    add_member(archive, "RELEASE-MANIFEST.json", encoded(manifest), 0o644, timestamp)
            destination.flush()
            os.fsync(destination.fileno())
        with output.open("rb") as stream:
            archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        artifact = {"file": output.name, "bytes": output.stat().st_size, "sha256": archive_hash}
        descriptor = os.open(adjacent, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created_paths.append(adjacent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded({**manifest, "archive": artifact}))
            stream.flush()
            os.fsync(stream.fileno())
        return {"output": str(output), "manifest": str(adjacent), "source_files": len(files),
                "archive_entries": len(files) + 1, **artifact}
    except BaseException:
        for path in reversed(created_paths):
            path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private .tar.gz file; existing paths are never overwritten")
    args = parser.parse_args()
    try:
        result = package(Path(__file__).resolve().parents[1], args.output)
    except (OSError, ValueError, KeyError) as exc:
        # Errors identify paths or package policy, never source or secret contents.
        parser.exit(1, f"Package creation failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
