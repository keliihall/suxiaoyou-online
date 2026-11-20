"""Managed-key encryption with synthetic credentials and independent processes."""
import os
from pathlib import Path
import subprocess
import sys

from cryptography.fernet import Fernet
from pydantic import SecretStr
import pytest

from app.cloud.config import CloudSettings
from app.cloud.secrets import ModelSecretError, ModelSecretVault


@pytest.fixture(autouse=True)
def isolate_master_environment(monkeypatch):
    monkeypatch.delenv("SUXIAOYOU_CLOUD_MODEL_MASTER_KEY", raising=False)


def settings(path, **kwargs):
    return CloudSettings(environment="test", data_dir=path, **kwargs)


def test_shared_instances_decrypt_and_persist_private_master(tmp_path):
    config = settings(tmp_path / "cloud")
    first = ModelSecretVault(config)
    plaintext = "offline-synthetic-provider-key"
    encrypted = first.seal(plaintext)
    assert encrypted != first.seal(plaintext)
    assert plaintext not in encrypted
    assert ModelSecretVault(config).unseal(encrypted) == plaintext
    directory = config.data_dir / "secrets"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "model-master.key").stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in directory.iterdir()) == ["model-master.key"]


def test_environment_master_is_masked_and_does_not_create_local_file(tmp_path, monkeypatch):
    master = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("SUXIAOYOU_CLOUD_MODEL_MASTER_KEY", master)
    config = settings(tmp_path / "unused")
    assert isinstance(config.model_master_key, SecretStr)
    assert master not in repr(config)
    ciphertext = ModelSecretVault(config).seal("offline-environment-key")
    assert ModelSecretVault(config).unseal(ciphertext) == "offline-environment-key"
    assert not config.data_dir.exists()


def test_tampering_and_wrong_master_never_return_plaintext(tmp_path):
    vault = ModelSecretVault(settings(tmp_path / "first"))
    ciphertext = vault.seal("offline-sensitive-value")
    tampered = ciphertext[:20] + ("A" if ciphertext[20] != "A" else "B") + ciphertext[21:]
    second = ModelSecretVault(settings(tmp_path / "second"))
    for target, value in ((vault, tampered), (second, ciphertext), (vault, "broken ciphertext"), (vault, "")):
        with pytest.raises(ModelSecretError) as error:
            target.unseal(value)
        assert "offline-sensitive-value" not in str(error.value)
        if value:
            assert value not in str(error.value)


@pytest.mark.parametrize("master", ["", "invalid-master-value", "汉" * 44])
def test_invalid_explicit_master_fails_closed_without_echo_or_file(tmp_path, master):
    config = settings(tmp_path / "unused", model_master_key=master)
    with pytest.raises(ModelSecretError, match="not a valid Fernet key") as error:
        ModelSecretVault(config)
    if master:
        assert master not in str(error.value)
    assert not config.data_dir.exists()


@pytest.mark.parametrize("target,mode", [("file", 0o644), ("file", 0o400), ("directory", 0o755), ("directory", 0o750)])
def test_abnormal_file_or_directory_permissions_are_rejected(tmp_path, target, mode):
    config = settings(tmp_path / "cloud")
    ModelSecretVault(config)
    directory = config.data_dir / "secrets"
    path = directory / "model-master.key" if target == "file" else directory
    path.chmod(mode)
    with pytest.raises(ModelSecretError, match="private"):
        ModelSecretVault(config)


@pytest.mark.parametrize("target", ["file", "directory", "data"])
def test_symlink_master_or_directory_is_rejected(tmp_path, target):
    config = settings(tmp_path / "cloud")
    ModelSecretVault(config)
    directory = config.data_dir / "secrets"
    path = directory / "model-master.key" if target == "file" else directory if target == "directory" else config.data_dir
    renamed = tmp_path / "other-location"
    path.rename(renamed)
    path.symlink_to(renamed, target_is_directory=target != "file")
    with pytest.raises(ModelSecretError, match="unavailable or unsafe"):
        ModelSecretVault(config)


def test_corrupt_existing_master_is_not_silently_replaced(tmp_path):
    config = settings(tmp_path / "cloud")
    ModelSecretVault(config)
    path = config.data_dir / "secrets" / "model-master.key"
    path.write_bytes(b"corrupt-existing-key")
    with pytest.raises(ModelSecretError, match="not a valid Fernet key"):
        ModelSecretVault(config)
    assert path.read_bytes() == b"corrupt-existing-key"


def test_concurrent_first_creation_across_processes_uses_one_complete_key(tmp_path):
    directory = tmp_path / "shared-new-cloud"
    program = """
import sys
from pathlib import Path
from app.cloud.config import CloudSettings
from app.cloud.secrets import ModelSecretVault
vault = ModelSecretVault(CloudSettings(environment='test', data_dir=Path(sys.argv[1])))
print(vault.seal('offline-concurrent-key'))
"""
    # Clear only the master-key variable so no developer credential can enter
    # this test. Each subprocess independently imports and initializes the vault.
    environment = {name: value for name, value in os.environ.items() if name != "SUXIAOYOU_CLOUD_MODEL_MASTER_KEY"}
    processes = [subprocess.Popen([sys.executable, "-c", program, str(directory)],
                                  cwd=Path(__file__).resolve().parents[2], env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
    try:
        ciphertexts = []
        for process in processes:
            output, error = process.communicate(timeout=30)
            assert process.returncode == 0, error.decode()
            ciphertexts.append(output.decode().strip())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    vault = ModelSecretVault(settings(directory))
    assert all(vault.unseal(value) == "offline-concurrent-key" for value in ciphertexts)
    assert sorted(path.name for path in (directory / "secrets").iterdir()) == ["model-master.key"]


@pytest.mark.parametrize("value", [None, "", "\ud800"])
def test_invalid_plaintext_has_controlled_errors(tmp_path, value):
    vault = ModelSecretVault(settings(tmp_path / "cloud"))
    with pytest.raises(ModelSecretError):
        vault.seal(value)
