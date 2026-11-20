"""Authenticated encryption for administrator-managed model API keys.

The fallback master key lives in the shared private cloud data volume, outside
the database. Publishing a complete file with an exclusive hard link prevents
API/Worker startup races from observing an empty or partially written key.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import stat

from cryptography.fernet import Fernet, InvalidToken

from app.cloud.config import CloudSettings


class ModelSecretError(ValueError):
    """A safe-to-display error that never includes a key or ciphertext."""


def _validate_owner(metadata) -> None:
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ModelSecretError("Model secret storage must be owned by the current service user")


def _validate_private(metadata, *, directory: bool) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    expected_mode = 0o700 if directory else 0o600
    if not expected_type(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != expected_mode:
        label = "directory (0700)" if directory else "regular file (0600)"
        raise ModelSecretError(f"Model secret storage must be a private {label}")
    _validate_owner(metadata)


def _cipher(key: bytes) -> Fernet:
    if not re.fullmatch(rb"[A-Za-z0-9_-]{43}=", key):
        raise ModelSecretError("Model master key is not a valid Fernet key")
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise ModelSecretError("Model master key is not a valid Fernet key") from None


def _read_key(directory_fd: int) -> bytes:
    descriptor = os.open("model-master.key", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                         dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as handle:
        _validate_private(os.fstat(handle.fileno()), directory=False)
        key = handle.read(128)
    _cipher(key)
    return key


def _load_or_create(settings: CloudSettings) -> bytes:
    data_dir = Path(os.path.abspath(settings.data_dir))
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_fd = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(data_fd)
        _validate_owner(metadata)
        if metadata.st_mode & 0o022:
            raise ModelSecretError("Cloud data directory must not be writable by other users")
        try:
            os.mkdir("secrets", mode=0o700, dir_fd=data_fd)
        except FileExistsError:
            pass
        directory_fd = os.open("secrets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=data_fd)
    finally:
        os.close(data_fd)
    try:
        _validate_private(os.fstat(directory_fd), directory=True)
        try:
            return _read_key(directory_fd)
        except FileNotFoundError:
            pass
        temporary = ".model-master." + secrets.token_hex(16) + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode=0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, "model-master.key", src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd, follow_symlinks=False)
                os.fsync(directory_fd)
            except FileExistsError:
                # Another process won publication. Load its complete key.
                pass
        finally:
            os.unlink(temporary, dir_fd=directory_fd)
        return _read_key(directory_fd)
    finally:
        os.close(directory_fd)


class ModelSecretVault:
    def __init__(self, settings: CloudSettings):
        try:
            if settings.model_master_key is not None:
                value = settings.model_master_key.get_secret_value()
                try:
                    key = value.encode("ascii")
                except UnicodeEncodeError:
                    raise ModelSecretError("Model master key is not a valid Fernet key") from None
            else:
                key = _load_or_create(settings)
            self._cipher = _cipher(key)
        except OSError:
            raise ModelSecretError("Model master key storage is unavailable or unsafe") from None

    def seal(self, plaintext: str) -> str:
        if not isinstance(plaintext, str) or not plaintext or len(plaintext) > 8192:
            raise ModelSecretError("Model API key must be a non-empty value of at most 8192 characters")
        try:
            return self._cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")
        except UnicodeError:
            raise ModelSecretError("Model API key contains invalid text") from None

    def unseal(self, ciphertext: str) -> str:
        if not isinstance(ciphertext, str) or not ciphertext or len(ciphertext) > 65536:
            raise ModelSecretError("Stored model key cannot be decrypted")
        try:
            plaintext = self._cipher.decrypt(ciphertext.encode("ascii")).decode("utf-8")
            if not plaintext:
                raise ModelSecretError("Stored model key cannot be decrypted")
            return plaintext
        except (InvalidToken, UnicodeError, ValueError):
            raise ModelSecretError("Stored model key cannot be decrypted") from None
